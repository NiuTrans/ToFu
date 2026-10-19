"""Demand injection acknowledgements preserve all other durable intent."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from lib.storage.errors import StorageError
from tests.storage.test_storage_authority_sql_contract import storage, _import_conversation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('size', [1, 32, 1025])
def test_facade_one_command_no_list(monkeypatch, size):
    from lib import message_queue
    client = Mock()
    client.command.return_value = {'removed': size}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **kw: client)
    assert message_queue.dedup_inbox_durable_rows(
        'c', [f'q-{i}' for i in range(size)], user_id=7) == size
    client.query.assert_not_called()
    client.command.assert_called_once()
    args, kwargs = client.command.call_args
    assert args[0] == 'queue.injection.ack'
    assert args[1]['user_id'] == 7
    assert len(args[1]['queue_ids']) == size
    assert kwargs['command_id'].startswith('queue-injection-ack:')


def test_facade_large_iterable_and_failure(monkeypatch):
    from lib import message_queue
    client = Mock()
    client.command.return_value = {'removed': 1025}
    monkeypatch.setattr(message_queue, '_queue_client', lambda **kw: client)
    assert message_queue.dedup_inbox_durable_rows(
        'c', (f'q-{i}' for i in range(1025)), user_id=1) == 1025
    client.command.assert_called_once()
    assert len(client.command.call_args.args[1]['queue_ids']) == 1025
    client.command.side_effect = RuntimeError('lost connection')
    assert message_queue.dedup_inbox_durable_rows('c', ['q'], user_id=1) == 0


def test_actual_storage_owner_kind_link_and_replay(storage):  # noqa: F811
    client = storage.client
    _import_conversation(client, 'ack-c', user_id=1)
    _import_conversation(client, 'ack-other', user_id=2)
    fixtures = [
        ('peer', 'peer_msg', {'_peerMessage': True}),
        ('bg', 'workflow_step', {'_backgroundCommand': 'command'}),
        ('human-peer', 'peer_msg', {'_peerMessage': True, '_peerHuman': True}),
        ('human', 'real', {'text': 'keep'}),
        ('workflow', 'workflow_step', {'text': 'keep'}),
        ('unrelated', 'peer_msg', {'_peerMessage': True}),
    ]
    for qid, kind, message in fixtures:
        client.command('queue.enqueue', {
            'conv_id': 'ack-c', 'user_id': 1, 'queue_id': qid,
            'kind': kind, 'message': message,
        }, f'enqueue:{qid}')
    client.command('queue.enqueue', {
        'conv_id': 'ack-other', 'user_id': 2, 'queue_id': 'foreign',
        'kind': 'peer_msg', 'message': {'_peerMessage': True},
    }, 'enqueue:foreign')
    payload = {'conv_id': 'ack-c', 'user_id': 1,
               'queue_ids': ['peer', 'bg', 'human-peer', 'human', 'workflow', 'foreign', 'missing', 'peer']}
    expected = {'removed': 2, 'removedQueueIds': ['peer', 'bg']}
    assert client.command('queue.injection.ack', payload, 'ack:batch') == expected
    assert client.command('queue.injection.ack', payload, 'ack:batch') == expected
    assert client.command('queue.injection.ack', payload, 'ack:again')['removed'] == 0
    changes = client.query('turn.sync.changes', {
        'conversation_id': 'ack-c', 'user_id': 1, 'after': 0,
    })
    assert len(changes['events']) == 1
    assert changes['events'][0]['payload']['removedQueueIds'] == ['peer', 'bg']
    remaining = client.query('queue.list', {'conv_id': 'ack-c', 'user_id': 1})
    assert {row['queueId'] for row in remaining} == {'human-peer', 'human', 'workflow', 'unrelated'}
    assert sorted(row['position'] for row in remaining) == [1, 2, 3, 4]
    assert len(client.query('queue.list', {'conv_id': 'ack-other', 'user_id': 2})) == 1
    assert client.command('queue.injection.ack', {
        'conv_id': 'ack-c', 'user_id': 2, 'queue_ids': ['unrelated'],
    }, 'ack:wrong-owner')['removed'] == 0
    with pytest.raises(StorageError):
        client.command('queue.injection.ack', {
            **payload, 'queue_ids': ['x', None],
        }, 'ack:malformed')


def test_link_columns_and_atomic_replay(tmp_path, monkeypatch):
    import sqlite3
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    from lib.storage_authority_sql.schema import initialize_schema
    from lib.storage_authority_sql.operations_pkg import _queue as ops
    from lib.storage_authority_sql.operations_pkg._common import _load
    connection = sqlite3.connect(tmp_path / 'ack.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    try:
        session.execute('INSERT INTO storage_conversations (id,user_id,title,messages_json,created_at_ms,updated_at_ms,settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('c', 1, '', '[]', 1, 1, '{}', 0, '', 1))
        for index, column in enumerate(['input_turn_id', 'output_turn_id', 'attempt_id', '']):
            qid = f'q{index}'
            ops._queue_enqueue(session, {'conv_id': 'c', 'user_id': 1,
                'queue_id': qid, 'kind': 'peer_msg', 'message': {'_peerMessage': True}})
            if column:
                session.execute(f'UPDATE storage_queue_items SET {column}=? WHERE id=? AND user_id=?', ('linked', qid, 1))
        connection.commit()
        renumber = Mock(wraps=ops._queue_renumber)
        monkeypatch.setattr(ops, '_queue_renumber', renumber)
        payload = {'conv_id': 'c', 'user_id': 1, 'queue_ids': ['q0', 'q1', 'q2', 'q3']}
        result = ops._queue_injection_ack(session, payload)
        assert result == {'removed': 1, 'removedQueueIds': ['q3']}
        renumber.assert_called_once()
        ops._queue_injection_ack_capture(session, 'queue.injection.ack', payload, result)
        changes = session.fetch_all('SELECT event_json FROM storage_conversation_changes WHERE conversation_id=? AND user_id=?', ('c', 1))
        assert len(changes) == 1
        assert _load(changes[0]['event_json'])['payload']['removedQueueIds'] == ['q3']
        assert len(ops._queue_list(session, payload)) == 3
        connection.rollback()
        assert len(session.fetch_all('SELECT id FROM storage_queue_items')) == 4
        assert session.fetch_all('SELECT event_json FROM storage_conversation_changes') == []
    finally:
        connection.close()


@pytest.mark.parametrize('case', ['wrong-command', 'linked', 'human', 'many-duplicates', 'duplicate'])
def test_background_receipt_rejects_protected_or_malformed_delivery(tmp_path, case):
    import sqlite3
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    from lib.storage_authority_sql.schema import initialize_schema
    from lib.storage_authority_sql.operations_pkg import _queue as ops
    connection = sqlite3.connect(tmp_path / 'receipt-guard.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    try:
        session.execute('INSERT INTO storage_conversations (id,user_id,title,messages_json,created_at_ms,updated_at_ms,settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('c', 1, '', '[]', 1, 1, '{}', 0, '', 1))
        message = {'_backgroundCommand': 'bg'}
        if case == 'human':
            message['_peerHuman'] = True
        ops._queue_enqueue(session, {'conv_id': 'c', 'user_id': 1, 'queue_id': 'q',
            'kind': 'workflow_step', 'message': message})
        if case == 'linked':
            session.execute('UPDATE storage_queue_items SET attempt_id=? WHERE id=?', ('accepted', 'q'))
        connection.commit()
        command_id = 'wrong' if case == 'wrong-command' else 'bg'
        deliveries = [{'queue_id': 'q', 'command_id': command_id}]
        if case == 'duplicate':
            deliveries *= 2
        elif case == 'many-duplicates':
            deliveries *= 1025
        projection = {'_bgCommandInjects': [{'blockId': 'receipt', 'previews': [
            {'queueId': item['queue_id'], 'commandId': item['command_id']} for item in deliveries]}]}
        with pytest.raises(StorageError):
            ops.acknowledge_background_command_receipt(session, conv_id='c', user_id=1,
                acknowledgement={'block_id': 'receipt', 'deliveries': deliveries}, projection=projection)
        assert len(ops._queue_list(session, {'conv_id': 'c', 'user_id': 1})) == 1
        # Even valid evidence in another owner/conversation scope cannot remove it.
        assert ops.acknowledge_background_command_receipt(session, conv_id='c', user_id=2,
            acknowledgement={'block_id': 'receipt', 'deliveries': [{'queue_id': 'q', 'command_id': 'bg'}]},
            projection={'_bgCommandInjects': [{'blockId': 'receipt', 'previews': [{'queueId': 'q', 'commandId': 'bg'}]}]}) == []
        assert len(ops._queue_list(session, {'conv_id': 'c', 'user_id': 1})) == 1
    finally:
        connection.close()


def test_large_ack_windows_preserve_order_protected_rows_and_rollback(tmp_path, monkeypatch):
    import sqlite3
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    from lib.storage_authority_sql.schema import initialize_schema
    from lib.storage_authority_sql.operations_pkg import _queue as ops

    connection = sqlite3.connect(tmp_path / 'large-ack.sqlite')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    session.execute('INSERT INTO storage_conversations (id,user_id,title,messages_json,created_at_ms,updated_at_ms,settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('large', 1, '', '[]', 1, 1, '{}', 0, '', 1))
    ids = [f'q-{index:04}' for index in range(1025)]
    for queue_id in ids:
        ops._queue_enqueue(session, {'conv_id':'large','user_id':1,'queue_id':queue_id,
            'kind':'peer_msg','message':{'text':queue_id,'_peerMessage':True}})
    ops._queue_enqueue(session, {'conv_id':'large','user_id':1,'queue_id':'protected',
        'kind':'real','message':{'text':'keep'}})
    connection.commit()
    payload = {'conv_id':'large','user_id':1,
        'queue_ids':ids + ['protected'] + [f'absent-{i}' for i in range(70_000)]}
    original = session.execute
    deletes = 0

    def fail_second_delete(sql, parameters=()):
        nonlocal deletes
        if sql.startswith('DELETE FROM storage_queue_items'):
            deletes += 1
            if deletes == 2:
                raise RuntimeError('injected second-window failure')
        return original(sql, parameters)

    monkeypatch.setattr(session, 'execute', fail_second_delete)
    with pytest.raises(RuntimeError, match='second-window'):
        ops._queue_injection_ack(session, payload)
    connection.rollback()
    assert session.fetch_one('SELECT COUNT(*) AS count FROM storage_queue_items')['count'] == 1026
    monkeypatch.setattr(session, 'execute', original)
    result = ops._queue_injection_ack(session, payload)
    assert result == {'removed':1025,'removedQueueIds':ids}
    connection.commit()
    assert ops._queue_injection_ack(session, payload)['removed'] == 0
    assert [item['queueId'] for item in ops._queue_list(session, {'conv_id':'large','user_id':1})] == ['protected']
    connection.close()

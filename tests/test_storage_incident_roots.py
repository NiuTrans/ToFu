"""Regression pins for the 2026-08-23 storage availability incident family."""

from __future__ import annotations

import orjson
import pytest

from lib.storage.errors import StorageError


pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]
pytest_plugins = ('tests._chat_sidecar',)


def test_task_result_summary_never_returns_heavy_values(chat_sidecar):
    from lib.storage import get_storage_client

    client = get_storage_client(write=True)
    for index, status in enumerate(('done', 'running', 'running')):
        client.command(
            'record.put', {
                'namespace': 'task_results',
                'key': f'incident-summary-{index}',
                'value': {
                    'task_id': f'incident-summary-{index}',
                    'conv_id': 'incident-conv',
                    'user_id': 1,
                    'status': status,
                    'created_at': 100 + index,
                    'completed_at': 200 + index,
                    'content': 'x' * (1024 * 1024),
                    'thinking': 'never cross the summary wire',
                },
            },
            f'incident-summary-put-{index}',
        )
    client.command(
        'record.put', {
            'namespace': 'task_results',
            'key': 'incident-summary-foreign',
            'value': {
                'task_id': 'incident-summary-foreign',
                'conv_id': 'incident-conv',
                'user_id': 2,
                'status': 'running',
                'created_at': 999,
                'completed_at': 999,
            },
        },
        'incident-summary-put-foreign',
    )

    result = client.query(
        'task_results.summary_list', {
            'conv_id': 'incident-conv',
            'user_id': 1,
            'status': 'running',
            'limit': 10,
            'scan_limit': 1000,
            'order_by': 'created_at_desc',
        }, deadline=30)

    assert [row['key'] for row in result['records']] == [
        'incident-summary-2', 'incident-summary-1']
    assert all('value' not in row and 'content' not in row
               for row in result['records'])
    assert len(orjson.dumps(result)) < 4096


def test_task_result_checkpoint_replays_from_authority_without_receipt():
    from lib.storage import get_storage_client

    client = get_storage_client(write=True)
    payload = {
        'key': 'incident-natural-checkpoint',
        'value': {
            'task_id': 'incident-natural-checkpoint',
            'status': 'running',
            'content': 'same committed projection',
        },
        'expected_version': 0,
    }
    first = client.command(
        'task_results.checkpoint', payload, 'checkpoint-first')
    # A different/absent command id proves replay comes from the authority
    # value comparison, not the permanent command-receipt table.
    replay = client.command('task_results.checkpoint', payload, None)
    assert replay == first
    assert client.query('record.get', {
        'namespace': 'task_results',
        'key': payload['key'],
    })['version'] == 1

    advanced = client.command('task_results.checkpoint', {
        **payload,
        'value': {**payload['value'], 'content': 'newer projection'},
        'expected_version': 1,
    }, None)
    assert advanced['version'] == 2
    with pytest.raises(StorageError) as stale:
        client.command('task_results.checkpoint', payload, None)
    assert stale.value.code == 'database_conflict'


def test_clean_none_command_result_is_not_a_permanent_receipt():
    from lib.storage_sidecar.adapters.base import receipt_cacheable

    assert receipt_cacheable(None) is False


def test_board_refresh_and_repeated_lifecycle_do_not_replay_stale_receipts():
    from lib.conversations.project_board import (
        claim_task,
        complete_task,
        post_task,
        read_board,
        reopen_task,
    )

    project = '/incident/board-receipts'
    task_id = post_task(
        project, 'conv-a', 'repeatable lifecycle', user_id=1)['id']

    first_claim = claim_task(
        project, 'conv-worker', task_id, user_id=1,
        ttl_ms=60_000, dispatched=False)
    refreshed = claim_task(
        project, 'conv-worker', task_id, user_id=1,
        ttl_ms=120_000, dispatched=True)
    assert first_claim['ok'] is True
    assert refreshed['ok'] is True
    assert refreshed['refreshed'] is True
    assert refreshed['transitioned'] is False
    assert refreshed['lease_expires_at'] > first_claim['lease_expires_at']
    claimed = next(
        t for t in read_board(project, user_id=1)['tasks']
        if t['id'] == task_id)
    assert claimed['dispatched'] is True

    assert complete_task(
        project, 'conv-worker', task_id, user_id=1)['transitioned'] is True
    assert reopen_task(
        project, 'human', task_id, user_id=1)['transitioned'] is True
    # The old permanent board.complete receipt returned success here without
    # executing, leaving the task open.
    assert complete_task(
        project, 'conv-worker', task_id, user_id=1)['transitioned'] is True
    assert read_board(project, user_id=1)['done'] == 1
    # The same stale-receipt bug existed in the other direction for reopen.
    assert reopen_task(
        project, 'human', task_id, user_id=1)['transitioned'] is True
    assert read_board(project, user_id=1)['open'] == 1
    repeated = reopen_task(project, 'human', task_id, user_id=1)
    assert repeated['ok'] is False and repeated['error'] == 'already_open'


def test_watch_set_operations_can_cycle_back_to_an_earlier_value():
    from lib.conversations.project_watch import (
        add_watch_item,
        edit_watch_item,
        list_watch_items,
        set_watch_status,
    )

    item = add_watch_item(
        '/incident/watch-receipts', 'concern', 'A', user_id=1)['item']
    item_id = item['item_id']
    assert edit_watch_item(item_id, user_id=1, text='B')['ok']
    assert edit_watch_item(item_id, user_id=1, text='A')['ok']
    assert edit_watch_item(item_id, user_id=1, text='B')['ok']
    current = next(
        row for row in list_watch_items(
            '/incident/watch-receipts', user_id=1,
            include_resolved=True)['items']
        if row['item_id'] == item_id)
    assert current['text'] == 'B'

    assert set_watch_status(item_id, 'resolved', user_id=1)['ok']
    assert set_watch_status(item_id, 'open', user_id=1)['ok']
    assert set_watch_status(item_id, 'resolved', user_id=1)['ok']
    current = next(
        row for row in list_watch_items(
            '/incident/watch-receipts', user_id=1,
            include_resolved=True)['items']
        if row['item_id'] == item_id)
    assert current['status'] == 'resolved'


def test_oversized_response_encoding_returns_a_classified_error(monkeypatch):
    from lib.storage_sidecar import server as sidecar_server

    handler = object.__new__(sidecar_server._StorageHandler)
    handler.request = object()
    sent = []

    def fake_send_frame(_socket, message):
        sent.append(message)
        if len(sent) == 1:
            raise StorageError(
                'database_protocol_error',
                'Storage frame exceeds the size limit',
            )

    monkeypatch.setattr(sidecar_server, 'send_frame', fake_send_frame)
    handler._send_response(
        {'protocol': 'storage.v1', 'request_id': 'req-1', 'ok': True,
         'result': {'oversized': True}},
        'req-1', 'record.list')

    assert len(sent) == 2
    fallback = sent[1]
    assert fallback['request_id'] == 'req-1'
    assert fallback['ok'] is False
    assert fallback['error']['code'] == 'database_protocol_error'
    assert fallback['error']['message'] == 'Storage frame exceeds the size limit'


def test_reclaim_checks_wall_budget_between_single_page_units(monkeypatch):
    from lib.storage_sidecar.operations_pkg import _common

    class FakeSession:
        backend = 'sqlite'

        def __init__(self):
            self.free = 10
            self.statements = []

        def fetch_one(self, statement, _params=()):
            if statement == 'PRAGMA auto_vacuum':
                return {'auto_vacuum': 2}
            if statement == 'PRAGMA freelist_count':
                return {'freelist_count': self.free}
            if statement == 'PRAGMA page_count':
                return {'page_count': 100}
            if statement == 'PRAGMA page_size':
                return {'page_size': 4096}
            raise AssertionError(statement)

        def execute(self, statement, _params=()):
            self.statements.append(statement)
            assert statement == 'PRAGMA incremental_vacuum(1)'
            self.free -= 1
            return 1

    ticks = iter((0.0, 0.0, 0.2))
    monkeypatch.setattr(_common.time, 'monotonic', lambda: next(ticks))
    session = FakeSession()

    result = _common._system_reclaim(
        session, {'max_pages': 10, 'min_free_pages': 1, 'budget_ms': 100})

    assert result['reclaimed'] == 1
    assert session.statements == ['PRAGMA incremental_vacuum(1)']

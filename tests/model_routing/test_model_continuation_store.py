"""Real Sidecar contracts for bounded, non-authoritative model continuation."""
from concurrent.futures import ThreadPoolExecutor
import copy
import os
import uuid

import pytest

from lib.storage import StorageSupervisor, StorageError
from lib.storage.model_continuation import (
    ModelContinuationStore, MAX_ENTRY_BYTES, MAX_MESSAGES, MAX_TURN_WITNESSES,
    settled_witness,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite'] + (['postgres'] if os.getenv('TOFU_STORAGE_TEST_POSTGRES') == '1' else []))
def authority(request, tmp_path):
    supervisor = StorageSupervisor(project_root=tmp_path, backend=request.param, startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def command(client, operation, payload):
    return client.command(operation, payload, uuid.uuid4().hex)


def seed(client, conv='continuation'):
    command(client, 'conversation.create', {'conv_id': conv, 'user_id': 1})
    rows = []
    for index in range(3):
        command(client, 'turn.append_settled', {
            'conversation_id': conv, 'user_id': 1, 'turn_id': f'{conv}-{index}',
            'command_id': uuid.uuid4().hex, 'actor': 'assistant',
            'projection': {'content': f'answer {index}'}})
        rows.append(client.query('turn.get', {
            'conversation_id': conv, 'user_id': 1, 'turn_id': f'{conv}-{index}'}))
    return rows


def projection(rows):
    return {'turns': [settled_witness(row) for row in rows], 'policy': 'fixture',
            'messages': [{'role': 'assistant', 'content': 'exact compacted model context'}]}


def test_survives_restart_and_does_not_mutate_transcript(authority):
    rows = seed(authority.client)
    key = dict(user_id=1, conversation_id='continuation', lane_id='main')
    before = authority.client.query('conversation.get', {'conv_id': 'continuation', 'user_id': 1})
    store = ModelContinuationStore(client=authority.client)
    value = projection(rows)
    assert store.put(**key, projection=value) is True
    assert store.put(**key, projection=value) is True
    assert authority.client.query('conversation.get', {'conv_id': 'continuation', 'user_id': 1}) == before
    authority.stop()
    authority.start()
    assert ModelContinuationStore(client=authority.client).get(**key) == value


def test_stale_writers_owner_lane_witness_and_bounds(authority):
    rows = seed(authority.client)
    key = dict(user_id=1, conversation_id='continuation', lane_id='main')
    store = ModelContinuationStore(client=authority.client)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda count: store.put(**key, projection=projection(rows[:count])), [3, 1, 2]))
    newest = projection(rows)
    assert store.get(**key) == newest
    assert not store.put(**key, projection=projection(rows[:1]))
    different = copy.deepcopy(newest)
    different['messages'][0]['content'] = 'same frontier must not replace'
    assert not store.put(**key, projection=different)
    forged = copy.deepcopy(newest)
    forged['turns'][-1][1] = '0' * 64
    assert not store.put(**key, projection=forged)
    for override in ({'user_id': 2}, {'lane_id': 'foreign'}, {'conversation_id': 'missing'}):
        foreign = key | override
        assert store.get(**foreign) is None
        assert not store.put(**foreign, projection=newest)
        assert not store.delete(**foreign)
    with pytest.raises(StorageError):
        store.get(**(key | {'user_id': True}))
    oversized = copy.deepcopy(newest)
    oversized['messages'][0]['content'] = '界' * (MAX_ENTRY_BYTES // 3 + 1)
    assert not store.put(**key, projection=oversized)
    for field, values in (
            ('messages', [{'role': 'assistant'}] * (MAX_MESSAGES + 1)),
            ('turns', [newest['turns'][0]] * (MAX_TURN_WITNESSES + 1))):
        invalid = newest | {field: values}
        # Sidecar independently enforces bounds even when bypassing the repository.
        assert command(authority.client, 'model_continuation.put', key | {'projection': invalid}) is False
    assert store.get(**key) == newest


def test_delete_restore_purge_and_explicit_delete(authority):
    rows = seed(authority.client)
    store = ModelContinuationStore(client=authority.client)
    key = dict(user_id=1, conversation_id='continuation', lane_id='main')
    value = projection(rows)
    assert store.put(**key, projection=value)
    assert store.delete(**key)
    assert not store.delete(**key)
    assert store.put(**key, projection=value)
    command(authority.client, 'conversation.delete', {'conv_id': 'continuation', 'user_id': 1})
    assert store.get(**key) is None
    assert not store.put(**key, projection=value)
    command(authority.client, 'conversation.restore', {'conv_id': 'continuation', 'user_id': 1})
    assert store.get(**key) is None
    # Restore changes the authority witnesses; old continuation cannot revive.
    assert not store.put(**key, projection=value)
    restored = [authority.client.query('turn.get', {
        'conversation_id': 'continuation', 'user_id': 1, 'turn_id': row['turnId'],
    }) for row in rows]
    assert store.put(**key, projection=projection(restored))
    command(authority.client, 'conversation.purge', {'conv_id': 'continuation', 'user_id': 1})
    assert store.get(**key) is None


@pytest.mark.parametrize('route_switch', [False, True])
def test_automatic_handoff_survives_durable_restart_and_later_stop(
        authority, monkeypatch, route_switch):
    from lib import storage
    from lib.tasks_pkg import model_replay as replay
    from lib.tasks_pkg.compaction._handoff import (
        HANDOFF_GUIDANCE, is_compaction_handoff, make_compaction_handoff,
    )
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, context_block, render_context,
    )
    from tests.compaction.test_compaction_history_providers import _text, _wire, PROVIDERS
    from tests.compaction.test_compaction_history_wire import tool_round, user

    conv = 'handoff-restart'
    command(authority.client, 'conversation.create', {'conv_id': conv, 'user_id': 1})
    rows = []
    for turn_id, actor, text in [('u1', 'human', 'Implement the scoped fix.'),
                                  ('a1', 'assistant', 'Verified and ready to report.')]:
        command(authority.client, 'turn.append_settled', {
            'conversation_id': conv, 'user_id': 1, 'turn_id': turn_id,
            'command_id': uuid.uuid4().hex, 'actor': actor,
            'projection': {'content': text}})
        rows.append(authority.client.query('turn.get', {
            'conversation_id': conv, 'user_id': 1, 'turn_id': turn_id}))
    before = authority.client.query('conversation.get', {'conv_id': conv, 'user_id': 1})
    monkeypatch.setattr(storage, 'get_storage_client', lambda **k: authority.client)
    handoff = make_compaction_handoff(
        '### Objective\nImplement the scoped fix.\n'
        '### Completed & Verified\nSaved the fix; do not repeat that write.',
        phase='mid_turn')
    messages = [user('Implement the scoped fix.'), *tool_round('kept'), handoff,
                {'role': 'assistant', 'content': rows[-1]['projection']['content']}]
    config = {'model': 'gpt-4o', '_modelReplayBase': [replay.turn_witness(rows[0])]}
    replay.publish_completed_task({
        'convId': conv, '_turnId': 'a1', '_userId': 1, 'model': 'gpt-4o',
        'config': config, 'content': rows[-1]['projection']['content'],
        'messages': messages})
    assert ModelContinuationStore(client=authority.client).get(
        user_id=1, conversation_id=conv, lane_id='main')['messages'] == messages
    assert authority.client.query('conversation.get', {'conv_id': conv, 'user_id': 1}) == before
    authority.stop()
    authority.start()

    # Storage, ownership/witness validation, composer and actual provider
    # conversion are exercised together; no durable fabricated user Turn.
    stop = user('Stop editing; report only.')
    next_rows = rows + [{
        'turnId': 'u2', 'actor': 'human', 'laneId': 'main', 'status': 'completed',
        'projection': {'content': stop['content']},
    }, {'turnId': 'a2', 'actor': 'assistant', 'laneId': 'main',
        'status': 'running', 'projection': {}}]
    restored_config = {'model': 'kimi-k3' if route_switch else 'gpt-4o'}
    restored, covered = replay.restore_prefix(
        next_rows, restored_config, conversation_id=conv, user_id=1)
    assert covered == 2
    assert restored_config['_modelReplayStatus'] == (
        'restored_portable' if route_switch else 'restored')
    assert restored == messages
    assert is_compaction_handoff(restored[-2])
    restored.append(stop)
    render_context(restored, [context_block('environment', 'restored environment')],
                   ComposeRequest(model=restored_config['model']))
    assert restored.index(handoff) < restored.index(stop)
    for provider in PROVIDERS:
        body = _wire(restored, provider)
        text = '\n'.join(_text(row) for row in body)
        assert text.count(HANDOFF_GUIDANCE) == 1
        assert text.index('</compaction_handoff>') < text.index(stop['content'])
        assert 'restored environment' in text
    assert replay.restore_prefix(
        next_rows, dict(restored_config), conversation_id=conv, user_id=2) == ([], 0)
    assert authority.client.query('conversation.get', {'conv_id': conv, 'user_id': 1}) == before


def test_catalog_live_cas_has_no_receipt_payload_history():
    from lib.storage_authority_sql.operation_registry import build_registry
    registry = build_registry()
    for name in ('get', 'put', 'delete'):
        assert not registry[f'model_continuation.{name}'].receipt_required

"""Conversation-attempt fence on standalone event appends (2026-09-18 fix).

Coalesced stream frames skip the ``turn.event.record`` transaction, so their
attempt-liveness fence travels with the standalone ``event.append`` as
``require_attempts_live`` and the storage authority enforces it INSIDE the
append transaction. This replaced a per-frame authoritative pre-read that
cost one extra round trip per visible delta (~10-40 reads/s per streaming
task) and queued hardest exactly when the authority was wedged.

Pinned here:
(a) live/missing/settled verdicts and fence bounds on ``event.append``;
(b) per-row verdicts and aggregate counts on ``event.append_batch``;
(c) ``append_persistent_event`` maps a rejection to the definitive
    :class:`EventAttemptStaleError` and persists nothing;
(d) the coalesced lane performs no attempt pre-read and hands the fence to
    the manager's standalone append, whose rejection plants the same
    cooperative-abort stamp the old pre-read produced;
(e) the conversation event stream degrades on wedge-classified read
    failures instead of dying mid-stream with a 503.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.fixture
def storage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture
def turn_service(storage, monkeypatch):
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    from lib import turn_lifecycle
    return turn_lifecycle


def _live_attempt(turn_service, conv_id: str) -> tuple[str, str]:
    created = turn_service.create_turn_pair(
        conv_id,
        command_id=f'{conv_id}-pair',
        input_projection={'content': 'q'},
        config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'fence', 'settings': {},
        })
    return created['attempt']['attemptId'], created['turn']['turnId']


def _append_payload(task_id: str, sequence: int, fence) -> dict:
    payload = {
        'task_id': task_id,
        'sequence': sequence,
        'event': {'type': 'delta', 'text': 'x'},
    }
    if fence is not None:
        payload['require_attempts_live'] = fence
    return payload


def test_fenced_append_liveness_verdicts(storage, turn_service):
    client = storage.client
    live_id, _turn_id = _live_attempt(turn_service, 'fence-live-conv')

    inserted = client.command(
        'event.append', _append_payload('fence-task-live', 1, [live_id]),
        None, priority='event')
    assert inserted == {
        'inserted': True, 'task_id': 'fence-task-live', 'sequence': 1,
    }

    missing = client.command(
        'event.append', _append_payload('fence-task-missing', 1, ['no-such']),
        None, priority='event')
    assert missing == {
        'inserted': False, 'rejected': 'attempt_not_live',
        'task_id': 'fence-task-missing', 'sequence': 1,
    }

    # Settle through the public terminal path used by every producer.
    from lib.turn_lifecycle import get_attempt
    attempt = get_attempt(live_id, user_id=1)
    task = {
        '_attemptId': live_id,
        '_userId': 1,
        'id': 'fence-settling-task',
        'status': 'completed',
        'content': 'done',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'config': {'model': 'gpt-4o'},
        'convId': attempt['conversationId'],
        '_turnId': attempt['turnId'],
    }
    del _turn_id
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True

    settled = client.command(
        'event.append', _append_payload('fence-task-settled', 1, [live_id]),
        None, priority='event')
    assert settled == {
        'inserted': False, 'rejected': 'attempt_not_live',
        'task_id': 'fence-task-settled', 'sequence': 1,
    }
    # Rejections persist nothing on the cold-replay lane.
    assert client.query('event.list', {'task_id': 'fence-task-settled'}) == []
    assert client.query('event.list', {'task_id': 'fence-task-missing'}) == []


def test_fenced_append_bounds(storage):
    client = storage.client
    for bad_fence in ([], ['a', 'b', 'c'], ['x' * 129], [''], 'not-a-list',
                      [42]):
        with pytest.raises(StorageError) as raised:
            client.command(
                'event.append', _append_payload('fence-bounds', 1, bad_fence),
                None, priority='event')
        assert raised.value.code == 'database_protocol_error', bad_fence


def test_fenced_append_batch_per_row_verdicts(storage, turn_service):
    client = storage.client
    live_id, _turn_id = _live_attempt(turn_service, 'fence-batch-conv')
    result = client.command('event.append_batch', {
        'events': [
            _append_payload('fence-batch-a', 1, [live_id]),
            _append_payload('fence-batch-b', 1, ['no-such-attempt']),
            _append_payload('fence-batch-c', 1, None),
        ],
    }, None, priority='event')
    assert result['inserted'] == 2
    assert result['deduplicated'] == 0
    assert result['rejected'] == 1
    rows = result['results']
    assert rows[0]['inserted'] is True and 'rejected' not in rows[0]
    assert rows[1] == {
        'inserted': False, 'rejected': 'attempt_not_live',
        'task_id': 'fence-batch-b', 'sequence': 1,
    }
    assert rows[2]['inserted'] is True
    assert client.query('event.list', {'task_id': 'fence-batch-b'}) == []

    unfenced = client.command('event.append_batch', {
        'events': [_append_payload('fence-batch-d', 1, None)],
    }, None, priority='event')
    assert unfenced['rejected'] == 0


def test_append_persistent_event_maps_stale_fence(storage, turn_service,
                                                  monkeypatch):
    from lib.tasks_pkg import event_log

    live_id, _turn_id = _live_attempt(turn_service, 'fence-persist-conv')
    ok = event_log.append_persistent_event(
        'fence-persist-live', 1, {'type': 'delta', 'text': 'x'},
        require_attempts_live=[live_id])
    assert ok['inserted'] is True

    with pytest.raises(event_log.EventAttemptStaleError):
        event_log.append_persistent_event(
            'fence-persist-stale', 1, {'type': 'delta', 'text': 'x'},
            require_attempts_live=['no-such-attempt'])
    assert event_log.read_events('fence-persist-stale') == []

    with pytest.raises(event_log.EventDurabilityError):
        event_log.append_persistent_event(
            'fence-persist-shape', 1, {'type': 'delta'},
            require_attempts_live=['a', 'b', 'c'])


def test_coalesced_lane_hands_fence_to_the_standalone_append(
        storage, turn_service, monkeypatch):
    """No per-frame pre-read; the append transaction carries the fence."""
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    # Keep every delta inside the coalescing window after the first write.
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '60000')
    conv_id = 'fence-coalesced-conv'
    task_id = 'fence-coalesced-task'
    chat_task_runtime.discard(task_id)
    attempt_id, turn_id = _live_attempt(turn_service, conv_id)
    turn_service.claim_attempt_start(attempt_id, user_id=1)
    turn_service.bind_task(attempt_id, task_id, user_id=1)
    turn_service.mark_task_started(attempt_id, task_id, user_id=1)

    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    task.update({
        'convId': conv_id,
        '_attemptId': attempt_id,
        '_turnId': turn_id,
        '_userId': 1,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'config': {'model': 'gpt-4o'},
    })
    real_get_attempt = turn_service.get_attempt
    pre_reads = []
    monkeypatch.setattr(
        turn_service, 'get_attempt',
        lambda *args, **kwargs: (
            pre_reads.append(args[0] if args else kwargs.get('attempt_id')),
            real_get_attempt(*args, **kwargs))[1])
    try:
        manager.append_event(task, {'type': 'delta', 'content': 'a'})
        manager.append_event(task, {'type': 'delta', 'content': 'ab'})
        # The first frame takes the write lane (one baseline read); the
        # coalesced second frame must not add another pre-read.
        assert len(pre_reads) <= 1

        # Settle the attempt, then stay inside the coalescing window: the
        # append transaction's fence is the only verdict left.
        task['status'] = 'completed'
        assert turn_service.record_task_event(
            task, {'type': 'done', 'finishReason': 'stop'}) is True
        monkeypatch.setattr(
            'lib.turn_lifecycle._delta_throttle_allows', lambda _id: False)
        manager.append_event(task, {'type': 'delta', 'content': 'too late'})
        assert task.get('aborted') is True
        assert task.get('_abort_reason') == 'conversation_attempt_stale_fence'
        # The fenced rejection persists nothing on the cold-replay lane.
        from lib.tasks_pkg import event_log
        assert all(
            (event.get('payload') or {}).get('content') != 'too late'
            for event in event_log.read_events(task_id))
    finally:
        chat_task_runtime.discard(task_id)


def test_sync_events_stream_degrades_on_wedge_read_failures(monkeypatch):
    """A deadline_elapsed read page must not kill the SSE stream (503)."""
    from quart import Quart, g, request

    import routes.conversation_sync_v3 as sync_routes
    from lib.api_keys import local_admin_context

    class WedgedService:
        heartbeat_interval_ms = 50

        def __init__(self):
            self.calls = 0

        def sequence_from_cursor(self, _conversation_id, _user_id, _cursor):
            return 0

        def changes(self, _conversation_id, _user_id, *, after_sequence,
                    limit):
            self.calls += 1
            if self.calls == 1:
                return {'events': [], 'reset': None, 'hasMore': False}
            if self.calls == 2:
                raise StorageError('database_timeout', 'deadline_elapsed')
            return {
                'events': [],
                'reset': {'type': 'sync.reset_required', 'cursor': 'c1'},
                'hasMore': False,
            }

        def heartbeat(self, conversation_id, user_id, sequence, *, degraded,
                      push_withheld=False):
            return {
                'contract': 'tofu.conversation-sync.event/v1',
                'type': 'sync.heartbeat',
                'conversationId': conversation_id,
                'cursor': 'c0',
                'serverBootId': 'test',
                'degraded': degraded,
                'pushWithheld': push_withheld,
            }

        def cursor_for_sequence(self, *_args):
            return 'c0'

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant():
        context = local_admin_context()
        context.user_id = request.headers.get('X-Test-User', '1')
        g.auth_ctx = context
        g.rate_decision = None

    service = WedgedService()
    monkeypatch.setattr(sync_routes, '_service', service)
    monkeypatch.setattr(
        sync_routes, 'push_withheld_for_conv', lambda _conversation_id: None)
    app.register_blueprint(sync_routes.conversation_sync_v3_bp)

    async def exercise():
        response = await app.test_client().get(
            '/api/v4/conversations/conv-wedged/events')
        return response.status_code, await response.get_data(as_text=True)

    status, body = asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    assert status == 200
    # The stream rode the wedge out: one degraded heartbeat, then the reset
    # frame — no 503 teardown, no reconnect/snapshot-refetch storm trigger.
    assert '"degraded":true' in body.replace(' ', '')
    assert 'sync.reset_required' in body
    assert service.calls == 3

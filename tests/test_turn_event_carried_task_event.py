"""One frame = one authority transaction (2026-08-20 double-write root fix).

A live turn frame historically persisted TWICE per push: ``event.append`` into
the cold-replay log (storage_events) and ``turn.event.record`` into the turn
authority — two commands, two writer-queue slots, and no atomicity between
them.  One side could commit while the other timed out, and the retry of the
failed half surfaced as "Event sequence has a conflicting payload".  The
frame's storage_events row now rides INSIDE the turn authority transaction
(``record_task_event(..., task_event=...)`` → return ``'carried'``).  Pinned
here: (a) the carried row commits atomically with the projection, (b) a
conflicting carried row rolls the WHOLE frame back — turn projection
included, fail-closed, and (c) a stale attempt still refuses the authority
write and leaves the standalone append to the caller. The carried row uses
the same storage-only projection as standalone appends; the live frame remains
unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lib.identity import PrincipalContext
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


def _running_task(turn_service, storage, conv_id, task_id):
    created = turn_service.create_turn_pair(
        conv_id, command_id=f'{task_id}-pair',
        input_projection={'content': 'q'}, config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'carried', 'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    turn_service.bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id, 'id': task_id, 'status': 'running',
        '_userId': 1,
        '_principalContext': PrincipalContext.user(
            subject_id='test-user:1', owner_user_id=1,
        ).to_payload(),
        'content': 'seed', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    return created, task


def _task_event(task_id, seq, wire):
    return {'task_id': task_id, 'sequence': seq, 'event': wire}


def _stored_event(storage, task_id, seq):
    events = storage.client.query('event.list', {
        'task_id': task_id, 'after_sequence': seq - 1, 'limit': 1,
    })
    return events[0] if events else None


def test_carried_frame_commits_event_row_and_projection_atomically(
        turn_service, storage):
    created, task = _running_task(turn_service, storage, 'carry-conv', 'carry-task')
    task['content'] = 'streamed text'
    wire = {'type': 'delta', 'content': 'streamed text', 'seq': 0}
    outcome = turn_service.record_task_event(
        task, wire, task_event=_task_event('carry-task', 0, wire))
    assert outcome == 'carried'
    # Both halves landed: the turn authority projection …
    turn = turn_service.get_turn(
        'carry-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == 'streamed text'
    # … and the cold-replay log row, byte-identical to the wire frame.
    stored = _stored_event(storage, 'carry-task', 0)
    assert stored is not None
    assert stored['event']['content'] == 'streamed text'


def test_conflicting_carried_row_rolls_back_the_whole_frame(turn_service, storage):
    created, task = _running_task(turn_service, storage, 'skew-conv', 'skew-task')
    # Pre-seed the replay log with a DIFFERENT payload at the same sequence —
    # the historical skew state the merge must now refuse atomically.
    storage.client.command('event.append', {
        'task_id': 'skew-task', 'sequence': 0,
        'event': {'type': 'delta', 'content': 'OLD bytes', 'seq': 0},
    }, 'skew-seed')
    revision_before = turn_service.get_turn(
        'skew-conv', created['turn']['turnId'], user_id=1,
    )['projectionRevision']

    task['content'] = 'NEW bytes'
    wire = {'type': 'delta', 'content': 'NEW bytes', 'seq': 0}
    with pytest.raises(StorageError) as excinfo:
        turn_service.record_task_event(
            task, wire, task_event=_task_event('skew-task', 0, wire))
    assert excinfo.value.code == 'database_conflict'
    # Fail-closed: the turn projection must NOT have advanced — the whole
    # frame rolled back, so authority and replay log stay in lockstep.
    turn = turn_service.get_turn(
        'skew-conv', created['turn']['turnId'], user_id=1)
    assert turn['projectionRevision'] == revision_before
    assert turn['projection'].get('content') != 'NEW bytes'


def test_stale_attempt_refuses_carrier_and_caller_appends_standalone(
        turn_service, storage):
    created, task = _running_task(turn_service, storage, 'stale-conv', 'stale-task')
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    # The attempt is settled now; a late frame must not mutate authority…
    wire = {'type': 'delta', 'content': 'late', 'seq': 1}
    outcome = turn_service.record_task_event(
        task, wire, task_event=_task_event('stale-task', 1, wire))
    assert outcome is False
    # …and the carried row was NOT written either — the caller's standalone
    # fallback owns that write (pre-merge semantics preserved).
    assert _stored_event(storage, 'stale-task', 1) is None


def test_terminal_turn_freezes_trace_and_owner_scoped_browser_receipts(
        turn_service, storage, monkeypatch):
    """Low-level event pruning must not erase what the user experienced."""
    from lib.tasks_pkg.turn_trace import (
        observe_task_trace_event,
        read_persisted_task_trace,
    )
    from lib.turn_lifecycle import LifecycleNotFound

    created, task = _running_task(
        turn_service, storage, 'trace-durable-conv', 'trace-durable-task')
    turn_id = created['turn']['turnId']
    attempt_id = created['attempt']['attemptId']
    phase = {
        'type': 'phase', 'phase': 'stream_stalled',
        'detailKey': 'status.streamStalled',
        'detailArgs': {'reasonKey': 'status.reason.noProgress'},
    }
    observe_task_trace_event(task, phase, observed_at_ms=10_000)
    task['phase'] = {**phase, 'emittedAt': 10_000}
    assert turn_service.record_task_event(
        task, phase,
        task_event=_task_event('trace-durable-task', 0, phase),
    ) == 'carried'
    live_turn = turn_service.get_turn(
        'trace-durable-conv', turn_id, user_id=1)
    assert live_turn['projection']['timingTrace']['running'] is True
    assert live_turn['projection']['timingTrace']['statusHistory'][0][
        'detailKey'] == 'status.streamStalled'

    phase_observation = {
        'observationId': 'paint:phase:1',
        'attemptId': attempt_id,
        'kind': 'phase_painted',
        'clientId': 'page-a',
        'phase': 'stream_stalled',
        'serverEmittedAt': 10_000,
        'receivedAt': 10_125,
        'paintedAt': 10_160,
        'projectionRevision': live_turn['projectionRevision'],
        'visibility': 'visible',
    }
    live_revision = live_turn['projectionRevision']
    turn_service.record_turn_perception(
        'trace-durable-conv', turn_id, attempt_id=attempt_id,
        observation=phase_observation, user_id=1)
    receipt_only_update = turn_service.get_turn(
        'trace-durable-conv', turn_id, user_id=1)
    assert receipt_only_update['projectionRevision'] == live_revision
    assert 'clientObservations' not in (
        receipt_only_update['projection']['timingTrace'])
    receipt_only_trace = read_persisted_task_trace(
        'trace-durable-task', user_id=1)
    assert receipt_only_trace['eventsAvailable'] is True
    assert receipt_only_trace['source'] == 'attempt-receipts'
    assert receipt_only_trace['clientObservations'][0][
        'observationId'] == 'paint:phase:1'
    assert 'summary' not in receipt_only_trace

    terminal = {'type': 'done', 'finishReason': 'stop'}
    observe_task_trace_event(task, terminal, observed_at_ms=12_000)
    task['phase'] = None
    assert turn_service.record_task_event(
        task, terminal,
        task_event=_task_event('trace-durable-task', 1, terminal),
    ) == 'carried'

    turn = turn_service.get_turn(
        'trace-durable-conv', turn_id, user_id=1)
    trace = turn['projection']['timingTrace']
    assert trace['source'] == 'turn-snapshot'
    assert trace['running'] is False
    assert trace['status'] == 'done'
    assert trace['statusHistory'][0]['phase'] == 'stream_stalled'
    assert trace['statusHistory'][0]['attention'] == 'stall'
    assert [item['observationId'] for item in trace['clientObservations']] == [
        'paint:phase:1']

    persisted = read_persisted_task_trace('trace-durable-task', user_id=1)
    assert persisted['taskId'] == 'trace-durable-task'
    assert persisted['eventLogAvailable'] is False
    discovery = storage.client.query('turn.timing_trace.list', {
        'conversation_id': 'trace-durable-conv', 'user_id': 1, 'limit': 10,
    })
    assert discovery == {
        'records': [{
            'attempt_id': attempt_id,
            'task_id': 'trace-durable-task',
            'status': 'completed',
            'turn_id': turn_id,
            'created_at': discovery['records'][0]['created_at'],
            'settled_at': discovery['records'][0]['settled_at'],
        }],
        'has_more': False,
    }
    assert discovery['records'][0]['settled_at'] is not None
    assert storage.client.query('turn.timing_trace.list', {
        'conversation_id': 'trace-durable-conv', 'user_id': 2, 'limit': 10,
    }) == {'records': [], 'has_more': False}
    assert storage.client.query('turn.timing_trace.get', {
        'task_id': 'trace-durable-task', 'user_id': 2,
    }) is None

    observation = {
        'observationId': 'paint:terminal:1',
        'attemptId': attempt_id,
        'kind': 'terminal_painted',
        'clientId': 'page-a',
        'serverEmittedAt': 20_000,
        'receivedAt': 20_125,
        'paintedAt': 20_160,
        'projectionRevision': turn['projectionRevision'],
        'visibility': 'visible',
    }
    first = turn_service.record_turn_perception(
        'trace-durable-conv', turn_id, attempt_id=attempt_id,
        observation=observation, user_id=1)
    replay = turn_service.record_turn_perception(
        'trace-durable-conv', turn_id, attempt_id=attempt_id,
        observation=observation, user_id=1)
    assert replay['conversationRevision'] == first['conversationRevision']
    assert replay['idempotentReplay'] is True

    recorded = turn_service.get_turn(
        'trace-durable-conv', turn_id, user_id=1)
    from lib.conversation_sync.validation import decode
    decode('TurnProjection', recorded['projection'])
    assert recorded['projectionRevision'] == turn['projectionRevision']
    # Post-terminal browser receipts extend the attempt diagnostic lane without
    # rewriting the potentially large settled Turn projection.
    assert [item['observationId'] for item in recorded['projection'][
        'timingTrace']['clientObservations']] == ['paint:phase:1']
    persisted = read_persisted_task_trace('trace-durable-task', user_id=1)
    receipts = persisted['clientObservations']
    assert [item['observationId'] for item in receipts] == [
        'paint:phase:1', 'paint:terminal:1']
    assert receipts[-1]['renderMs'] == 35
    assert receipts[-1]['transportMs'] == 125

    # A later edit/regeneration may replace the Turn's current trace. The old
    # task remains independently inspectable through its permanent attempt row.
    replacement = dict(recorded['projection'])
    replacement.pop('timingTrace')
    turn_service.update_turn_projection(
        'trace-durable-conv', turn_id, projection=replacement,
        expected_projection_revision=recorded['projectionRevision'], user_id=1)
    historical = read_persisted_task_trace('trace-durable-task', user_id=1)
    assert historical['status'] == 'done'
    assert len(historical['clientObservations']) == 2

    # The public diagnostic endpoint must still resolve the attempt when both
    # the hot runtime and the legacy task-result access index are absent.
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    import routes.api_v1.tasks as tasks_mod

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant_trace_owner():
        context = local_admin_context()
        context.owner_user_id = 1
        g.auth_ctx = context
        g.rate_decision = None

    monkeypatch.setattr(tasks_mod, '_registries', lambda: {})
    app.register_blueprint(tasks_mod.api_v1_tasks_bp)

    async def _read_trace_endpoint():
        response = await app.test_client().get(
            '/api/v1/tasks/trace-durable-task/trace')
        return response.status_code, await response.get_json()

    status_code, trace_body = asyncio.run(_read_trace_endpoint())
    assert status_code == 200
    assert trace_body['taskId'] == 'trace-durable-task'
    assert trace_body['source'] == 'turn-snapshot'
    assert len(trace_body['clientObservations']) == 2

    with pytest.raises(LifecycleNotFound):
        turn_service.record_turn_perception(
            'trace-durable-conv', turn_id, attempt_id=attempt_id,
            observation=observation, user_id=2)


def test_manager_carrier_delta_projects_snapshots_but_live_frame_stays_full(
        turn_service, storage):
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.request_inspector import _read_events_uncached
    from lib.tasks_pkg.snapshot_delta import forget_projector_task

    task_id = 'snapshot-carried-task'
    conv_id = 'snapshot-carried-conv'
    chat_task_runtime.discard(task_id)
    created, attempt_task = _running_task(
        turn_service, storage, conv_id, task_id)
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    task.update(attempt_task)
    task.update({
        'convId': conv_id,
        '_turnId': created['turn']['turnId'],
        'status': 'running',
    })
    tools = [{
        'type': 'function',
        'function': {'name': 'read_file', 'description': 'x' * 200,
                     'parameters': {'type': 'object'}},
    }]
    first_messages = [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'question'},
    ]
    second_messages = [
        *first_messages,
        {'role': 'assistant', 'content': 'calling'},
        {'role': 'tool', 'content': 'result', 'tool_call_id': 'call-1'},
    ]
    first = {
        'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 1,
        'messages': first_messages, 'tools': tools,
    }
    second = {
        'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 2,
        'messages': second_messages, 'tools': tools,
    }
    post_tool = dict(second, kind='state')
    try:
        manager.append_event(task, first)
        manager.append_event(task, post_tool)
        manager.append_event(task, second)

        stored_first = _stored_event(storage, task_id, 0)['event']
        stored_post_tool = _stored_event(storage, task_id, 1)['event']
        stored_second = _stored_event(storage, task_id, 2)['event']
        assert stored_first['snapshotDeltaVersion'] == 2
        assert stored_first['prefixLen'] == 0
        assert stored_first['tools'] == tools
        assert stored_post_tool['prefixLen'] == len(first_messages)
        assert stored_post_tool['newMessages'] == second_messages[2:]
        # Request R2 is identical to the preceding state mirror. V2 shares a
        # chronological (task, turn) baseline instead of storing this tail a
        # second time in the request-kind chain.
        assert stored_second['prefixLen'] == len(second_messages)
        assert 'newMessages' not in stored_second
        assert stored_second['toolsHash'] == stored_first['toolsHash']
        assert 'messages' not in stored_second
        assert 'tools' not in stored_second

        # TaskRuntime / WebSocket consumers still see the original full frame.
        live_second = task['events'][-1]
        assert live_second['messages'] == second_messages
        assert live_second['tools'] == tools

        # Request Inspector is the cold consumer of structural snapshots; it
        # rebuilds the storage delta to the same complete payload.
        cold_rows, authority_ok = _read_events_uncached(task_id, rebuild=True)
        assert authority_ok is True
        rebuilt = [
            row['payload'] for row in cold_rows
            if row.get('type') == 'messages_snapshot'
        ]
        assert rebuilt[-1]['messages'] == second_messages
        assert rebuilt[-1]['tools'] == tools
        assert not rebuilt[-1].get('degraded')
    finally:
        chat_task_runtime.discard(task_id)
        forget_projector_task(task_id)


@pytest.mark.parametrize('event_type', [
    'round_committed',
    'preference_learned',
])
def test_manager_post_settlement_observer_uses_standalone_replay_only(
        turn_service, storage, monkeypatch, event_type):
    """Sanctioned terminal observers must not re-enter Turn authority.

    Commit-round and preference consolidation both run after the attempt's
    terminal transaction.  Their dedicated settled-Turn CAS owns projection
    enrichment; this event is only a live/cold task-replay notification.
    Routing it through ``turn.event.record`` plants a false zombie-abort on a
    successfully completed task and pays for an authority call guaranteed to
    fail.
    """
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    task_id = f'observer-{event_type}'
    conv_id = f'observer-conv-{event_type}'
    chat_task_runtime.discard(task_id)
    created, attempt_task = _running_task(
        turn_service, storage, conv_id, task_id)
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    task.update(attempt_task)
    task.update({
        'convId': conv_id,
        '_turnId': created['turn']['turnId'],
        'status': 'done',
    })
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    settled_before = turn_service.get_turn(
        conv_id, created['turn']['turnId'], user_id=1)

    authority_calls = []
    real_record_task_event = turn_service.record_task_event

    def record_task_event_spy(*args, **kwargs):
        authority_calls.append(args[1].get('type'))
        return real_record_task_event(*args, **kwargs)

    monkeypatch.setattr(
        turn_service, 'record_task_event', record_task_event_spy)
    try:
        manager.append_event(task, {
            'type': event_type,
            'snapshotId': 'snapshot-1',
            'summary': 'learned preference',
        })

        assert authority_calls == []
        assert not task.get('aborted')
        stored = _stored_event(storage, task_id, 0)
        assert stored is not None
        assert stored['event']['type'] == event_type
        settled_after = turn_service.get_turn(
            conv_id, created['turn']['turnId'], user_id=1)
        assert (settled_after['projectionRevision']
                == settled_before['projectionRevision'])
    finally:
        chat_task_runtime.discard(task_id)


def test_manager_late_delta_still_hits_stale_attempt_fence(
        turn_service, storage, monkeypatch):
    """The observer exception must stay exact; ordinary late work is a zombie."""
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    task_id = 'late-delta-control'
    conv_id = 'late-delta-control-conv'
    chat_task_runtime.discard(task_id)
    created, attempt_task = _running_task(
        turn_service, storage, conv_id, task_id)
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    task.update(attempt_task)
    task.update({
        'convId': conv_id,
        '_turnId': created['turn']['turnId'],
        'status': 'done',
    })
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True

    authority_calls = []
    real_record_task_event = turn_service.record_task_event

    def record_task_event_spy(*args, **kwargs):
        authority_calls.append(args[1].get('type'))
        return real_record_task_event(*args, **kwargs)

    monkeypatch.setattr(
        turn_service, 'record_task_event', record_task_event_spy)
    try:
        manager.append_event(task, {'type': 'delta', 'content': 'too late'})
        assert authority_calls == ['delta']
        assert task.get('aborted') is True
        assert task.get('_abort_reason') == 'turn_attempt_stale_fence'
    finally:
        chat_task_runtime.discard(task_id)

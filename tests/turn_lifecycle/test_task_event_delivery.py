"""Real Turn/event transactions remain atomic while root execution advances."""
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.turn_lifecycle.test_turn_event_carried_task_event import (
    _running_task, _stored_event,
)
from lib.tasks_pkg import event_delivery
from lib.tasks_pkg.manager import _events as manager
from lib.tasks_pkg.manager.runtime import chat_task_runtime

pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite', 'tofudb'])
def storage(tmp_path, monkeypatch, request):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    if request.param == 'tofudb':
        from scripts.check_tofudb_engine_drift import resolve_deployment_binary
        monkeypatch.setenv('TOFUDB_BIN', resolve_deployment_binary(Path(__file__).resolve().parents[2]))
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    supervisor = StorageSupervisor(project_root=tmp_path, backend=request.param, startup_timeout=60)
    # Keep imported getter references stable across fixture teardown. Only the
    # installed runtime changes, so lazy consumers follow the next authority.
    runtime = StorageRuntime(supervisor, auto_restart=False)
    install_runtime_for_test(runtime)
    try:
        runtime.start()
        # Preserve exact receipt payloads across incidental cold-start pressure;
        # the injected executor/ACK failures remain outside this fixture seam.
        from lib.storage.retry import call_with_transient_retry
        original_command = supervisor.client.command
        def command(*args, **kwargs):
            return call_with_transient_retry(lambda: original_command(*args, **kwargs),
                label='isolated event delivery fixture', delays=(.05, .1, .2))
        monkeypatch.setattr(supervisor.client, 'command', command)
        yield supervisor
    finally:
        install_runtime_for_test(None)


@pytest.fixture
def turn_service(storage, monkeypatch):
    from lib import turn_lifecycle
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    return turn_lifecycle


@pytest.fixture
def owner(monkeypatch):
    value = event_delivery.EventDeliveryCoordinator(workers=2, capacity=128,
        byte_capacity=16 * 1024 * 1024, retry_attempts=1, idle_timeout=.02)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', value)
    yield value
    assert value.close(3)


def _task(turn_service, storage, name):
    name += '-' + uuid.uuid4().hex[:8]
    created, source = _running_task(turn_service, storage, name + '-conv', name)
    task = chat_task_runtime.create(user_id=1, task_id=name)
    task.update(source)
    task.update(convId=name+'-conv', _turnId=created['turn']['turnId'], _workerStartedAt=time.time())
    return created, task


def _discard(task):
    from lib.agent_core.execution_session import ExecutionPhase
    task['_executionSession'].settle(ExecutionPhase.CANCELLED)
    chat_task_runtime.discard(task['id'])


@pytest.mark.parametrize('storage', ['tofudb'], indirect=True)
def test_large_structural_frame_preserves_projection_event_and_settlement(
        turn_service, storage):
    """Checkpoint assembly may exceed the old 8 MiB patch-only quota."""
    created, task = _task(turn_service, storage, 'large-structural-frame')
    try:
        task['content'] = 'x' * (9 * 1024 * 1024)
        wire = {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 1,
                'messages': [{'role': 'user', 'content': 'Complete request evidence'}],
                'seq': 0}
        assert turn_service.record_task_event(task, wire, task_event={
            'task_id': task['id'], 'sequence': 0, 'event': wire,
        }) == 'carried'
        turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert turn['projection']['content'] == task['content']
        assert turn['projection']['segments'][-1]['text'] == task['content']
        assert not task.get('_turnProjectionOversizeCount')
        assert _stored_event(storage, task['id'], 0)['event']['type'] == wire['type']
        canonical = storage.client.query('turn.events.list', {
            'attempt_id': task['_attemptId'], 'user_id': 1, 'after': 0,
            'limit': 100, 'projection_mode': 'patch',
        })[-1]
        assert canonical['payload']['event']['messages'] == wire['messages']
        changes = storage.client.query('turn.sync.changes', {
            'conversation_id': task['convId'], 'user_id': 1, 'after': 0, 'limit': 100,
        })
        assert changes['events'][-1]['type'] == 'attempt.event'
        assert changes['events'][-1]['payload']['event'] == canonical

        task['status'] = 'done'
        task['finishReason'] = 'stop'
        done = {'type': 'done', 'finishReason': 'stop', 'seq': 1}
        assert turn_service.record_task_event(task, done, task_event={
            'task_id': task['id'], 'sequence': 1, 'event': done,
        }) == 'carried'
        settled = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert settled['status'] == 'completed'
        assert settled['projection']['content'] == task['content']
        assert _stored_event(storage, task['id'], 1)['event']['type'] == 'done'
    finally:
        _discard(task)


# The literal that packages/tofu-db/src/turn.rs::validate_settleable_turn_size
# sends when a non-terminal event would consume the terminal reserve.
_SETTLEABLE_REFUSAL = 'Turn reconstructed state exceeds its settleable snapshot budget'


@pytest.mark.slow
@pytest.mark.parametrize('storage', ['tofudb'], indirect=True)
def test_a_turn_at_its_settleable_ceiling_keeps_evidence_and_settles_failed(
        turn_service, storage, owner, monkeypatch):
    """A near-limit Turn closes as a typed failure instead of wedging its FIFO.

    The native authority refuses a non-terminal event whose reconstructed Turn
    would leave no room for the terminal settlement it reserves. Task 68a575b7
    (a Continue replaying 2269 tool rounds) kept that refused frame as its
    failed FIFO head: the next round start failed as "Earlier task event is
    not durable" and maintenance resent the refused head every minute, while
    the terminal event could never commit behind it.
    """
    from lib.agent_core.events import EventType, build_event
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.orchestrator._post_loop import handle_task_fatal
    refusals = []
    original = storage.client.command

    def command(operation, payload, *args, **kwargs):
        try:
            return original(operation, payload, *args, **kwargs)
        except StorageError as exc:
            refusals.append((operation, exc.code, exc.message))
            raise

    monkeypatch.setattr(storage.client, 'command', command)
    _, task = _task(turn_service, storage, 'near-limit-turn')
    try:
        # Content and its text segment mirror each other: 2 x 28 MB stays below
        # the non-terminal ceiling (67,043,328 bytes minus an 8 MiB reserve).
        for growth in range(4):
            task['content'] += 'x' * 7_000_000
            manager.append_event(task, {'type': 'phase', 'phase': 'tool',
                                        'detail': f'growth {growth}'})
            event_delivery.flush_task_events(task)
        assert refusals == []
        task['content'] += 'y' * 2_000_000
        manager.append_event(task, {'type': 'phase', 'phase': 'tool', 'detail': 'past the reserve'})
        event_delivery.flush_task_events(task)
        assert refusals == [
            ('turn.event.record', 'database_resource_exhausted', _SETTLEABLE_REFUSAL)]
        # The refused frame is durable in the attempt's event log, not a failed head.
        assert _stored_event(storage, task['id'], 4)['event']['detail'] == 'past the reserve'
        assert event_delivery.task_event_backlog(task) == (0, False)
        assert task['aborted'] and task['_abort_reason'] == 'storage_frame_overflow'
        assert task['error']['kind'] == 'storage_fence'
        running = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert running['status'] == 'running'
        assert 'y' not in running['projection']['content'][-10:]
        # The producer stops at its next frame instead of failing as a lost write.
        with pytest.raises(event_delivery.EventDeliveryCancelled) as stopped:
            manager.append_event(task, build_event(EventType.ROUND_START, roundNum=1))
        assert handle_task_fatal(task, stopped.value) is False
        settled = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert settled['status'] == 'failed'
        assert settled['settlement']['cause'] == 'internal_fence_abort'
        assert settled['settlement']['error']['kind'] == 'storage_fence'
        assert turn_service.get_attempt(task['_attemptId'], user_id=1)['status'] == 'failed'
        rows = storage.client.query('event.list', {
            'task_id': task['id'], 'after_sequence': -1, 'limit': 100})
        assert rows[-1]['event']['type'] == 'done'
        assert rows[-1]['event']['error']['kind'] == 'storage_fence'
        # Nothing remains for terminal maintenance to resend.
        assert not task.get('_terminalTurnPending')
        assert event_delivery.task_event_backlog(task) == (0, False)
        assert turn_service.ensure_terminal_task_settled(task) is True
        assert [operation for operation, *_ in refusals] == ['turn.event.record']
    finally:
        _discard(task)


def test_retained_storage_reader_follows_replacement_authority(
        turn_service, storage, tmp_path):
    """A late import must not retain the fixture's stopped supervisor."""
    from lib.storage import StorageRuntime, StorageSupervisor, get_storage_client
    from lib.storage.service import install_runtime_for_test

    replacement = StorageRuntime(StorageSupervisor(
        project_root=tmp_path / 'replacement-authority', backend='sqlite',
        startup_timeout=60), auto_restart=False)
    install_runtime_for_test(replacement)
    try:
        replacement.start()
        storage.stop()
        assert get_storage_client().health()['ready'] is True
    finally:
        install_runtime_for_test(None)


def test_root_events_cross_provider_boundary_without_waiting_and_freeze_projection(
        turn_service, storage, owner, monkeypatch):
    from lib.tasks_pkg.manager import _provider_ingress_guard as guard
    from lib.agent_core import push
    created, task = _task(turn_service, storage, 'delivery-frozen')
    entered, release = threading.Event(), threading.Event()
    original = turn_service.record_task_event
    pushed = []
    def record(task, wire, *args, **kwargs):
        if wire['seq'] == 0:
            entered.set()
            assert release.wait(3)
        return original(task, wire, *args, **kwargs)
    def observe(channel, task_id, wire, **kwargs):
        stored = _stored_event(storage, task_id, wire['seq'])
        assert stored is not None
        turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        pushed.append((wire['seq'], turn['projection']['content']))
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    monkeypatch.setattr(push, 'push_event', observe)
    try:
        token = guard.begin_provider_ingress(task, span_id='dispatch')
        task['content'] = 'first'
        manager.append_event(task, {'type': 'delta', 'content': 'first'})
        assert entered.wait(2)
        guard.end_provider_ingress(task, token=token)
        task['content'] = 'first second'
        manager.append_event(task, {'type': 'phase', 'phase': 'calling'})
        assert owner.snapshot()['pending_events'] == 2
        assert pushed == []
        assert chat_task_runtime.poll(task['id'])['events'] == []
        task['content'] = 'future mutation'
    finally:
        release.set()
    event_delivery.flush_task_events(task)
    assert pushed == [(0, 'first'), (1, 'first second')]
    replay = chat_task_runtime.poll(task['id'])
    assert [e['seq'] for e in replay['events']] == [0, 1]
    _discard(task)


def test_terminal_waits_for_previous_event_and_settles_exact_final_content(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'delivery-terminal')
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = turn_service.record_task_event
    def record(task, wire, *args, **kwargs):
        if wire['type'] == 'delta':
            entered.set()
            assert release.wait(3)
        return original(task, wire, *args, **kwargs)
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    task['content'] = 'answer'
    task['finishReason'] = 'stop'
    manager.append_event(task, {'type': 'delta', 'content': 'answer'})
    assert entered.wait(2)
    def finish():
        task['status'] = 'done'
        manager.append_event(task, {'type': 'done', 'finishReason': 'stop'})
        done.set()
    terminal = threading.Thread(target=finish)
    terminal.start()
    try:
        assert not done.wait(.03)
        assert not chat_task_runtime.poll(task['id'])['done']
    finally:
        release.set()
        terminal.join(3)
    assert done.is_set()
    turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
    assert turn['status'] == 'completed'
    assert turn['projection']['content'] == 'answer'
    assert _stored_event(storage, task['id'], 1)['event']['type'] == 'done'
    assert task['_eventDeliveryLane'].projection.source is None
    _discard(task)


def test_late_executor_is_revoked_without_publishing_or_retaining_zombie_queue(
        turn_service, storage, owner, monkeypatch):
    from lib.agent_core import push
    _, task = _task(turn_service, storage, 'delivery-stale')
    turn_service.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    pushed = []
    monkeypatch.setattr(push, 'push_event', lambda *args, **kwargs: pushed.append(args))
    manager.append_event(task, {'type': 'delta', 'content': 'late'})
    event_delivery.flush_task_events(task)
    assert task['aborted']
    assert task['_eventDeliveryRevoked']
    assert pushed == []
    assert owner.snapshot()['pending_events'] == 0
    assert _stored_event(storage, task['id'], 0) is None
    _discard(task)


def test_failed_terminal_barrier_is_repaired_without_losing_its_carrier(
        turn_service, storage, owner, monkeypatch):
    from lib.tasks_pkg.manager._persist import persist_task_result
    from lib.tasks_pkg import model_replay
    _, task = _task(turn_service, storage, 'delivery-repair')
    entered, release = threading.Event(), threading.Event()
    original = turn_service.record_task_event
    original_flush = event_delivery.flush_task_events
    def record(task, wire, *args, **kwargs):
        if wire['type'] == 'delta':
            entered.set()
            assert release.wait(3)
        return original(task, wire, *args, **kwargs)
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    monkeypatch.setattr(event_delivery, 'flush_task_events',
                        lambda task: original_flush(task, timeout=.01))
    monkeypatch.setattr(model_replay, 'prepare_completed_task', lambda task: None)
    task.update(content='preserved answer', finishReason='stop')
    manager.append_event(task, {'type': 'delta', 'content': 'preserved answer'})
    assert entered.wait(2)
    try:
        task['status'] = 'done'
        manager.append_event(task, {'type': 'done', 'finishReason': 'stop', 'model': 'gpt-4o'})
        assert task['_terminalTurnPending'][1]['sequence'] == 1
        assert task['_terminalPersistencePending']
        assert not chat_task_runtime.poll(task['id'])['done']
        assert _stored_event(storage, task['id'], 1) is None
    finally:
        release.set()
    monkeypatch.setattr(event_delivery, 'flush_task_events', original_flush)
    assert persist_task_result(task) is True
    stored = _stored_event(storage, task['id'], 1)
    assert stored['event']['model'] == 'gpt-4o'
    assert chat_task_runtime.poll(task['id'])['done']
    assert task['_eventVisibilityCursor'] == 2
    assert task['_eventDeliveryReceipt']['visibleCursor'] == 2
    assert '_pushWithheldAt' not in task
    assert not task.get('_terminalPersistencePending')
    _discard(task)


def test_lost_ack_retries_exact_prefix_delta_payload_without_natural_key_conflict(
        turn_service, storage, owner, monkeypatch):
    from copy import deepcopy
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.request_inspector import _read_events_uncached
    _, task = _task(turn_service, storage, 'delivery-ack')
    original = turn_service.record_task_event
    lost_ack = False
    attempted_carriers = []
    def record(task, wire, *args, **kwargs):
        nonlocal lost_ack
        if wire['seq'] == 1:
            attempted_carriers.append(deepcopy(kwargs['task_event']))
        result = original(task, wire, *args, **kwargs)
        if wire['seq'] == 1 and not lost_ack:
            lost_ack = True
            raise StorageError('database_timeout', 'injected lost acknowledgement')
        return result
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'question'}]
    def emit(rows, number):
        manager.append_event(task, dict(type='messages_snapshot', kind='request',
            roundNum=number, model='gpt-4o', label='request', messages=rows, tools=[]))
    emit(messages, 1)
    event_delivery.flush_task_events(task)
    expanded = [*messages, {'role': 'assistant', 'content': 'answer'}]
    emit(expanded, 2)
    # The common persistence wait repairs this transient lost ACK in place.
    # The frozen payload, not a surfaced transient error, is the invariant.
    owner.flush(task['_eventDeliveryLane'], recover=False)
    assert lost_ack and len(attempted_carriers) == 2
    assert attempted_carriers[0] == attempted_carriers[1]
    before = _stored_event(storage, task['id'], 1)['event']
    assert before['prefixLen'] == len(messages)
    event_delivery.flush_task_events(task)
    assert _stored_event(storage, task['id'], 1)['event'] == before
    rows, authority_ok = _read_events_uncached(task['id'], rebuild=True)
    assert authority_ok
    assert [row['payload']['messages'] for row in rows] == [messages, expanded]
    assert owner.snapshot()['pending_events'] == 0
    _discard(task)


def test_live_rejection_keeps_exact_head_for_repair_without_revoking_executor(
        turn_service, storage, owner, monkeypatch):
    _, task = _task(turn_service, storage, 'delivery-live-rejection')
    original = turn_service.record_task_event
    rejecting = True
    def record(*args, **kwargs):
        return False if rejecting else original(*args, **kwargs)
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    manager.append_event(task, {'type': 'phase', 'phase': 'calling'})
    with pytest.raises(event_delivery.EventDeliveryError):
        owner.flush(task['_eventDeliveryLane'], recover=False)
    assert owner.snapshot()['pending_events'] == 1
    assert not task.get('_eventDeliveryRevoked')
    assert not task.get('aborted')
    assert _stored_event(storage, task['id'], 0) is None
    rejecting = False
    event_delivery.flush_task_events(task)
    assert _stored_event(storage, task['id'], 0) is not None
    assert owner.snapshot()['pending_events'] == 0
    _discard(task)

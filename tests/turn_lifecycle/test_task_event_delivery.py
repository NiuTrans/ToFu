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
    from lib.storage import StorageSupervisor
    if request.param == 'tofudb':
        from scripts.check_tofudb_engine_drift import resolve_deployment_binary
        monkeypatch.setenv('TOFUDB_BIN', resolve_deployment_binary(Path(__file__).resolve().parents[2]))
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    supervisor = StorageSupervisor(project_root=tmp_path, backend=request.param, startup_timeout=60)
    supervisor.start()
    # Preserve exact receipt payloads across incidental cold-start pressure;
    # the injected executor/ACK failures remain outside this fixture seam.
    from lib.storage.retry import call_with_transient_retry
    original_command = supervisor.client.command
    def command(*args, **kwargs):
        return call_with_transient_retry(lambda: original_command(*args, **kwargs),
            label='isolated event delivery fixture', delays=(.05, .1, .2))
    monkeypatch.setattr(supervisor.client, 'command', command)
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture
def turn_service(storage, monkeypatch):
    from lib import turn_lifecycle
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: storage.client)
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
    assert task['_eventDeliveryLane'].projection_source is None
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
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.request_inspector import _read_events_uncached
    _, task = _task(turn_service, storage, 'delivery-ack')
    original = turn_service.record_task_event
    lost_ack = False
    def record(task, wire, *args, **kwargs):
        nonlocal lost_ack
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
    with pytest.raises(event_delivery.EventDeliveryError):
        owner.flush(task['_eventDeliveryLane'], recover=False)
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

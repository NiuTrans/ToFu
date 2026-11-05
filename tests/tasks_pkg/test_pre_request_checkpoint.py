"""The pre-request checkpoint never waits for request diagnostics.

Owner decision 2026-10-28: saving request diagnostics must not block a paid
model call. ``checkpoint_model_dispatch`` runs the domain sink (the Swarm /
Evolution write-ahead record) synchronously, then dispatches while the Request
Inspector snapshot and every other accepted event still persist in the task's
ordered FIFO. The next awaited checkpoint (tools_in_flight, ready, terminal)
drains them before its own effect, exactly as before.

Specs: no wait before the request; durable, ordered events afterwards; the
sink write-ahead and a failed lane still block; Stop during the asynchronous
persist; a process lost before the snapshot ACK leaves a durable prefix that
the event-log invariants and the Request Inspector accept.
"""
from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType, build_event
from lib.storage.errors import StorageError
from lib.tasks_pkg import round_prep_timing as rpt
from lib.tasks_pkg.event_delivery import (
    DeliveryLane, EventBacklog, EventDeliveryCancelled, EventDeliveryCoordinator,
    EventDeliveryError, EventDeliveryRevoked, task_event_backlog,
)
from lib.tasks_pkg.persistence_wait import checkpoint_invocation, checkpoint_model_dispatch

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

_MESSAGES = [{'role': 'system', 'content': 'You are a careful engineer.'},
             {'role': 'user', 'content': 'Inspect the repository.'}]


class _Authority:
    """Stub turn authority: commits in call order and can hold one event type."""

    def __init__(self):
        self.committed = []
        self.durable = None
        self.hold_type = None
        self.lose_held = False
        self.entered = threading.Event()
        self.released = threading.Event()

    def hold(self, event_type, *, lose=False):
        self.hold_type, self.lose_held = event_type, lose
        self.entered.clear()
        self.released.clear()

    def _await_held(self, wire):
        if wire.get('type') == self.hold_type:
            self.entered.set()
            assert self.released.wait(10)
            if self.lose_held:
                raise EventDeliveryRevoked('process lost before acknowledgement')

    def _commit(self, carrier):
        for row in [*(carrier.get('preceding_events') or []), carrier]:
            if self.durable is not None:
                self.durable(row)
            self.committed.append((row['sequence'], row['event'].get('type')))

    def record(self, task, wire, **kwargs):
        self._await_held(wire)
        self._commit(kwargs['task_event'])
        return 'carried'

    def record_batch(self, task, entries):
        """A queued run commits in one transaction: every entry, or none."""
        for entry in entries:
            self._await_held(entry.raw_event)
        for entry in entries:
            self._commit(entry.task_event)
        return ['carried'] * len(entries)


@pytest.fixture
def root(monkeypatch):
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)
    owner = EventDeliveryCoordinator(workers=2, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    monkeypatch.setattr('lib.translate.terminal.schedule_terminal_turn_translations',
                        lambda task: None)
    authority = _Authority()
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', authority.record)
    monkeypatch.setattr(turn_lifecycle, 'record_task_events', authority.record_batch)
    task = chat_task_runtime.create(user_id=1, task_id='pre-request-' + uuid.uuid4().hex)
    task.update(_attemptId='pre-request-attempt', _turnId='pre-request-turn',
                convId='pre-request-conv', _workerStartedAt=time.time(), status='running',
                config={}, content='', thinking='', toolRounds=[],
                content_lock=threading.Lock())
    try:
        yield SimpleNamespace(task=task, authority=authority, owner=owner)
    finally:
        authority.released.set()
        assert owner.close(timeout=5)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def _emit_request_snapshot(task, messages, round_num):
    from lib.tasks_pkg.orchestrator._messages_snapshot import emit_messages_snapshot_event
    emit_messages_snapshot_event(
        task, messages, tid=task['id'][:8], round_num=round_num, model='fixture-model',
        thinking_enabled=False, thinking_depth=0, preset='', temperature=1.0,
        max_tokens=512, response_format=None, tools=[], prepared_messages=messages)


def _request_span(round_num, *, complete=False):
    fields = {'spanId': f'model:pre-request:{round_num}', 'model': 'fixture-model',
              'requestTag': f'R{round_num}', 'roundNum': round_num}
    if complete:
        return build_event(EventType.MODEL_REQUEST_COMPLETE, status='succeeded',
                           durationMs=5, **fields)
    return build_event(EventType.MODEL_REQUEST_START, requestLevel='logical', **fields)


def _in_thread(function):
    outcome = SimpleNamespace(done=threading.Event(), value=None, error=None)

    def run():
        try:
            outcome.value = function()
        except BaseException as error:
            outcome.error = error
        finally:
            outcome.done.set()

    threading.Thread(target=run, daemon=True).start()
    return outcome


def test_request_dispatches_while_its_snapshot_is_still_being_persisted(root):
    task, authority = root.task, root.authority
    authority.hold('messages_snapshot')
    rpt.begin_round_prep(task, 0)
    _emit_request_snapshot(task, _MESSAGES, 0)
    assert authority.entered.wait(2)

    # The snapshot write is held indefinitely: returning at all proves the
    # paid request is not queued behind request diagnostics.
    dispatch = _in_thread(lambda: checkpoint_model_dispatch(task, _MESSAGES, 0))
    assert dispatch.done.wait(5), 'the pre-request checkpoint waited for diagnostics'
    assert dispatch.error is None and dispatch.value == 0
    assert task['_invocationCheckpointPhase'] == 'model_in_flight'
    assert authority.committed == []
    assert task_event_backlog(task) == EventBacklog(1, False)

    record = rpt.finish_round_prep(task)
    assert record['barrierPendingEvents'] == 1
    assert record['barrierAwaitedEvents'] == 0
    assert 'barrierMs' not in record
    assert 'snapshot' in record['build_bodyMs']

    authority.released.set()
    checkpoint_invocation(task, _MESSAGES, 0, phase='tools_in_flight')
    assert [kind for _, kind in authority.committed] == ['messages_snapshot']


def test_deferred_evidence_stays_ordered_and_the_tool_barrier_drains_it(root):
    task, authority = root.task, root.authority
    from lib.tasks_pkg.manager import append_event
    authority.hold('messages_snapshot')
    _emit_request_snapshot(task, _MESSAGES, 0)
    assert authority.entered.wait(2)
    assert checkpoint_model_dispatch(task, _MESSAGES, 0) == 0

    append_event(task, _request_span(1))
    task['content'] = 'partial'
    append_event(task, {'type': 'delta', 'content': 'partial'})
    append_event(task, _request_span(1, complete=True))
    # Tool intent remains an awaited barrier: no external effect may start
    # before the request evidence and the stream receipts are durable.
    barrier = _in_thread(lambda: checkpoint_invocation(task, _MESSAGES, 0,
                                                       phase='tools_in_flight'))
    assert not barrier.done.wait(0.2)
    authority.released.set()
    assert barrier.done.wait(5) and barrier.error is None
    assert barrier.value == 4

    kinds = [kind for _, kind in authority.committed]
    assert kinds == ['messages_snapshot', 'model_request_start', 'delta',
                     'model_request_complete']
    sequences = [sequence for sequence, _ in authority.committed]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert task_event_backlog(task) == EventBacklog(0, False)


class _Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000.0


def test_domain_sink_write_ahead_still_acknowledges_before_dispatch(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(rpt, 'time', clock)
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)
    task = {'id': 'write-ahead-task', 'convId': 'write-ahead-conv'}
    phases = []

    def sink(carrier, messages, round_num):
        phases.append((carrier['_invocationCheckpointPhase'], round_num))
        clock.now += 0.3
        if len(phases) < 3:
            raise StorageError('database_busy', 'checkpoint pressure')

    task['_invocationCheckpointSink'] = sink
    rpt.begin_round_prep(task, 4)
    assert checkpoint_model_dispatch(task, _MESSAGES, 4) == 0
    # The marker that a paid request may be in flight is durable first;
    # transient pressure retries that exact write, never the request.
    assert phases == [('model_in_flight', 4)] * 3
    record = rpt.finish_round_prep(task)
    assert record['stepsMs']['barrier'] == pytest.approx(900.0)
    assert record['barrierMs'] == {'sink': pytest.approx(900.0)}
    assert record['barrierAwaitedEvents'] == record['barrierPendingEvents'] == 0

    def refuse(carrier, messages, round_num):
        raise StorageError('database_integrity', 'checkpoint refused')

    task['_invocationCheckpointSink'] = refuse
    with pytest.raises(StorageError):
        checkpoint_model_dispatch(task, _MESSAGES, 5)


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)
    owner = EventDeliveryCoordinator(workers=2, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr('lib.tasks_pkg.event_delivery.coordinator', lambda: owner)
    yield owner
    assert owner.close(timeout=5)


def _failed_head(owner, task, persist):
    lane = DeliveryLane(task)
    task['_eventDeliveryLane'] = lane
    owner.submit(lane, 0, 128, persist, lambda: None)
    with owner.condition:
        assert owner.condition.wait_for(lambda: lane.error is not None, timeout=2)
    return lane


def test_a_lane_with_a_failed_head_repairs_or_refuses_before_dispatch(delivery):
    attempts = []

    def refused_once():
        attempts.append(1)
        if len(attempts) == 1:
            raise StorageError('database_integrity', 'refused once')

    task = {'id': 'failed-head', 'convId': 'failed-head-conv'}
    lane = _failed_head(delivery, task, refused_once)
    assert task_event_backlog(task) == EventBacklog(1, True)
    assert checkpoint_model_dispatch(task, _MESSAGES, 0) == 1
    assert len(attempts) == 2 and lane.completed == lane.accepted == 1

    def always_refused():
        raise StorageError('database_integrity', 'still refused')

    refused = {'id': 'refused-head', 'convId': 'refused-head-conv'}
    lane = _failed_head(delivery, refused, always_refused)
    with pytest.raises(EventDeliveryError):
        checkpoint_model_dispatch(refused, _MESSAGES, 0)
    with delivery.condition:
        lane.pending[0].persist = lambda: None
    delivery.flush(lane)


def test_stop_during_the_async_persist_keeps_evidence_and_orders_the_terminal(root):
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    task, authority = root.task, root.authority
    authority.hold('messages_snapshot')
    _emit_request_snapshot(task, _MESSAGES, 0)
    assert authority.entered.wait(2)
    assert checkpoint_model_dispatch(task, _MESSAGES, 0) == 0

    assert chat_task_runtime.abort_owned(task['id'], user_id=1, source='test-stop')
    with pytest.raises(EventDeliveryCancelled):
        append_event(task, {'type': 'delta', 'content': 'late'})
    terminal = _in_thread(lambda: append_event(task, {'type': 'done', 'finishReason': 'aborted'}))
    # Stop halts execution, not persistence of already-accepted evidence: the
    # terminal frame commits only after the snapshot that preceded it.
    assert not terminal.done.wait(0.2)
    authority.released.set()
    assert terminal.done.wait(5) and terminal.error is None
    assert [kind for _, kind in authority.committed] == ['messages_snapshot', 'done']
    assert task_event_backlog(task) == EventBacklog(0, False)


def test_process_lost_before_snapshot_ack_leaves_a_prefix_both_readers_accept(
        root, chat_sidecar):
    from lib.storage import get_storage_client
    from lib.tasks_pkg import request_inspector
    from lib.tasks_pkg.event_invariants import run_task_log_invariants
    from lib.tasks_pkg.manager import append_event
    task, authority = root.task, root.authority
    client = get_storage_client(write=True)
    # The carried event row is exactly what turn.event.record commits.
    authority.durable = lambda row: client.command('event.append', {
        'task_id': row['task_id'], 'sequence': row['sequence'], 'event': row['event']}, None)

    _emit_request_snapshot(task, _MESSAGES, 0)
    checkpoint_model_dispatch(task, _MESSAGES, 0)
    append_event(task, _request_span(1))
    append_event(task, _request_span(1, complete=True))
    checkpoint_invocation(task, _MESSAGES, 1, phase='ready')

    second = [*_MESSAGES, {'role': 'assistant', 'content': 'Reading the tree.'},
              {'role': 'user', 'content': 'Continue.'}]
    authority.hold('messages_snapshot', lose=True)
    _emit_request_snapshot(task, second, 1)
    assert authority.entered.wait(2)
    assert checkpoint_model_dispatch(task, second, 1) == 0
    append_event(task, _request_span(2))
    authority.released.set()
    deadline = time.monotonic() + 5
    while not task.get('_eventDeliveryRevoked') and time.monotonic() < deadline:
        time.sleep(0.01)
    assert task.get('_eventDeliveryRevoked')

    # Only a FIFO prefix was durable: round 2 lost its snapshot and therefore
    # every later frame, including its model_request_start.
    assert [kind for _, kind in authority.committed] == [
        'messages_snapshot', 'model_request_start', 'model_request_complete']
    assert run_task_log_invariants(task['id'], allow_open_tail=True) == ()
    request_inspector.invalidate_task_cache(task['id'])
    folded = request_inspector.fold_request_log(task['id'])
    assert [row['roundNum'] for row in folded['requests']] == [1]
    assert folded['coverage'] == 'full' and 'readError' not in folded
    assert request_inspector.get_request_payload(task['id'], 2) is None
    payload = request_inspector.get_request_payload(task['id'], 1)
    assert [message['content'] for message in payload['messages']] == [
        message['content'] for message in _MESSAGES]

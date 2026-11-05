"""A child's Request Inspector evidence never blocks its paid model call.

Owner decision 2026-10-28 (request diagnostics must not block a paid model
call), child half. A child's only durable events are its Inspector snapshots
under ``<parent>#agent:<child>``. ``deliver_invocation_event`` accepts each one
into the child's ordered evidence lane on the shared event-delivery executor and
hands the event to the required sink at once. The pre-request and ready
boundaries never await that lane; tool intent drains it and the terminal settle
drains it before the child's result is used.

Specs: no wait before the request or the next request; one durable order; a
failed head repairs or refuses the next paid call exactly like the root lane;
Stop never cancels accepted evidence; a process lost before an ACK leaves a
durable prefix that the Request Inspector answers as not found.
"""
from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg import invocation
from lib.tasks_pkg import round_prep_timing as rpt
from lib.tasks_pkg.event_delivery import (
    EventBacklog, EventDeliveryCoordinator, EventDeliveryError, EventDeliveryRevoked,
    task_event_backlog,
)
from lib.tasks_pkg.persistence_wait import checkpoint_invocation, checkpoint_model_dispatch

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

_MESSAGES = [{'role': 'system', 'content': 'You are a careful engineer.'},
             {'role': 'user', 'content': 'Inspect the repository.'}]
_SECOND = [*_MESSAGES, {'role': 'assistant', 'content': 'Reading the tree.'},
           {'role': 'user', 'content': 'Continue.'}]


class _Evidence:
    """Stub Inspector append: commits in call order, can hold or refuse once."""

    def __init__(self):
        self.committed = []
        self.attempts = []
        self.durable = None
        self.hold_kind = None
        self.lose_held = False
        self.refusals = 0
        self.entered = threading.Event()
        self.released = threading.Event()

    def hold(self, kind, *, lose=False):
        self.hold_kind, self.lose_held = kind, lose
        self.entered.clear()
        self.released.clear()

    def append(self, task_id, sequence, event):
        kind = event.get('kind')
        self.attempts.append((task_id, sequence, kind))
        if kind == self.hold_kind:
            self.hold_kind = None
            self.entered.set()
            assert self.released.wait(10)
            if self.lose_held:
                raise EventDeliveryRevoked('process lost before acknowledgement')
        if self.refusals:
            self.refusals -= 1
            raise StorageError('database_integrity', 'evidence refused')
        if self.durable is not None:
            self.durable(task_id, sequence, event)
        self.committed.append((sequence, kind))
        return {'inserted': True}


@pytest.fixture
def lane(monkeypatch):
    from lib.tasks_pkg import event_delivery, event_log
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.snapshot_delta import forget_projector_task

    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)
    owner = EventDeliveryCoordinator(workers=2, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    authority = _Evidence()
    original_append = event_log.append_persistent_event
    monkeypatch.setattr(event_log, 'append_persistent_event', authority.append)
    received, stop = [], threading.Event()
    parent_id = 'child-lane-parent-' + uuid.uuid4().hex
    child = create_child_invocation(
        {'id': parent_id, 'convId': 'child-lane-conv', '_userId': 71,
         '_transientRuntime': True, 'config': {'memoryEnabled': False}},
        model='fixture-model', tools=[], objective='Inspect the repository',
        event_sink=received.append, abort_check=stop.is_set)
    # A storage-backed parent: the child keeps durable Inspector evidence.
    child['_invocationStorageFree'] = False
    child.update(content='', thinking='')
    try:
        yield SimpleNamespace(child=child, authority=authority, owner=owner, received=received,
                              stop=stop, parent_id=parent_id, original_append=original_append)
    finally:
        authority.hold_kind = None
        authority.refusals = 0
        authority.released.set()
        invocation.settle_invocation_evidence(child)
        assert owner.close(timeout=5)
        forget_projector_task(child['_inspectorTaskId'])
        discard_task(child['id'])


def _emit_request_snapshot(task, messages, round_num):
    from lib.tasks_pkg.orchestrator._messages_snapshot import emit_messages_snapshot_event
    emit_messages_snapshot_event(
        task, messages, tid=task['id'][:8], round_num=round_num, model='fixture-model',
        thinking_enabled=False, thinking_depth=0, preset='', temperature=1.0,
        max_tokens=512, response_format=None, tools=[], prepared_messages=messages)


def _emit_state_snapshot(task, messages):
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.round_evidence import build_round_evidence
    append_event(task, build_round_evidence(task, messages, ref=task['_inspectorRef'],
                                            kind='state', model='fixture-model'))


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


def _wait_for_failed_head(owner, child):
    with owner.condition:
        assert owner.condition.wait_for(lambda: task_event_backlog(child).failed, timeout=2)


def test_child_request_dispatches_while_its_snapshot_is_still_being_persisted(lane):
    child, authority = lane.child, lane.authority
    authority.hold('request')
    rpt.begin_round_prep(child, 0)
    emitted = _in_thread(lambda: _emit_request_snapshot(child, _MESSAGES, 0))
    assert authority.entered.wait(2)

    # The append is held indefinitely: returning at all proves neither the
    # emitter nor the pre-request boundary waits for request diagnostics.
    assert emitted.done.wait(5), 'the child snapshot emitter waited for its append'
    dispatch = _in_thread(lambda: checkpoint_model_dispatch(child, _MESSAGES, 0))
    assert dispatch.done.wait(5), 'the child pre-request checkpoint waited for diagnostics'
    assert dispatch.error is None and dispatch.value == 0
    assert child['_invocationCheckpointPhase'] == 'model_in_flight'
    assert authority.committed == []
    assert task_event_backlog(child) == EventBacklog(1, False)
    # The required sink already holds the snapshot; its handoff never waits.
    assert [event['type'] for event in lane.received
            if event['type'] == 'messages_snapshot'] == ['messages_snapshot']

    record = rpt.finish_round_prep(child)
    assert record['barrierPendingEvents'] == 1
    assert record['barrierAwaitedEvents'] == 0
    assert 'snapshot' in record['build_bodyMs']

    authority.released.set()
    assert checkpoint_invocation(child, _MESSAGES, 0, phase='tools_in_flight') == 1
    assert [kind for _, kind in authority.committed] == ['request']
    assert authority.attempts[0][0] == child['_inspectorTaskId']


def test_child_ready_and_next_request_never_await_post_tool_state(lane):
    child, authority = lane.child, lane.authority
    _emit_request_snapshot(child, _MESSAGES, 0)
    assert checkpoint_model_dispatch(child, _MESSAGES, 0) == 0
    checkpoint_invocation(child, _MESSAGES, 0, phase='tools_in_flight')
    assert [kind for _, kind in authority.committed] == ['request']

    authority.hold('state')
    state = _in_thread(lambda: _emit_state_snapshot(child, _SECOND))
    assert authority.entered.wait(2)
    assert state.done.wait(5), 'the post-tool state emitter waited for its append'
    # Ready receipts belong to the domain sink; a child's lane holds only
    # diagnostics, so the boundary before the next paid request never awaits it.
    ready = _in_thread(lambda: checkpoint_invocation(child, _SECOND, 1, phase='ready'))
    assert ready.done.wait(5), 'the ready boundary waited for request diagnostics'
    assert ready.error is None and ready.value == 0
    _emit_request_snapshot(child, _SECOND, 1)
    assert checkpoint_model_dispatch(child, _SECOND, 1) == 0
    assert task_event_backlog(child) == EventBacklog(2, False)

    # Tool intent still drains accepted evidence before the round's effects.
    tools = _in_thread(lambda: checkpoint_invocation(child, _SECOND, 1, phase='tools_in_flight'))
    assert not tools.done.wait(0.2)
    authority.released.set()
    assert tools.done.wait(5) and tools.error is None and tools.value == 2
    assert [kind for _, kind in authority.committed] == ['request', 'state', 'request']
    sequences = [sequence for sequence, _ in authority.committed]
    assert sequences == sorted(sequences) and len(set(sequences)) == 3
    delivered = [event['seq'] for event in lane.received if event['type'] == 'messages_snapshot']
    assert delivered == sequences


def test_a_failed_child_evidence_head_repairs_or_refuses_before_dispatch(lane):
    child, authority, owner = lane.child, lane.authority, lane.owner
    authority.refusals = 1
    _emit_request_snapshot(child, _MESSAGES, 0)
    _wait_for_failed_head(owner, child)
    assert task_event_backlog(child) == EventBacklog(1, True)
    assert not child.get('_invocationDeliveryError')
    # Repair is one more attempt of the exact head, then the call dispatches.
    assert checkpoint_model_dispatch(child, _MESSAGES, 0) == 1
    assert len(authority.attempts) == 2 and len(set(authority.attempts)) == 1
    assert [kind for _, kind in authority.committed] == ['request']

    authority.refusals = float('inf')
    _emit_request_snapshot(child, _SECOND, 1)
    _wait_for_failed_head(owner, child)
    with pytest.raises(EventDeliveryError):
        checkpoint_model_dispatch(child, _SECOND, 1)
    # The terminal settle retries once more, records the authority verdict as
    # the invocation's delivery failure and releases the unrepairable lane.
    invocation.settle_invocation_evidence(child)
    failure = child['_invocationDeliveryError']
    assert isinstance(failure, StorageError) and failure.code == 'database_integrity'
    assert child['aborted'] and child['abort_event'].is_set()
    assert '_invocationEvidenceLane' not in child
    metrics = owner.snapshot()
    assert metrics['failed_lanes'] == 0 and metrics['pending_events'] == 0
    assert [kind for _, kind in authority.committed] == ['request']


def test_stop_never_cancels_accepted_child_evidence(lane):
    from lib.llm.llm_errors import AbortedError
    child, authority = lane.child, lane.authority
    authority.hold('request')
    emitted = _in_thread(lambda: _emit_request_snapshot(child, _MESSAGES, 0))
    assert authority.entered.wait(2) and emitted.done.wait(5)
    lane.stop.set()
    assert invocation.invocation_abort_requested(child)

    # A cancelled round's post-tool state is still accepted, never refused.
    state = _in_thread(lambda: _emit_state_snapshot(child, _SECOND))
    assert state.done.wait(5) and state.error is None
    assert not child.get('_invocationDeliveryError')
    with pytest.raises(AbortedError):
        checkpoint_invocation(child, _SECOND, 1, phase='tools_in_flight')

    authority.released.set()
    invocation.settle_invocation_evidence(child)
    assert [kind for _, kind in authority.committed] == ['request', 'state']
    assert not child.get('_invocationDeliveryError')


def test_process_lost_before_a_child_snapshot_ack_leaves_a_prefix_the_inspector_reads(
        lane, chat_sidecar):
    from lib.tasks_pkg import request_inspector
    child, authority = lane.child, lane.authority
    authority.durable = lane.original_append
    inspector_task_id = child['_inspectorTaskId']

    _emit_request_snapshot(child, _MESSAGES, 0)
    checkpoint_model_dispatch(child, _MESSAGES, 0)
    checkpoint_invocation(child, _MESSAGES, 0, phase='tools_in_flight')

    authority.hold('request', lose=True)
    _emit_request_snapshot(child, _SECOND, 1)
    assert authority.entered.wait(2)
    assert checkpoint_model_dispatch(child, _SECOND, 1) == 0
    authority.released.set()
    deadline = time.monotonic() + 5
    while not child.get('_eventDeliveryRevoked') and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child.get('_eventDeliveryRevoked')

    # Only the FIFO prefix was durable: round 2 lost its snapshot. Readers
    # report it as absent, never as a read failure, and recovery needs none.
    assert [kind for _, kind in authority.committed] == ['request']
    request_inspector.invalidate_task_cache(inspector_task_id)
    folded = request_inspector.fold_request_log(inspector_task_id)
    assert [row['roundNum'] for row in folded['requests']] == [1]
    assert folded['coverage'] == 'full' and 'readError' not in folded
    assert request_inspector.get_request_payload(inspector_task_id, 2) is None
    payload = request_inspector.get_request_payload(inspector_task_id, 1)
    assert [message['content'] for message in payload['messages']] == [
        message['content'] for message in _MESSAGES]
    assert request_inspector.inspector_child_belongs_to_parent(inspector_task_id, lane.parent_id)

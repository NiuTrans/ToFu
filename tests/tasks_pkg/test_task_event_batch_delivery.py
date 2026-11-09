"""A queued run of structural events persists through one storage call.

When a pump takes a lane whose head has not started, it claims the head and
following unstarted items of the same batch scope within the existing RPC
row bound, never waiting for later arrivals, and freezes them. Their entries
persist through the head's
``persist_batch`` in one call; every frame publishes in FIFO order after that
call returns, and failure or a lost acknowledgement retries exactly the frozen
run. Items without a batch port end runs, and a run of one persists through
its own port. Barrier-class frames never queue, and the batch call refuses
exactly them.
"""
import threading
import time
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg.event_delivery import (
    DeliveryLane, EventDeliveryCancelled, EventDeliveryCoordinator, EventDeliveryError,
    EventDeliveryRevoked, ProgressFrame,
)

pytestmark = pytest.mark.unit

SCOPE = 'turn.event.record_batch'


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.001)
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._MAX_DELAY_SECONDS', 0.01)
    owner = EventDeliveryCoordinator(workers=2, retry_delay=0, idle_timeout=.02)
    yield owner
    assert owner.close(timeout=2)


class Storage:
    """One sink for single and batch storage calls and their publications.

    ``single`` and ``batch`` are the persistence ports; ``entry`` freezes one
    item's batch entry. A publication of a frame whose storage call has not
    returned is recorded as a violation.
    """

    def __init__(self):
        self.calls = []
        self.batches = []
        self.frozen = []
        self.committed = set()
        self.published = []
        self.violations = []
        self.batch_hooks = []

    def single(self, sequence, block=None):
        def persist():
            self.calls.append(('single', sequence))
            if block is not None:
                entered, release = block
                entered.set()
                assert release.wait(3)
            self.committed.add(sequence)
        return persist

    def entry(self, sequence):
        def freeze(frames):
            self.frozen.append(sequence)
            return {'sequence': sequence,
                    'frames': None if frames is None else [frame.sequence for frame in frames]}
        return freeze

    def batch(self, entries):
        self.calls.append(('batch', [entry['sequence'] for entry in entries]))
        self.batches.append(entries)
        if self.batch_hooks:
            self.batch_hooks.pop(0)(entries)
        for entry in entries:
            self.committed.update(entry['frames'] or [entry['sequence']])

    def publish(self, sequence):
        def published():
            if sequence not in self.committed:
                self.violations.append(sequence)
            self.published.append(sequence)
        return published


def _submit(owner, lane, storage, sequence, *, block=None, scope=SCOPE, batch=True, **kwargs):
    port = (dict(batch_scope=scope, persist_batch=storage.batch, batch_entry=storage.entry(sequence))
            if batch else {})
    owner.submit(lane, sequence, 128, storage.single(sequence, block), storage.publish(sequence),
                 **port, **kwargs)


def _blocked_head(owner, lane, storage, **kwargs):
    entered, release = threading.Event(), threading.Event()
    _submit(owner, lane, storage, 0, block=(entered, release), **kwargs)
    assert entered.wait(2)
    return release


def test_a_backlog_behind_a_blocked_head_persists_as_one_run_published_after_commit(coordinator):
    lane = DeliveryLane({'id': 'backlog'})
    storage = Storage()
    release = _blocked_head(coordinator, lane, storage)
    for sequence in range(1, 17):
        _submit(coordinator, lane, storage, sequence)
    assert coordinator.snapshot()['pending_events'] == 17
    costs = []
    # The barrier fixes its target and timing records before the head returns.
    assert coordinator.flush(lane, slow_after=0, on_slow=lambda pending: release.set(),
                             on_release=costs.append) == 17
    assert storage.calls == [('single', 0), ('batch', list(range(1, 17)))]
    assert storage.published == list(range(17)) and storage.violations == []
    assert sorted(storage.frozen) == storage.frozen == list(range(1, 17))
    snapshot = coordinator.snapshot()
    assert snapshot['pending_events'] == snapshot['pending_batches'] == snapshot['pending_bytes'] == 0
    assert snapshot['completed'] == snapshot['committed'] == 17
    assert lane.accepted == lane.completed == 17 and not lane.pending
    # Two storage calls: the head alone, then the whole run once.
    [cost] = costs
    assert cost.persist_calls == 2


@pytest.mark.parametrize('merge_progress', [False, True])
def test_committed_rpc_prefix_is_published_while_a_later_batch_is_blocked(
        coordinator, merge_progress):
    from lib.agent_core.storage_event_policy import resolve_storage_event_budget
    limit = resolve_storage_event_budget().batch_max_events
    lane = DeliveryLane({'id': 'visible-prefix'})
    storage = Storage()
    entered, release = threading.Event(), threading.Event()

    def block(entries):
        entered.set()
        assert release.wait(3)

    storage.batch_hooks = [lambda entries: None, block]
    head = _blocked_head(coordinator, lane, storage)
    identity = object()
    try:
        for sequence in range(1, limit + 3):
            progress = {}
            if merge_progress and 1 < sequence <= limit:
                publish = storage.publish(sequence)
                progress = dict(
                    projection_identity=identity,
                    progress_frame=ProgressFrame(sequence, {'type': 'delta'}, publish),
                    persist_progress=lambda frames: storage.committed.update(
                        frame.sequence for frame in frames),
                    progress_wire_bytes=1, progress_payload_bytes=1)
            _submit(coordinator, lane, storage, sequence, **progress)
        head.set()
        assert entered.wait(2), 'the whole backlog was retained as one publication batch'
        assert storage.published == list(range(limit + 1))
        assert lane.completed == limit + 1
        assert storage.violations == []
    finally:
        head.set()
        release.set()
    coordinator.flush(lane)
    assert storage.published == list(range(limit + 3))


@pytest.mark.parametrize('lost_ack', [False, True])
def test_a_transient_failure_retries_the_exact_frozen_run_and_arrivals_form_the_next(
        coordinator, lost_ack):
    lane = DeliveryLane({'id': 'frozen-run'})
    storage = Storage()
    entered, release = threading.Event(), threading.Event()

    def fail_once(entries):
        entered.set()
        assert release.wait(3)
        if lost_ack:
            for entry in entries:
                storage.committed.add(entry['sequence'])
            raise StorageError('database_timeout', 'injected lost acknowledgement')
        raise StorageError('database_unavailable', 'injected outage')

    storage.batch_hooks.append(fail_once)
    head = _blocked_head(coordinator, lane, storage)
    for sequence in range(1, 5):
        _submit(coordinator, lane, storage, sequence)
    head.set()
    assert entered.wait(2)
    # Arrivals during the frozen run's attempt never join it.
    for sequence in (5, 6):
        _submit(coordinator, lane, storage, sequence)
    release.set()
    coordinator.flush(lane)
    assert storage.calls == [('single', 0), ('batch', [1, 2, 3, 4]), ('batch', [1, 2, 3, 4]),
                             ('batch', [5, 6])]
    first, retried, _ = storage.batches
    assert all(a is b for a, b in zip(first, retried)), 'a retry must persist the same entries'
    assert storage.frozen == [1, 2, 3, 4, 5, 6]
    assert storage.published == list(range(7)) and storage.violations == []
    assert coordinator.snapshot()['retries'] >= 1
    assert coordinator.snapshot()['pending_events'] == 0


def test_a_permanent_failure_keeps_the_run_and_withheld_marker_and_repair_retries_it_first(
        coordinator):
    lane = DeliveryLane({'id': 'permanent-run'})
    storage = Storage()
    entered, release = threading.Event(), threading.Event()
    failure = StorageError('database_integrity', 'injected refusal')

    def refuse(entries):
        entered.set()
        assert release.wait(3)
        raise failure

    storage.batch_hooks.append(refuse)
    head = _blocked_head(coordinator, lane, storage)
    for sequence in range(1, 4):
        _submit(coordinator, lane, storage, sequence)
    head.set()
    assert entered.wait(2)
    for sequence in (4, 5):
        _submit(coordinator, lane, storage, sequence)
    release.set()
    with coordinator.condition:
        assert coordinator.condition.wait_for(lambda: lane.error is failure, timeout=2)
    with pytest.raises(EventDeliveryError):
        coordinator.flush(lane, recover=False)
    assert lane.task['_pushWithheldCount'] == 1 and '_pushWithheldAt' in lane.task
    assert coordinator.snapshot()['pending_events'] == 5
    assert storage.published == [0]
    with pytest.raises(EventDeliveryError):
        _submit(coordinator, lane, storage, 6)
    coordinator.flush(lane)
    assert storage.calls == [('single', 0), ('batch', [1, 2, 3]), ('batch', [1, 2, 3]),
                             ('batch', [4, 5])]
    assert storage.published == list(range(6)) and storage.violations == []
    assert '_pushWithheldAt' not in lane.task and '_pushWithheldCount' not in lane.task
    assert coordinator.snapshot()['pending_bytes'] == 0


def test_items_without_a_batch_port_or_of_another_scope_end_runs(coordinator):
    lane = DeliveryLane({'id': 'boundaries'})
    storage = Storage()
    release = _blocked_head(coordinator, lane, storage)
    _submit(coordinator, lane, storage, 1)
    _submit(coordinator, lane, storage, 2)
    _submit(coordinator, lane, storage, 3, batch=False)
    _submit(coordinator, lane, storage, 4)
    _submit(coordinator, lane, storage, 5)
    _submit(coordinator, lane, storage, 6, scope='event.append_batch')
    _submit(coordinator, lane, storage, 7)
    release.set()
    coordinator.flush(lane)
    assert storage.calls == [('single', 0), ('batch', [1, 2]), ('single', 3), ('batch', [4, 5]),
                             ('single', 6), ('single', 7)]
    assert storage.published == list(range(8)) and storage.violations == []
    assert storage.frozen == [1, 2, 4, 5]


@pytest.mark.parametrize('revoked_by', ['authority', 'owner'])
def test_revocation_releases_every_member_and_never_interrupts_an_entered_run(
        coordinator, revoked_by):
    lane = DeliveryLane({'id': 'revoked-run'})
    storage = Storage()
    entered, release = threading.Event(), threading.Event()

    def revoke(entries):
        entered.set()
        assert release.wait(3)
        if revoked_by == 'authority':
            raise EventDeliveryRevoked('injected stale attempt')
        raise StorageError('database_integrity', 'injected refusal')

    storage.batch_hooks.append(revoke)
    head = _blocked_head(coordinator, lane, storage)
    for sequence in range(1, 4):
        _submit(coordinator, lane, storage, sequence)
    head.set()
    assert entered.wait(2)
    _submit(coordinator, lane, storage, 4)
    assert coordinator.revoke(lane) is False, 'an entered run keeps its members'
    assert coordinator.snapshot()['pending_events'] == 4
    release.set()
    if revoked_by == 'owner':
        # The pump completes the revocation requested during its run: the
        # refused run is released, never retained as a failed head.
        with coordinator.condition:
            assert coordinator.condition.wait_for(lambda: lane.revoked, timeout=2)
        assert lane.error is None
        assert coordinator.revoke(lane) is True, 'a released lane revokes idempotently'
    coordinator.flush(lane)
    snapshot = coordinator.snapshot()
    assert snapshot['pending_events'] == snapshot['pending_batches'] == snapshot['pending_bytes'] == 0
    assert snapshot['revoked_events'] == 4
    assert lane.revoked and lane.completed == lane.accepted == 5
    assert storage.published == [0]
    with pytest.raises(EventDeliveryRevoked):
        _submit(coordinator, lane, storage, 5)


def test_accept_after_stop_still_runs_a_stopped_lane(coordinator):
    stop = threading.Event()
    stop.set()
    lane = DeliveryLane({'id': 'stopped-child', 'aborted': True, 'abort_event': stop})
    storage = Storage()
    with pytest.raises(EventDeliveryCancelled):
        _submit(coordinator, lane, storage, 0)
    release = _blocked_head(coordinator, lane, storage, accept_after_stop=True)
    for sequence in range(1, 4):
        _submit(coordinator, lane, storage, sequence, accept_after_stop=True)
    release.set()
    coordinator.flush(lane)
    assert storage.calls == [('single', 0), ('batch', [1, 2, 3])]
    assert storage.published == [0, 1, 2, 3]


def test_progress_frames_inside_a_run_are_each_published(coordinator):
    lane = DeliveryLane({'id': 'progress-run'})
    storage = Storage()
    identity = object()
    release = _blocked_head(coordinator, lane, storage)
    _submit(coordinator, lane, storage, 1)
    for sequence in (2, 3, 4):
        publish = storage.publish(sequence)
        coordinator.submit(lane, sequence, 128, storage.single(sequence), publish,
                           projection_identity=identity,
                           progress_frame=ProgressFrame(sequence, {'type': 'delta'}, publish),
                           persist_progress=lambda frames: storage.calls.append('progress-port'),
                           progress_wire_bytes=128, progress_payload_bytes=32,
                           batch_scope=SCOPE, persist_batch=storage.batch,
                           batch_entry=storage.entry(sequence))
    _submit(coordinator, lane, storage, 5)
    assert coordinator.snapshot()['merged_progress'] == 2
    release.set()
    coordinator.flush(lane)
    assert storage.calls == [('single', 0), ('batch', [1, 4, 5])]
    [batch] = storage.batches
    assert [entry['frames'] for entry in batch] == [None, [2, 3, 4], None]
    assert storage.published == list(range(6)) and storage.violations == []
    snapshot = coordinator.snapshot()
    assert snapshot['completed'] == snapshot['committed'] == 6
    assert snapshot['pending_events'] == snapshot['pending_bytes'] == 0


@pytest.fixture
def live_attempt(monkeypatch):
    """A running conversation attempt whose frames deliver through a fresh owner."""
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owner = EventDeliveryCoordinator(workers=1, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    task = chat_task_runtime.create(user_id=1, task_id='batch-barrier-' + uuid.uuid4().hex)
    task.update(_attemptId='batch-barrier-attempt', convId='batch-barrier-conv',
                _turnId='batch-barrier-turn', _workerStartedAt=time.time(), status='running',
                config={}, content='', thinking='', toolRounds=[], content_lock=threading.Lock())
    try:
        yield owner, task
    finally:
        assert owner.close(3)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


_KIND_VALUES = {'int': 1, 'number': 1, 'str': 'x', 'bool': False, 'dict': {}, 'list': [],
                'None': None}


def _registered_frame(event_type):
    """The smallest frame that satisfies the registered field contract of *event_type*."""
    from lib.agent_core.events import get_event_spec
    return {'type': event_type, **{
        field.name: _KIND_VALUES[field.kind.split('|')[0].strip()]
        for field in get_event_spec(event_type).schema or () if field.required}}


def _batch_call_refuses(frame):
    """Whether ``record_task_events`` refuses *frame*; a task without an attempt writes nothing."""
    from lib import turn_lifecycle
    try:
        turn_lifecycle.record_task_events({'id': 'batch-eligibility'}, [
            turn_lifecycle.TaskEventBatchEntry(raw_event=frame, sequence=0)])
    except ValueError:
        return True
    return False


def test_the_batch_call_refuses_exactly_the_registered_frames_the_producer_keeps_as_barriers(
        live_attempt, monkeypatch):
    """Every registered frame either drains the lane and keeps its own
    transaction, or queues with a batch port. A queued frame the batch call
    refused would fail its whole run permanently, and a barrier the batch call
    accepted would be committed as an ordinary projection update."""
    from lib import turn_lifecycle
    from lib.agent_core.events import event_types, interaction_types, terminal_types
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    owner, task = live_attempt
    # Terminal frames settle the stream, so they are appended last.
    kinds = sorted(event_types(), key=lambda kind: (kind in terminal_types(), kind))
    refused = {kind: _batch_call_refuses(_registered_frame(kind)) for kind in kinds}
    assert terminal_types() | interaction_types() <= {kind for kind in kinds if refused[kind]}
    ports, recorded = {}, {}
    submit = owner.submit

    def observe_submit(lane, sequence, *args, **kwargs):
        ports[sequence] = kwargs.get('batch_entry') is not None
        return submit(lane, sequence, *args, **kwargs)

    def record(task, wire, **kwargs):
        recorded[wire['seq']] = wire['type']
        return 'carried'

    monkeypatch.setattr(owner, 'submit', observe_submit)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record)
    for kind in kinds:
        _events.append_event(task, _registered_frame(kind))
        event_delivery.flush_task_events(task)
    assert sorted(recorded.values()) == sorted(kinds)
    queued = {kind: ports.get(sequence) for sequence, kind in recorded.items()}
    assert {kind: port is None for kind, port in queued.items()} == refused
    assert all(queued[kind] for kind in kinds if not refused[kind])


def test_an_interaction_frame_drains_the_queued_run_then_keeps_its_own_transaction(
        live_attempt, monkeypatch):
    from lib import turn_lifecycle
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    _, task = live_attempt
    entered, release = threading.Event(), threading.Event()
    calls = []

    def record(task, wire, **kwargs):
        if wire['seq'] == 0:
            entered.set()
            assert release.wait(3)
        calls.append(('single', wire['seq'], wire['type']))
        return 'carried'

    def record_batch(task, entries):
        calls.append(('batch', [entry.sequence for entry in entries]))
        return ['carried'] * len(entries)

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record)
    monkeypatch.setattr(turn_lifecycle, 'record_task_events', record_batch)
    try:
        _events.append_event(task, {'type': 'phase', 'phase': 'calling'})
        assert entered.wait(2)
        _events.append_event(task, {'type': 'phase', 'phase': 'tool'})
        _events.append_event(task, {'type': 'phase', 'phase': 'thinking'})
        # The interaction barrier waits for the frames accepted before it.
        ask = threading.Thread(target=_events.append_event,
                               args=(task, _registered_frame('approval_required')))
        ask.start()
        release.set()
        ask.join(3)
        assert not ask.is_alive()
        event_delivery.flush_task_events(task)
        assert calls == [('single', 0, 'phase'), ('batch', [1, 2]),
                         ('single', 3, 'approval_required')]
    finally:
        release.set()


def test_a_custom_tool_call_is_committed_alone_and_visible_before_its_handler_waits(
        live_attempt, monkeypatch):
    """A client-mode custom tool is answered by a client that reads the request
    from the committed stream and posts its ``callId`` back
    (``POST /api/v4/tasks/{id}/tool_result``). As a registered interaction the
    request drains the frames accepted before it, commits in its own
    transaction, and is visible before the handler starts waiting, so the
    handoff budget never runs against a request no client can see."""
    from lib import turn_lifecycle
    from lib.task_replay.task_replay import task_memory_replay_page
    from lib.tasks_pkg import event_delivery, manager
    from lib.tasks_pkg.manager import _events
    from lib.tools.tool_env import dispose_tool_env, mint_tool_env, resolve_client_tool_result
    _, task = live_attempt
    env = mint_tool_env(tools=[{
        'type': 'function', 'execution': {'mode': 'client'},
        'function': {'name': 'custom__lookup', 'description': 'x',
                     'parameters': {'type': 'object', 'properties': {}}}}])
    task['_tool_env'] = env
    entered, release, published = threading.Event(), threading.Event(), threading.Event()
    calls, visible_at_wait, outcome = [], [], {}

    def record(task, wire, **kwargs):
        if wire['seq'] == 0:
            entered.set()
            assert release.wait(3)
        calls.append(('single', wire['seq'], wire['type']))
        return 'carried'

    def record_batch(task, entries):
        calls.append(('batch', [entry.sequence for entry in entries]))
        return ['carried'] * len(entries)

    def publish(task, event):
        # The handler waits for the answer as soon as publication returns.
        _events.append_event(task, event)
        visible_at_wait.extend(task_memory_replay_page(task, 0).events)
        published.set()

    def call_tool():
        outcome['result'] = env._run_client(
            env.tools[0], task, 'call-1', {'query': 'q'}, 1, {'toolCallId': 'call-1'})

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record)
    monkeypatch.setattr(turn_lifecycle, 'record_task_events', record_batch)
    monkeypatch.setattr(manager, 'append_event', publish)
    tool = threading.Thread(target=call_tool)
    try:
        _events.append_event(task, {'type': 'phase', 'phase': 'calling'})
        assert entered.wait(2)
        _events.append_event(task, {'type': 'phase', 'phase': 'tool'})
        _events.append_event(task, {'type': 'phase', 'phase': 'thinking'})
        tool.start()
        release.set()
        assert published.wait(3)
        requests = [frame for frame in visible_at_wait if frame['type'] == 'custom_tool_call']
        assert [frame['seq'] for frame in requests] == [3]
        [request] = requests
        assert {key: request[key] for key in ('roundNum', 'toolCallId', 'toolName', 'arguments')} == {
            'roundNum': 1, 'toolCallId': 'call-1', 'toolName': 'custom__lookup',
            'arguments': {'query': 'q'}}
        assert resolve_client_tool_result(
            request['callId'], 'answer', task_id=task['id'], user_id=1)
        tool.join(3)
        assert not tool.is_alive()
        assert outcome['result'] == ('answer', False)
        event_delivery.flush_task_events(task)
        assert calls == [('single', 0, 'phase'), ('batch', [1, 2]),
                         ('single', 3, 'custom_tool_call')]
    finally:
        release.set()
        dispose_tool_env(env)
        if tool.is_alive():
            tool.join(3)

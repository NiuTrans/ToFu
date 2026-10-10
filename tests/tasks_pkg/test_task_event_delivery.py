"""Execution/durability separation, FIFO ownership and finite residency."""
from contextvars import ContextVar
import threading
import time

import pytest
from lib.storage.errors import StorageError
from lib.tasks_pkg.event_delivery import DeliveryLane, EventDeliveryCoordinator, EventDeliveryError

pytestmark = pytest.mark.unit


@pytest.fixture
def coordinator():
    owner = EventDeliveryCoordinator(workers=2, capacity=32, byte_capacity=4096,
                                     retry_attempts=2, retry_delay=0, idle_timeout=.02)
    yield owner
    assert owner.close(timeout=2)


def test_blocked_write_does_not_block_next_event_or_other_owner(coordinator):
    blocked = DeliveryLane({'id': 'slow', '_userId': 1})
    independent = DeliveryLane({'id': 'fast', '_userId': 2})
    entered, release, fast = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def persist_first():
        entered.set()
        assert release.wait(3)
        observed.append('commit-1')
    try:
        coordinator.submit(blocked, 1, 128, persist_first, lambda: observed.append('push-1'))
        assert entered.wait(2)
        coordinator.submit(blocked, 2, 128, lambda: observed.append('commit-2'), lambda: observed.append('push-2'))
        coordinator.submit(independent, 1, 128, fast.set, lambda: None)
        assert fast.wait(2)
        assert observed == []
        assert coordinator.snapshot()['pending_events'] >= 2
    finally:
        release.set()
    coordinator.flush(blocked)
    assert observed == ['commit-1', 'push-1', 'commit-2', 'push-2']


def test_failed_head_is_retained_and_recovered_before_followers(coordinator):
    lane = DeliveryLane({'id': 'retry'})
    failed, release = threading.Event(), threading.Event()
    recovered = False
    observed = []
    def first():
        assert release.wait(3)
        if not recovered:
            failed.set()
            raise StorageError('database_timeout', 'injected', retryable=False)
        observed.append(1)
    coordinator.submit(lane, 1, 128, first, lambda: None)
    coordinator.submit(lane, 2, 128, lambda: observed.append(2), lambda: None)
    release.set()
    assert failed.wait(2)
    with pytest.raises(EventDeliveryError):
        coordinator.flush(lane, recover=False)
    assert coordinator.snapshot()['pending_events'] == 2
    assert '_pushWithheldAt' in lane.task
    assert observed == []
    recovered = True
    coordinator.flush(lane)
    assert observed == [1, 2]
    assert '_pushWithheldAt' not in lane.task
    assert coordinator.snapshot()['pending_bytes'] == 0


@pytest.mark.parametrize('code', ['database_timeout', 'database_busy', 'database_unavailable'])
def test_transient_failed_head_keeps_accepting_memory_events_until_durable_repair(coordinator, code):
    lane = DeliveryLane({'id': 'transient-memory'})
    recovered = False
    failure = StorageError(code, 'injected outage')
    observed = []

    def persist_first():
        if not recovered:
            raise failure
        observed.append(('commit', 1))

    coordinator.submit(lane, 1, 128, persist_first, lambda: observed.append(('push', 1)))
    with coordinator.condition:
        assert coordinator.condition.wait_for(lambda: lane.error is failure, timeout=2)
    try:
        for sequence in range(2, 65):
            coordinator.submit(lane, sequence, 128,
                lambda sequence=sequence: observed.append(('commit', sequence)),
                lambda sequence=sequence: observed.append(('push', sequence)))
        assert lane.accepted == 64 and lane.completed == 0
        assert lane.error is failure
        assert coordinator.snapshot()['pending_events'] == 64
        assert observed == []
        with pytest.raises(EventDeliveryError) as caught:
            coordinator.flush(lane, recover=False)
        assert caught.value.__cause__ is failure
    finally:
        recovered = True
        coordinator.flush(lane)
    assert observed == [(action, sequence) for sequence in range(1, 65)
                        for action in ('commit', 'push')]
    assert coordinator.snapshot()['pending_bytes'] == 0


@pytest.mark.parametrize('failure', [StorageError('database_integrity', 'bad checksum'),
                                   RuntimeError('worker failure')])
def test_permanent_failed_head_still_refuses_new_events(coordinator, failure):
    lane = DeliveryLane({'id': 'permanent-memory'})
    recovered = False

    def persist():
        if not recovered:
            raise failure

    coordinator.submit(lane, 1, 128, persist, lambda: None)
    with coordinator.condition:
        assert coordinator.condition.wait_for(lambda: lane.error is failure, timeout=2)
    try:
        with pytest.raises(EventDeliveryError) as caught:
            coordinator.submit(lane, 2, 128, lambda: None, lambda: None)
        assert caught.value.__cause__ is failure
        assert lane.accepted == 1
    finally:
        recovered = True
        coordinator.flush(lane)


def test_progress_after_lost_ack_preserves_the_original_receipt_shape(coordinator):
    from lib.tasks_pkg.event_delivery import ProgressFrame
    lane = DeliveryLane({'id': 'lost-ack-memory'})
    identity = object()
    unavailable = True
    committed, published, attempts = [], [], []
    failure = StorageError('database_unavailable', 'commit acknowledgement lost')

    def persist(frames):
        shape = tuple(frame.sequence for frame in frames)
        attempts.append(shape)
        if shape[0] == 1:
            if shape != (1,):
                raise StorageError('database_integrity', 'replayed receipt payload changed')
            if not committed:
                committed.append(1)
            if unavailable:
                raise failure
        else:
            committed.extend(shape)

    def submit(sequence):
        event = {'type': 'delta', 'content': str(sequence)}
        publish = lambda: published.append(sequence)
        coordinator.submit(lane, sequence, 128, lambda: None, publish,
            projection_identity=identity,
            progress_frame=ProgressFrame(sequence, event, publish),
            persist_progress=persist, progress_wire_bytes=128, progress_payload_bytes=32)

    submit(1)
    with coordinator.condition:
        assert coordinator.condition.wait_for(lambda: lane.error is failure, timeout=2)
    try:
        submit(2)
        submit(3)
        assert committed == [1] and published == []
    finally:
        unavailable = False
        coordinator.flush(lane)
    assert attempts == [(1,), (1,), (1,), (2, 3)]
    assert committed == published == [1, 2, 3]


def test_byte_and_item_hints_do_not_refuse_owned_inflight_or_followers():
    owner = EventDeliveryCoordinator(workers=1, capacity=2, byte_capacity=256)
    lane = DeliveryLane({'id': 'bounded'})
    entered, release = threading.Event(), threading.Event()
    observed = []
    def first():
        entered.set()
        assert release.wait(3)
        observed.append(1)
    try:
        owner.submit(lane, 1, 128, first, lambda: None)
        assert entered.wait(2)
        owner.submit(lane, 2, 128, lambda: observed.append(2), lambda: None)
        owner.submit(lane, 3, 128, lambda: observed.append(3), lambda: None, timeout=.01)
        assert owner.snapshot()['pending_events'] == 3
        assert owner.snapshot()['pending_bytes'] == 384
        assert owner.snapshot()['admission_waits'] == 0
    finally:
        release.set()
        assert owner.close(2)
    assert observed == [1, 2, 3]


def test_durable_barrier_timeout_retains_evidence_and_shutdown_waits(coordinator):
    lane = DeliveryLane({'id': 'barrier'})
    entered, release = threading.Event(), threading.Event()
    def persist():
        entered.set()
        assert release.wait(3)
    try:
        coordinator.submit(lane, 1, 128, persist, lambda: None)
        assert entered.wait(2)
        with pytest.raises(EventDeliveryError, match='barrier timed out'):
            coordinator.flush(lane, timeout=.01)
        assert not coordinator.close(.01)
        assert coordinator.snapshot()['pending_events'] == 1
        with pytest.raises(EventDeliveryError, match='shutting down'):
            coordinator.submit(lane, 2, 128, lambda: None, lambda: None)
    finally:
        release.set()
    coordinator.flush(lane)


def test_context_identity_follows_each_event_and_push_error_does_not_retry_commit(coordinator):
    principal = ContextVar('event-test-principal')
    observed = []
    lane = DeliveryLane({'id': 'context'})
    def push():
        observed.append(('push', principal.get()))
        raise RuntimeError('disconnected')
    token = principal.set('owner-one')
    try:
        coordinator.submit(lane, 1, 128, lambda: observed.append(('commit', principal.get())), push)
    finally:
        principal.reset(token)
    coordinator.flush(lane)
    assert observed == [('commit', 'owner-one'), ('push', 'owner-one')]
    assert coordinator.snapshot()['committed'] == 1


def test_idle_workers_retire_and_later_admission_restarts_them(coordinator):
    lane = DeliveryLane({'id': 'idle'})
    coordinator.submit(lane, 1, 128, lambda: None, lambda: None)
    coordinator.flush(lane)
    with coordinator.condition:
        assert coordinator.condition.wait_for(lambda: not coordinator.workers, timeout=2)
    coordinator.submit(lane, 2, 128, lambda: None, lambda: None)
    coordinator.flush(lane)
    assert coordinator.snapshot()['committed'] == 2


def test_metrics_read_does_not_start_optional_delivery_workers(monkeypatch):
    from lib.tasks_pkg import event_delivery
    monkeypatch.setattr(event_delivery, '_COORDINATOR', None)
    monkeypatch.setattr(event_delivery, 'resolve_storage_event_budget',
                        lambda: pytest.fail('metrics must not resolve/create a lane'))
    assert event_delivery.event_delivery_metrics() == {}


def test_last_delivery_releases_detached_projection_residency(coordinator):
    lane = DeliveryLane({'id': 'projection-release'})
    source = {'toolRounds': [{'result': 'x' * 1024}]}
    coordinator.submit(lane, 1, 2048, lambda: None, lambda: None,
                       projection_source=source, projection_bytes=2048)
    coordinator.flush(lane)
    assert lane.projection_source is None
    assert coordinator.snapshot()['pending_bytes'] == 0


def test_composition_handoff_drains_before_retarget_and_keeps_its_own_port(coordinator, monkeypatch):
    from lib.tasks_pkg import event_delivery
    monkeypatch.setattr(event_delivery, '_COORDINATOR', coordinator)
    task = {'id': 'handoff', '_workerStartedAt': time.time(), '_attemptId': 'root'}
    lane = event_delivery.delivery_lane_for(task)
    entered, release, switched = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def persist():
        entered.set()
        assert release.wait(3)
        observed.append(task['_attemptId'])
    coordinator.submit(lane, 0, 128, persist, lambda: None)
    assert entered.wait(2)
    def handoff():
        event_delivery.detach_task_event_delivery(task)
        task['_attemptId'] = 'flow-phase'
        switched.set()
    worker = threading.Thread(target=handoff)
    worker.start()
    try:
        assert not switched.wait(.02)
    finally:
        release.set()
        worker.join(2)
    assert switched.is_set()
    assert observed == ['root']
    assert event_delivery.delivery_lane_for(task) is None


def test_shared_structure_charged_once_until_last_follower_commits():
    owner = EventDeliveryCoordinator(workers=1, capacity=8, byte_capacity=2304)
    lane = DeliveryLane({'id': 'shared'})
    identity = object()
    first_entered, first_release = threading.Event(), threading.Event()
    last_entered, last_release = threading.Event(), threading.Event()
    observed = []
    def first():
        first_entered.set()
        assert first_release.wait(3)
        observed.append(1)
    def last():
        last_entered.set()
        assert last_release.wait(3)
        observed.append(2)
    try:
        owner.submit(lane, 1, 128, first, lambda: None,
            projection_source={'toolRounds': []}, projection_bytes=2048,
            projection_identity=identity)
        assert first_entered.wait(2)
        owner.submit(lane, 2, 128, last, lambda: None, timeout=.02,
            projection_source={'toolRounds': []}, projection_bytes=2048,
            projection_identity=identity)
        assert owner.snapshot()['pending_bytes'] == 2304
        first_release.set()
        assert last_entered.wait(2)
        assert owner.snapshot()['pending_bytes'] == 2176
    finally:
        first_release.set()
        last_release.set()
        assert owner.close(2)
    assert observed == [1, 2]
    assert owner.snapshot()['pending_bytes'] == 0
    assert not owner.projections


@pytest.mark.parametrize('finish', ['recover', 'cancel', 'shutdown'])
def test_submission_never_waits_for_capacity_and_keeps_owner_gates(finish):
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    owner = EventDeliveryCoordinator(workers=1, capacity=1, byte_capacity=128)
    lane = DeliveryLane({'id': 'pressure', 'abort_event': threading.Event()})
    entered, release, producer_done = threading.Event(), threading.Event(), threading.Event()
    observed, errors = [], []
    def persist():
        entered.set()
        assert release.wait(3)
        observed.append(1)
    def produce():
        try:
            owner.submit(lane, 2, 128, lambda: observed.append(2), lambda: None)
        except Exception as exc:
            errors.append(exc)
        finally:
            producer_done.set()
    owner.submit(lane, 1, 128, persist, lambda: None)
    assert entered.wait(2)
    if finish == 'cancel':
        lane.task['abort_event'].set()
    elif finish == 'shutdown':
        assert not owner.close(.01)
    producer = threading.Thread(target=produce)
    producer.start()
    try:
        assert producer_done.wait(2)
        assert owner.snapshot()['admission_waits'] == 0
        assert owner.snapshot()['pending_bytes'] == (256 if finish == 'recover' else 128)
        if finish == 'cancel':
            assert len(errors) == 1 and isinstance(errors[0], EventDeliveryCancelled)
        elif finish == 'shutdown':
            assert len(errors) == 1 and 'shutting down' in str(errors[0])
        else:
            assert errors == []
    finally:
        release.set()
        producer.join(2)
        assert owner.close(2)
    assert observed == ([1, 2] if finish == 'recover' else [1])
    assert owner.snapshot()['pending_bytes'] == 0


@pytest.mark.parametrize('error', [
    EventDeliveryError('Task event delivery admission timed out; nothing was dropped'),
    EventDeliveryError('Task event delivery admission previously failed'),
])
def test_local_delivery_failure_preserves_original_cause_and_never_switches_model(monkeypatch, error):
    from lib.tasks_pkg.llm_fallback import _call
    from lib.error_envelope import from_exception
    calls = []
    def stream(*args, **kwargs):
        calls.append('stream')
        raise error
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    monkeypatch.setattr(_call, '_get_fallback_model', lambda task: 'fallback-model')
    monkeypatch.setattr(_call, 'append_event', lambda *args, **kwargs:
        pytest.fail('local delivery failure must not emit a fallback phase'))
    monkeypatch.setattr(_call, '_attempt_pool_rescue', lambda *args, **kwargs:
        pytest.fail('local delivery failure must not rotate providers'))
    task = {'id': 'local-delivery', 'convId': 'local-delivery-conv', '_userId': 1,
        'config': {}, 'content': '', 'thinking': '', 'events': [],
        'events_lock': threading.Lock()}
    with pytest.raises(EventDeliveryError) as caught:
        _call._llm_call_with_fallback(task, {'model': 'test-model'}, 'test-model',
            2, 512, True, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])
    assert caught.value is error
    assert calls == ['stream']
    envelope = from_exception(error, context='task-fatal')
    assert envelope['kind'] == 'server_busy'
    assert envelope['retryable']
    assert envelope['raw'] == str(error)
    assert '_fallback_model' not in task


def test_production_progress_events_share_one_charged_detached_history(monkeypatch, record_property):
    import uuid
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owner = EventDeliveryCoordinator(workers=1, capacity=128, byte_capacity=32 * 1024 * 1024)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    entered, release = threading.Event(), threading.Event()
    captured, canonical_frames = [], []
    def persist(task, wire, **kwargs):
        if wire['seq'] == 0:
            entered.set()
            assert release.wait(3)
        source = kwargs['projection_source']
        canonical_frames.extend(kwargs['task_event'].get('preceding_events', []))
        canonical_frames.append({k: kwargs['task_event'][k] for k in ('task_id', 'sequence', 'event')})
        captured.append((wire['seq'], source['content'], source['toolRounds'][0]['result']))
        return 'carried'
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    task = chat_task_runtime.create(user_id=1, task_id='pressure-' + uuid.uuid4().hex)
    task.update(_attemptId='pressure-attempt', convId='pressure-conv', _turnId='pressure-turn',
        _workerStartedAt=time.time(), status='running', config={}, thinking='', content='',
        content_lock=threading.Lock(), toolRounds=[
            {'round': i, 'toolName': 'read_files', 'status': 'done',
             'result': str(i) + 'x' * 10000} for i in range(100)])
    try:
        _events.append_event(task, {'type': 'delta', 'content': '0'})
        assert entered.wait(2)
        lane = event_delivery.delivery_lane_for(task)
        identity = lane.projection_identity
        structure_bytes = lane.projection_bytes
        first_charge = owner.snapshot()['pending_bytes']
        for index in range(1, 64):
            task['content'] = str(index)
            _events.append_event(task, {'type': 'delta', 'content': str(index)})
            assert lane.projection_identity is identity
        charged = owner.snapshot()['pending_bytes']
        assert charged == structure_bytes + sum(item.retained_bytes for item in lane.pending)
        assert charged < structure_bytes * 2
        assert charged < first_charge * 64 / 10
        record_property('charged_bytes', charged)
        record_property('history_bytes', structure_bytes)
        record_property('legacy_charge_bytes', structure_bytes * 64 + sum(item.retained_bytes for item in lane.pending))
        task['toolRounds'][0]['result'] = 'live mutation'
        release.set()
        owner.flush(lane)
        assert [row[0] for row in captured] == [0, 63]
        assert [row[1] for row in captured] == ['', '63']
        assert [row['sequence'] for row in canonical_frames] == list(range(64))
        assert [row['event']['content'] for row in canonical_frames] == list(map(str, range(64)))
        assert owner.snapshot()['merged_progress'] == 62
        assert all(row[2] == '0' + 'x' * 10000 for row in captured)
        assert owner.snapshot()['pending_bytes'] == 0
    finally:
        release.set()
        assert owner.close(3)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def test_production_progress_continues_after_storage_retry_exhaustion(monkeypatch, record_property):
    import uuid
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    owner = EventDeliveryCoordinator(workers=1, retry_attempts=2, retry_delay=0)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    recovered = False
    failure = StorageError('database_busy', 'injected prolonged outage')
    frames, projections, committed_sequences = [], [], []

    def persist(task, wire, **kwargs):
        if not recovered:
            raise failure
        event = kwargs['task_event']
        committed_sequences.append(wire['seq'])
        frames.extend(event.get('preceding_events', []))
        frames.append({key: event[key] for key in ('task_id', 'sequence', 'event')})
        projections.append(kwargs['projection_source']['toolRounds'][0]['result'])
        return 'carried'

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    task = chat_task_runtime.create(user_id=1, task_id='outage-' + uuid.uuid4().hex)
    task.update(_attemptId='outage-attempt', convId='outage-conv', _turnId='outage-turn',
        _workerStartedAt=time.time(), status='running', config={}, thinking='', content='',
        content_lock=threading.Lock(), toolRounds=[{'round': 0, 'toolName': 'read_files',
            'status': 'done', 'result': 'retained history' * 1000}])
    try:
        _events.append_event(task, {'type': 'delta', 'content': '0'})
        lane = event_delivery.delivery_lane_for(task)
        with owner.condition:
            assert owner.condition.wait_for(lambda: lane.error is failure, timeout=2)
        started = time.perf_counter()
        for index in range(1, 64):
            task['content'] = str(index)
            _events.append_event(task, {'type': 'delta', 'content': str(index)})
        record_property('producer_after_outage_ms', (time.perf_counter() - started) * 1000)
        record_property('retained_after_outage_bytes', owner.snapshot()['pending_bytes'])
        assert not task.get('aborted') and not task['abort_event'].is_set()
        assert lane.accepted == 64 and lane.completed == 0 and frames == []
        with pytest.raises(EventDeliveryError):
            owner.flush(lane, recover=False)
        task['toolRounds'][0]['result'] = 'live mutation'
        recovered = True
        owner.flush(lane)
        assert committed_sequences == [0, 63]
        assert [frame['sequence'] for frame in frames] == list(range(64))
        assert [frame['event']['content'] for frame in frames] == list(map(str, range(64)))
        assert projections and all(value == 'retained history' * 1000 for value in projections)
        assert owner.snapshot()['pending_bytes'] == 0
    finally:
        recovered = True
        assert owner.close(3)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def test_progress_batch_bounds_split_without_a_total_admission_window():
    from lib.tasks_pkg.event_delivery import ProgressFrame
    owner = EventDeliveryCoordinator(workers=1, capacity=2, byte_capacity=2 * 1024 * 1024)
    lane = DeliveryLane({'id': 'batch-slots'})
    identity = object()
    entered, release = threading.Event(), threading.Event()
    commits, published = [], []
    def persist(frames):
        if frames[0].sequence == 0:
            entered.set()
            assert release.wait(3)
        commits.append([frame.sequence for frame in frames])
    def submit(sequence, timeout=.01):
        wire = {'type': 'delta', 'content': str(sequence)}
        frame = ProgressFrame(sequence, wire, lambda: published.append(sequence))
        owner.submit(lane, sequence, 2048, lambda: None, lambda: None,
            timeout=timeout, projection_source={'toolRounds': []}, projection_bytes=128,
            projection_identity=identity, progress_frame=frame, persist_progress=persist,
            progress_wire_bytes=2048, progress_payload_bytes=256)
    try:
        submit(0)
        assert entered.wait(2)
        for sequence in range(1, owner.progress_batch_limit + 1):
            submit(sequence)
        assert owner.snapshot()['pending_batches'] == 2
        assert owner.snapshot()['pending_events'] == owner.progress_batch_limit + 1
        submit(owner.progress_batch_limit + 1)
        assert owner.snapshot()['pending_batches'] == 3
        assert published == []
        release.set()
        owner.flush(lane)
        assert commits == [[0], list(range(1, owner.progress_batch_limit + 1)),
                           [owner.progress_batch_limit + 1]]
        assert published == list(range(owner.progress_batch_limit + 2))
        assert owner.snapshot()['pending_bytes'] == 0
        assert not owner.projections
    finally:
        release.set()
        assert owner.close(3)


def test_progress_batch_contract_outputs_are_current():
    from scripts.gen_task_event_delivery_contract import generated_outputs
    for path, expected in generated_outputs().items():
        assert path.read_text() == expected


def test_sampled_forced_checkpoint_does_not_wait_for_earlier_storage_work(coordinator, monkeypatch):
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _sync
    monkeypatch.setattr(event_delivery, '_COORDINATOR', coordinator)
    task = {'id': 'sampled-checkpoint', 'convId': '', '_userId': 1,
        '_workerStartedAt': time.time(), 'content': 'partial', 'thinking': '',
        'status': 'running', 'toolRounds': [], 'config': {}}
    lane = event_delivery.delivery_lane_for(task)
    entered, release, checkpoint_done = threading.Event(), threading.Event(), threading.Event()
    def earlier():
        entered.set()
        assert release.wait(3)
    def checkpoint(*args, **kwargs):
        assert kwargs['content'] == 'partial'
        checkpoint_done.set()
        return True
    monkeypatch.setattr(_sync, '_upsert_task_row', checkpoint)
    coordinator.submit(lane, 0, 128, earlier, lambda: None)
    assert entered.wait(2)
    try:
        assert _sync.checkpoint_task_partial(task, force=True, wait_for_durability=False)
        assert not checkpoint_done.is_set()
        assert lane.accepted == 2
        task['content'] = 'later'
        release.set()
        coordinator.flush(lane)
        assert checkpoint_done.is_set()
    finally:
        release.set()


def test_batch_metrics_distinguish_frames_slots_and_merges(monkeypatch):
    from lib.tasks_pkg import event_delivery
    from routes.metrics import _collect_infra_metrics
    monkeypatch.setattr(event_delivery, 'event_delivery_metrics', lambda: {
        'pending_events': 64, 'pending_batches': 2, 'peak_batches': 2,
        'merged_progress': 62, 'progress_batch_capacity': 500})
    lines = []
    _collect_infra_metrics(lines)
    body = '\n'.join(lines)
    assert 'tofu_task_event_delivery_pending_events 64' in body
    assert 'tofu_task_event_delivery_pending_batches 2' in body
    assert 'tofu_task_event_delivery_merged_progress_total 62' in body


def test_independent_lanes_start_all_callbacks_above_idle_and_residency_hints():
    owner = EventDeliveryCoordinator(workers=1, capacity=1, byte_capacity=1)
    barrier = threading.Barrier(32, timeout=10)
    lanes = [DeliveryLane({'id': f'demand-{index}'}) for index in range(32)]
    commits, publishes = [], []
    lock = threading.Lock()

    def persist(index):
        barrier.wait()
        with lock:
            commits.append(index)

    started = time.monotonic()
    try:
        for index, lane in enumerate(lanes):
            owner.submit(lane, 0, 8 * 1024 * 1024,
                lambda index=index: persist(index),
                lambda index=index: publishes.append(index), timeout=0)
        for lane in lanes:
            owner.flush(lane, timeout=10)
        assert set(commits) == set(publishes) == set(range(32))
        assert owner.snapshot()['admission_waits'] == 0
        assert owner.snapshot()['peak_workers'] >= 32
        assert owner.snapshot()['pending_bytes'] == 0
        print(f'32 task-event lanes / idle hint 1: {(time.monotonic()-started)*1000:.2f} ms')
    finally:
        assert owner.close(5)
    assert not owner.workers and not owner._pumps
    assert not any(thread.name.startswith('task-event-delivery_') for thread in threading.enumerate())


def test_unstarted_event_worker_failure_retains_only_its_lane_and_recovers(monkeypatch):
    owner = EventDeliveryCoordinator(workers=1, capacity=1, byte_capacity=1)
    entered, release, independent = threading.Event(), threading.Event(), threading.Event()
    first = DeliveryLane({'id': 'active'})
    failed = DeliveryLane({'id': 'failed'})
    sibling = DeliveryLane({'id': 'sibling'})
    original = threading.Thread.start
    starts = 0
    calls = []

    def start(thread):
        nonlocal starts
        if thread.name.startswith('task-event-delivery_'):
            starts += 1
            if starts == 2:
                raise RuntimeError('injected physical worker start failure')
        return original(thread)

    def blocked():
        entered.set()
        assert release.wait(5)
        calls.append('active')

    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        owner.submit(first, 0, 128, blocked, lambda: None)
        assert entered.wait(2)
        with pytest.raises(EventDeliveryError, match='worker could not start'):
            owner.submit(failed, 0, 128, lambda: calls.append('failed'), lambda: None)
        assert failed.error is not None and len(failed.pending) == 1
        owner.submit(sibling, 0, 128, independent.set, lambda: None)
        assert independent.wait(2)
        assert 'failed' not in calls
        owner.flush(failed, timeout=5)
        assert calls == ['failed']
        release.set()
        owner.flush(first)
        owner.flush(sibling)
        assert calls == ['failed', 'active']
        assert owner.snapshot()['pending_bytes'] == 0
    finally:
        release.set()
        assert owner.close(5)
    assert not any(thread.name.startswith('task-event-delivery_') for thread in threading.enumerate())


@pytest.mark.parametrize('stale_parent', [False, True])
def test_child_disposal_after_root_acceptance_preserves_parent_authority(monkeypatch, coordinator, stale_parent):
    import uuid
    from lib import turn_lifecycle
    from lib.agent_core.publication import ChildPublicationFence, check_publication
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    monkeypatch.setattr(event_delivery, '_COORDINATOR', coordinator)
    task = chat_task_runtime.create(user_id=1, task_id='transfer-' + uuid.uuid4().hex)
    task.update(_attemptId='parent-attempt', _turnId='parent-turn', convId='parent-conv',
        _workerStartedAt=time.time(), status='running', config={}, content='', thinking='',
        toolRounds=[], content_lock=threading.Lock())
    lane = event_delivery.delivery_lane_for(task)
    entered, release = threading.Event(), threading.Event()
    observed = []
    principal = ContextVar('transfer-principal', default=None)

    def persist(task, wire, **kwargs):
        check_publication()
        if wire['type'] == 'delta':
            assert principal.get() == 'parent-owner'
        assert task['_attemptId'] == 'parent-attempt'
        if stale_parent:
            from lib.tasks_pkg.event_delivery import EventDeliveryRevoked
            raise EventDeliveryRevoked('parent attempt superseded')
        observed.extend(row['event']['content'] for row in
                        kwargs['task_event'].get('preceding_events', []))
        observed.append(wire.get('content', wire['type']))
        return 'carried'

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', persist)
    monkeypatch.setattr('lib.translate.terminal.schedule_terminal_turn_translations', lambda task: None)
    def blocker():
        entered.set()
        assert release.wait(3)
    coordinator.submit(lane, -1, 128, blocker, lambda: None)
    assert entered.wait(2)
    fence = ChildPublicationFence()
    token = principal.set('parent-owner')
    try:
        # Two frames cover both standalone acceptance and merged batch context.
        for content in ('first', 'second'):
            fence.deliver(lambda event: _events.append_event(task, event),
                          {'type': 'delta', 'content': content})
        fence.close()
        assert lane.accepted == 3
        release.set()
        event_delivery.flush_task_events(task)
        assert lane.completed == lane.accepted
        if stale_parent:
            assert lane.revoked
            assert observed == []
            assert task['_eventDeliveryRevoked']
        else:
            assert not lane.revoked
            assert observed == ['first', 'second']
            _events.append_event(task, {'type': 'done'})
            assert observed == ['first', 'second', 'done']
            assert '_eventDeliveryError' not in task
    finally:
        principal.reset(token)
        release.set()
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def test_child_revoked_before_root_admission_is_not_accepted(coordinator):
    from lib.agent_core.publication import ChildPublicationFence, ChildPublicationRevoked
    lane = DeliveryLane({'id': 'revoked-before-admission'})
    fence = ChildPublicationFence()
    def sink(event):
        # Closure can win after callback entry but before queue acceptance.
        fence.close()
        coordinator.submit(lane, 0, 128, lambda: pytest.fail('must not persist'), lambda: None)
    with pytest.raises(ChildPublicationRevoked):
        fence.deliver(sink, {})
    assert lane.accepted == 0
    assert not lane.pending
    assert coordinator.snapshot()['pending_events'] == 0

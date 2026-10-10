"""Demand-driven scheduling and translation TaskRuntime integration."""

from __future__ import annotations

import threading
import time

import pytest

from lib.agent_core.fair_work_lane import (
    OwnerFairWorkLane,
)


pytestmark = pytest.mark.unit


class _ControlledPickupLane(OwnerFairWorkLane):
    """Hold expansion at a controlled pickup boundary to test transient pickup races.

    Production starts all independent work. Queue tests deliberately delay
    additional carrier creation so cancellation/selection seams stay observable.
    """
    def _ensure_workers_locked(self):
        if len(self._threads) < self.max_workers:
            super()._ensure_workers_locked()


def _lane(*, workers=1, capacity=8, idle=0.05):
    return _ControlledPickupLane(
        max_workers=workers,
        queue_capacity=capacity,
        idle_seconds=idle,
        thread_name_prefix='test-fair-work',
        metric_pool='test-fair-work',
    )


def test_pending_jobs_rotate_across_explicit_owners():
    lane = _lane(capacity=8)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    order = []

    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)

    first = lane.submit_task('a-active', 1, blocker)
    assert blocker_started.wait(1)
    futures = [
        lane.submit_task('a-2', 1, lambda: order.append('a-2')),
        lane.submit_task('a-3', 1, lambda: order.append('a-3')),
        lane.submit_task('b-1', 2, lambda: order.append('b-1')),
        lane.submit_task('b-2', 2, lambda: order.append('b-2')),
    ]
    release_blocker.set()

    first.result(timeout=2)
    for future in futures:
        future.result(timeout=2)
    assert order == ['a-2', 'b-1', 'a-3', 'b-2']
    lane.shutdown()


def test_attended_work_advances_only_within_its_owner_queue():
    lane = _lane(capacity=8)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    order = []

    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)

    active = lane.submit_task('active', 0, blocker)
    assert blocker_started.wait(1)
    pending = [
        lane.submit_task('owner-a-background', 1,
                         lambda: order.append('a-background')),
        lane.submit_task('owner-b-background', 2,
                         lambda: order.append('b-background')),
        lane.submit_task(
            'owner-a-attended',
            1,
            lambda: order.append('a-attended'),
            front_of_owner_queue=True,
        ),
    ]
    release_blocker.set()

    active.result(timeout=2)
    for future in pending:
        future.result(timeout=2)
    assert order == ['a-attended', 'b-background', 'a-background']
    lane.shutdown()


def test_transient_queue_has_no_count_quota_and_cancellation_removes_job():
    lane = _lane(capacity=2)
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)

    active = lane.submit_task('active', 1, blocker)
    assert blocker_started.wait(1)
    queued = lane.submit_task('queued-1', 1, lambda: None)
    lane.submit_task('queued-2', 2, lambda: None)

    additional = lane.submit_task('additional', 3, lambda: None)

    assert lane.cancel_task('queued-1') is True
    assert queued.cancelled() is True
    replacement = lane.submit_task('replacement', 3, lambda: 'accepted')
    release_blocker.set()
    active.result(timeout=2)
    assert replacement.result(timeout=2) == 'accepted'
    snapshot = lane.snapshot()
    additional.result(timeout=2)
    assert snapshot['rejected'] == 0
    assert snapshot['queueCapacity'] == 0
    assert snapshot['cancelled'] == 1
    assert snapshot['peakQueued'] >= 3
    lane.shutdown()


def test_workers_start_lazily_and_retire_after_idle_budget():
    lane = _lane(workers=2, capacity=2, idle=0.03)
    assert lane.snapshot()['residentThreads'] == 0
    lane.submit_task('one', 1, lambda: None).result(timeout=1)

    deadline = time.monotonic() + 1
    while lane.snapshot()['residentThreads'] and time.monotonic() < deadline:
        time.sleep(0.01)

    snapshot = lane.snapshot()
    assert snapshot['residentThreads'] == 0
    assert snapshot['retiredThreads'] >= 1
    lane.shutdown()


def test_worker_start_failure_rolls_back_admission(monkeypatch):
    lane = _lane(capacity=2)

    def fail_start(_thread):
        raise OSError('thread ceiling reached')

    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(OSError, match='thread ceiling'):
        lane.submit_task('cannot-start', 1, lambda: None)

    snapshot = lane.snapshot()
    assert snapshot['queued'] == 0
    assert snapshot['residentThreads'] == 0
    assert snapshot['accepted'] == 0
    assert snapshot['rejected'] == 1


def test_translation_submission_stays_pending_until_worker_entry(monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.translate import execution

    lane = _lane(capacity=2)
    monkeypatch.setattr(execution, '_translation_lane', lane)
    runtime = TaskRuntime('translate-test', push_channel='')
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    first = runtime.create(user_id=7, task_id='first')
    second = runtime.create(user_id=8, task_id='second')

    def blocking_worker():
        blocker_started.set()
        assert release_blocker.wait(2)
        runtime.finish(first['id'], result='first')

    def queued_worker():
        runtime.finish(second['id'], result='second')

    assert execution.submit_translation_task(
        runtime, first['id'], blocking_worker,
        running_fields={'progress': None}) is True
    assert blocker_started.wait(1)
    assert execution.submit_translation_task(
        runtime, second['id'], queued_worker,
        running_fields={'progress': None}) is True
    assert second['status'] == 'pending'

    assert execution.abort_translation_task(
        runtime, second['id'], user_id=8) is True
    assert second['status'] == 'aborted'
    assert lane.snapshot()['queued'] == 0

    release_blocker.set()
    deadline = time.monotonic() + 2
    while first['status'] != 'done' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first['status'] == 'done'
    lane.shutdown()


def test_optional_and_attended_translation_reuse_lane_with_correct_priority(
        monkeypatch):
    from lib.translate import execution

    lane = _lane(capacity=3)
    monkeypatch.setattr(execution, '_translation_lane', lane)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    order = []

    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)

    active = lane.submit_task('active', 7, blocker)
    assert blocker_started.wait(1)
    background = execution.submit_reconstructible_translation(
        'segments:task-1',
        owner_user_id=7,
        function=lambda: order.append('reconstructible'),
    )
    attended = execution.submit_attended_translation(
        'request-1',
        owner_user_id=7,
        function=lambda: order.append('attended'),
    )

    release_blocker.set()
    active.result(timeout=2)
    attended.result(timeout=2)
    background.result(timeout=2)
    assert order == ['attended', 'reconstructible']
    lane.shutdown()


def test_translation_accepts_work_beyond_retired_queue_allowance(monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.translate import execution

    lane = _lane(capacity=1)
    monkeypatch.setattr(execution, '_translation_lane', lane)
    runtime = TaskRuntime('translate-test', push_channel='')
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    active = runtime.create(user_id=1, task_id='active')
    queued = runtime.create(user_id=1, task_id='queued')
    rejected = runtime.create(user_id=2, task_id='rejected')

    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)
        runtime.finish(active['id'], result='done')

    assert execution.submit_translation_task(
        runtime, active['id'], blocker) is True
    assert blocker_started.wait(1)
    assert execution.submit_translation_task(
        runtime, queued['id'], lambda: None) is True
    assert execution.submit_translation_task(
        runtime, rejected['id'], lambda: None) is True
    assert rejected['status'] == 'pending'

    release_blocker.set()
    lane.shutdown()


def test_translation_worker_start_failure_is_typed(monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.translate import execution

    class _UnavailableLane:
        def submit_task(self, *_args, **_kwargs):
            raise OSError('thread ceiling reached')

    monkeypatch.setattr(execution, '_translation_lane', _UnavailableLane())
    runtime = TaskRuntime('translate-test', push_channel='')
    task = runtime.create(user_id=1, task_id='cannot-start')

    assert execution.submit_translation_task(
        runtime, task['id'], lambda: None) is False
    assert task['status'] == 'error'
    assert task['error']['kind'] == 'task_start_failed'
    assert task['error']['retryable'] is True


@pytest.mark.parametrize('shutdown', [False, True])
def test_cancellation_callbacks_do_not_hold_the_scheduler_lock(shutdown):
    lane = _lane(capacity=1)
    entered = threading.Event()
    release = threading.Event()
    observed = threading.Event()
    callback_observations = []
    observers = []

    def blocker():
        entered.set()
        assert release.wait(5)

    active = lane.submit_task('active', 1, blocker)
    assert entered.wait(1)
    pending = lane.submit_task('pending', 2, lambda: pytest.fail('cancelled work ran'))

    def on_cancelled(_future):
        # A callback may coordinate with another thread using the public
        # scheduler API. Cancellation must not lock that thread out.
        def inspect():
            callback_observations.append(lane.snapshot())
            observed.set()
        observer = threading.Thread(target=inspect)
        observers.append(observer)
        observer.start()
        callback_observations.append(observed.wait(1))

    pending.add_done_callback(on_cancelled)
    try:
        if shutdown:
            lane.shutdown(wait=False, cancel_pending=True)
        else:
            assert lane.cancel_task('pending')
        assert pending.cancelled()
        assert True in callback_observations, 'callback blocked a scheduler observer'
        snapshots = [value for value in callback_observations if isinstance(value, dict)]
        assert snapshots[0]['queued'] == 0
        assert snapshots[0]['cancelled'] == 1
    finally:
        release.set()
        active.result(timeout=2)
        lane.shutdown()
        for observer in observers:
            observer.join(timeout=2)
            assert not observer.is_alive()


@pytest.mark.parametrize('cancellation', ['job', 'future', 'shutdown'])
def test_removed_cancelled_future_notifies_waiters(cancellation):
    from concurrent.futures import wait

    lane = _lane(capacity=1)
    entered = threading.Event()
    release = threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5)
    active = lane.submit_task('active', 1, blocker)
    assert entered.wait(1)
    pending = lane.submit_task('pending', 2, lambda: pytest.fail('cancelled work ran'))
    try:
        if cancellation == 'job':
            assert lane.cancel_task('pending')
        elif cancellation == 'future':
            assert pending.cancel()
        else:
            lane.shutdown(wait=False, cancel_pending=True)
        completed, remaining = wait([pending], timeout=0.1)
        assert completed == {pending} and not remaining
    finally:
        release.set()
        active.result(timeout=2)
        lane.shutdown()


def test_future_cancellation_racing_worker_pickup_settles_once():
    from concurrent.futures import as_completed

    lane = _lane(workers=2, capacity=64)
    release = threading.Event()
    entered = threading.Barrier(3)
    def blocker():
        entered.wait(timeout=2)
        assert release.wait(5)
    active = [lane.submit_task(f'active-{i}', i + 1, blocker) for i in range(2)]
    entered.wait(timeout=2)
    futures = [lane.submit_task(f'racing-{i}', i % 3 + 1, lambda: 'ran')
               for i in range(40)]
    def cancel():
        assert release.wait(5)
        for future in futures:
            future.cancel()
    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        release.set()
        assert set(as_completed(active + futures, timeout=3)) == set(active + futures)
    finally:
        release.set()
        canceller.join(timeout=2)
        lane.shutdown()
    assert not canceller.is_alive()
    snapshot = lane.snapshot()
    assert snapshot['active'] == snapshot['queued'] == snapshot['residentThreads'] == 0
    assert snapshot['cancelled'] == sum(future.cancelled() for future in futures)


def test_fresh_translation_precedes_same_owner_backfill(monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.translate import execution
    lane = _lane(capacity=8)
    monkeypatch.setattr(execution, '_translation_lane', lane)
    runtime = TaskRuntime('translation-priority', push_channel='')
    started, release = threading.Event(), threading.Event()
    order = []
    def block():
        started.set()
        assert release.wait(2)
    active = lane.submit_task('active', 7, block)
    assert started.wait(1)
    futures = []
    for name, prioritize in [('backfill', False), ('fresh', True)]:
        runtime.create(user_id=7, task_id=name)
        def work(name=name):
            order.append(name)
            runtime.finish(name, result=name)
        assert execution.submit_translation_task(runtime, name, work, prioritize=prioritize)
    release.set()
    active.result(timeout=2)
    lane.shutdown(wait=True)
    assert order == ['fresh', 'backfill']


def test_independent_burst_starts_on_demand_and_reclaims_excess_idle_threads():
    lane = OwnerFairWorkLane(max_workers=1, queue_capacity=1, idle_seconds=.03,
        thread_name_prefix='test-demand', metric_pool='test-demand')
    release = threading.Event()
    entered = set()
    lock = threading.Lock()
    def run(index):
        with lock:
            entered.add(index)
        assert release.wait(5)
        return index
    futures = []
    try:
        for index in range(64):
            futures.append(lane.submit_task(str(index), index % 4 + 1,
                                           lambda index=index: run(index)))
        deadline = time.monotonic() + 3
        while len(entered) != 64:
            assert time.monotonic() < deadline
            time.sleep(.005)
        snapshot = lane.snapshot()
        assert snapshot['active'] == 64
        assert snapshot['queued'] == 0
        assert snapshot['queueCapacity'] == 0
        assert snapshot['idleReuseWorkers'] == 1
        assert snapshot['rejected'] == 0
        release.set()
        assert [future.result(timeout=1) for future in futures] == list(range(64))
        deadline = time.monotonic() + 2
        while lane.snapshot()['residentThreads']:
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert lane.snapshot()['retiredThreads'] == 64
    finally:
        release.set()
        lane.shutdown()
    snapshot = lane.snapshot()
    assert snapshot['active'] == snapshot['queued'] == snapshot['residentThreads'] == 0
    assert not lane._jobs_by_id and not lane._running_ids


def test_idle_reusable_carrier_does_not_retain_completed_input_or_output():
    import gc
    import weakref
    class Payload:
        pass
    lane = OwnerFairWorkLane(max_workers=1, idle_seconds=0,
        thread_name_prefix='test-idle-release', metric_pool='test-idle-release')
    payload = Payload()
    source_ref = weakref.ref(payload)
    future = lane.submit_task('payload', 1, lambda payload=payload: Payload())
    output = future.result(timeout=2)
    output_ref = weakref.ref(output)
    del payload, output, future
    try:
        deadline = time.monotonic() + 2
        while lane.snapshot()['active']:
            assert time.monotonic() < deadline
            time.sleep(.005)
        gc.collect()
        assert lane.snapshot()['residentThreads'] == 1
        assert source_ref() is None and output_ref() is None
    finally:
        lane.shutdown()


def test_metric_failures_cannot_deny_jobs_or_break_cleanup(monkeypatch):
    import lib.agent_core.fair_work_lane as module
    def fail(*args, **kwargs):
        raise RuntimeError('observability unavailable')
    for name in ('publish_executor_state', 'observe_executor_queue_wait',
                 'record_executor_rejection', 'record_executor_idle_retirement'):
        monkeypatch.setattr(module, name, fail)
    lane = OwnerFairWorkLane(max_workers=1, idle_seconds=.01,
        thread_name_prefix='test-metric-failure', metric_pool='test-metric-failure')
    try:
        assert lane.submit_task('one', 1, lambda: 'done').result(timeout=1) == 'done'
        deadline = time.monotonic() + 2
        while lane.snapshot()['residentThreads']:
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert not lane._jobs_by_id
    finally:
        lane.shutdown()
    with pytest.raises(RuntimeError, match='lane shutdown'):
        lane.submit_task('two', 2, lambda: None)


def test_new_carrier_start_failure_preserves_already_running_owner(monkeypatch):
    lane = OwnerFairWorkLane(max_workers=1, idle_seconds=.03,
        thread_name_prefix='test-start-rollback', metric_pool='test-start-rollback')
    entered = threading.Event()
    release = threading.Event()
    def existing():
        entered.set()
        assert release.wait(3)
        return 'owned result'
    future = lane.submit_task('existing', 1, existing)
    try:
        assert entered.wait(1)
        def fail_start(thread):
            raise OSError('actual host start failure')
        with monkeypatch.context() as failure:
            failure.setattr(threading.Thread, 'start', fail_start)
            with pytest.raises(OSError, match='actual host start failure'):
                lane.submit_task('new-owner', 2, lambda: 'unstarted')
        snapshot = lane.snapshot()
        assert snapshot['active'] == snapshot['residentThreads'] == 1
        assert snapshot['queued'] == 0
        assert snapshot['accepted'] == snapshot['rejected'] == 1
        assert set(lane._jobs_by_id) == {'existing'}
    finally:
        release.set()
        lane.shutdown()
    assert future.result(timeout=1) == 'owned result'

"""Finite owner-fair scheduling and translation TaskRuntime integration."""

from __future__ import annotations

import threading
import time

import pytest

from lib.agent_core.fair_work_lane import (
    FairWorkLaneQueueFull,
    OwnerFairWorkLane,
)


pytestmark = pytest.mark.unit


def _lane(*, workers=1, capacity=8, idle=0.05):
    return OwnerFairWorkLane(
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


def test_queue_capacity_is_hard_and_cancellation_releases_it():
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

    with pytest.raises(FairWorkLaneQueueFull):
        lane.submit_task('rejected', 3, lambda: None)

    assert lane.cancel_task('queued-1') is True
    assert queued.cancelled() is True
    replacement = lane.submit_task('replacement', 3, lambda: 'accepted')
    release_blocker.set()
    active.result(timeout=2)
    assert replacement.result(timeout=2) == 'accepted'
    snapshot = lane.snapshot()
    assert snapshot['rejected'] == 1
    assert snapshot['cancelled'] == 1
    assert snapshot['peakQueued'] <= 2
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


def test_translation_queue_rejection_settles_typed_task_error(monkeypatch):
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
        runtime, rejected['id'], lambda: None) is False
    assert rejected['status'] == 'error'
    assert rejected['error']['kind'] == 'server_busy'
    assert rejected['error']['retryable'] is True
    assert 'queue is full' in rejected['error']['detail'].lower()

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

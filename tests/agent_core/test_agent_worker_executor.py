"""Demand-driven Agent scheduling, cancellation and wedged-thread ownership."""

from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from lib.agent_core.worker_executor import (
    RecoverableAgentExecutor,
)

pytestmark = pytest.mark.unit


def test_queue_wait_observer_failure_does_not_strand_job(monkeypatch):
    from lib import observability

    def fail_observation(*args, **kwargs):
        raise RuntimeError('queue observation unavailable')

    monkeypatch.setattr(observability, 'observe_executor_queue_wait', fail_observation)
    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=0, max_abandoned_workers=1)
    try:
        future = executor.submit(lambda: 42)
        assert future.result(timeout=2) == 42
        assert executor.resource_snapshot()['used'] == 0
        assert executor.resource_snapshot()['completed'] == 1
    finally:
        executor.shutdown(wait=True)


def test_generic_job_ids_read_no_os_randomness_and_stay_unique(monkeypatch):
    # uuid4 reads the OS random source, which releases the GIL on every submit.
    import uuid
    from tofu_workers.executor import DemandExecutor

    def os_random_identity():
        raise AssertionError('a generic job id must not read OS randomness')

    monkeypatch.setattr(uuid, 'uuid4', os_random_identity)
    executor = DemandExecutor(max_workers=8, queue_capacity=0, max_abandoned_workers=1)
    release = threading.Event()
    try:
        # All eight stay registered at once, so a repeated id would be refused.
        futures = [executor.submit(release.wait, 5) for _ in range(8)]
        release.set()
        assert [future.result(timeout=5) for future in futures] == [True] * 8
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_idle_carrier_releases_completed_payload_without_cycle_collection():
    class Payload:
        pass
    payload = Payload()
    reference = weakref.ref(payload)
    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=0, max_abandoned_workers=1)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        future = executor.submit(lambda value: None, payload)
        del payload
        assert future.result(timeout=2) is None
        del future
        until = time.monotonic() + 2
        while reference() is not None and time.monotonic() < until:
            time.sleep(0.002)
        assert reference() is None, 'idle worker or cancel callback retained completed input'
        assert executor.worker_threads_snapshot(), 'test must exercise retained idle carrier'
    finally:
        executor.shutdown(wait=True)
        if was_enabled:
            gc.enable()


def test_burst_starts_without_waiting_for_old_worker_or_queue_limits():
    release = threading.Event()
    started = [threading.Event() for _ in range(24)]
    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=1, max_abandoned_workers=1,
        thread_name_prefix='test-agent-demand')
    try:
        futures = [executor.submit_task(
            f'task-{index}', lambda event=event: (event.set(), release.wait(5)))
            for index, event in enumerate(started)]
        assert all(event.wait(2) for event in started)
        snapshot = executor.scheduling_snapshot()
        assert snapshot['active'] == 24
        assert snapshot['queued'] == 0
        assert snapshot['queueCapacity'] == 0
        executor.resize(1)
        assert not any(future.done() for future in futures)
        release.set()
        for future in futures:
            future.result(2)
        deadline = time.monotonic() + 2
        while executor.scheduling_snapshot()['residentThreads'] > 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert executor.resource_snapshot()['used'] == 0
        assert executor.resource_snapshot()['completed'] == 24
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_worker_thread_start_failure_rolls_back_the_submission(monkeypatch):
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=1,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-start-failure',
    )

    def reject_thread_start(_thread):
        raise RuntimeError('thread budget exhausted')

    monkeypatch.setattr(threading.Thread, 'start', reject_thread_start)
    try:
        with pytest.raises(RuntimeError, match='thread budget exhausted'):
            executor.submit_task('task-rejected', lambda: None)
        snapshot = executor.scheduling_snapshot('task-rejected')
        assert snapshot['queued'] == 0
        assert snapshot['residentThreads'] == 0
        assert snapshot['taskState'] == 'unknown'
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def test_abandoning_a_proven_wedge_restores_one_logical_slot():
    release_wedge = threading.Event()
    wedge_started = threading.Event()
    replacement_started = threading.Event()
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=2,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-recovery',
    )
    try:
        wedged = executor.submit_task(
            'task-wedged',
            lambda: (wedge_started.set(), release_wedge.wait(timeout=5)),
        )
        assert wedge_started.wait(timeout=1)
        replacement = executor.submit_task(
            'task-next', lambda: replacement_started.set())
        assert replacement_started.wait(timeout=1)

        assert executor.abandon_task('task-wedged') is True
        assert replacement_started.wait(timeout=1)
        replacement.result(timeout=1)
        snapshot = executor.scheduling_snapshot('task-wedged')
        assert snapshot['capacity'] == 1
        assert snapshot['active'] == 0
        assert snapshot['abandoned'] == 1
        assert snapshot['residentThreads'] >= 1
        assert snapshot['taskState'] == 'abandoned'

        release_wedge.set()
        wedged.result(timeout=2)
        for _ in range(100):
            if executor.scheduling_snapshot()['residentThreads'] <= 1:
                break
            time.sleep(0.01)
        assert executor.scheduling_snapshot()['residentThreads'] <= 1
    finally:
        release_wedge.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_abandoned_thread_residency_has_a_hard_replacement_budget():
    release_first = threading.Event()
    release_second = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=2,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-recovery-budget',
    )
    try:
        first = executor.submit_task(
            'task-first',
            lambda: (first_started.set(), release_first.wait(timeout=5)),
        )
        assert first_started.wait(timeout=1)
        second = executor.submit_task(
            'task-second',
            lambda: (second_started.set(), release_second.wait(timeout=5)),
        )
        assert executor.abandon_task('task-first') is True
        assert second_started.wait(timeout=1)
        assert executor.abandon_task('task-second') is False
        assert executor.scheduling_snapshot()['abandoned'] == 1
    finally:
        release_first.set()
        release_second.set()
        first.result(timeout=2)
        second.result(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def test_abandonment_rolls_back_when_replacement_thread_cannot_start(
        monkeypatch):
    release = threading.Event()
    started = threading.Event()
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=2,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-recovery-start-failure',
    )
    try:
        running = executor.submit_task(
            'task-wedged',
            lambda: (started.set(), release.wait(timeout=5)),
        )
        assert started.wait(timeout=1)
        reserve_own = executor._reserve_own_workers_locked
        monkeypatch.setattr(executor, '_reserve_own_workers_locked', lambda: ())
        queued = executor.submit_task('task-next', lambda: 'next')
        monkeypatch.setattr(executor, '_reserve_own_workers_locked', reserve_own)

        def reject_thread_start(_thread):
            raise RuntimeError('thread budget exhausted')

        monkeypatch.setattr(threading.Thread, 'start', reject_thread_start)
        with pytest.raises(RuntimeError, match='thread budget exhausted'):
            executor.abandon_task('task-wedged')

        snapshot = executor.scheduling_snapshot('task-wedged')
        assert snapshot['taskState'] == 'running'
        assert snapshot['active'] == 1
        assert snapshot['abandoned'] == 0
        assert snapshot['residentThreads'] == 1

        release.set()
        running.result(timeout=2)
        assert queued.result(timeout=2) == 'next'
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_worker_failure_settles_scheduler_before_future_raises():
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=1,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-failure-order',
    )

    def fail():
        raise RuntimeError('worker failed')

    try:
        future = executor.submit_task('task-failed', fail)
        with pytest.raises(RuntimeError, match='worker failed'):
            future.result(timeout=2)

        snapshot = executor.scheduling_snapshot('task-failed')
        assert snapshot['active'] == 0
        assert snapshot['queued'] == 0
        assert snapshot['taskState'] == 'unknown'
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('cancellation', ['task', 'future', 'shutdown'])
def test_cancelled_agent_notifies_waiters_without_locking_callbacks(cancellation, monkeypatch):
    from concurrent.futures import wait

    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=1, max_abandoned_workers=1,
        thread_name_prefix='test-agent-cancel-notification')
    release = threading.Event()
    started = threading.Event()
    observed = threading.Event()
    callback_completed = []
    observers = []
    active = executor.submit_task('active', lambda: (started.set(), release.wait(5)))
    assert started.wait(1)
    monkeypatch.setattr(executor, '_reserve_own_workers_locked', lambda: ())
    pending = executor.submit_task('pending', lambda: pytest.fail('cancelled task ran'))
    def callback(_future):
        def inspect():
            executor.scheduling_snapshot()
            observed.set()
        observer = threading.Thread(target=inspect)
        observers.append(observer)
        observer.start()
        callback_completed.append(observed.wait(1))
    pending.add_done_callback(callback)
    try:
        if cancellation == 'task':
            assert executor.cancel_task('pending')
        elif cancellation == 'future':
            assert pending.cancel()
        else:
            executor.shutdown(wait=False, cancel_futures=True)
        completed, remaining = wait([pending], timeout=0.1)
        assert completed == {pending} and not remaining
        assert callback_completed == [True]
        assert executor.scheduling_snapshot()['queued'] == 0
    finally:
        release.set()
        active.result(timeout=2)
        executor.shutdown()
        for observer in observers:
            observer.join(timeout=2)
            assert not observer.is_alive()


_EXECUTOR_GAUGES = ('workers', 'queued', 'active', 'resident_threads', 'abandoned')


def _scraped_executor_gauges(pool):
    from lib.observability import prometheus_lines
    series = {f'tofu_executor_{key}{{pool="{pool}"}}': key for key in _EXECUTOR_GAUGES}
    gauges = {}
    for line in prometheus_lines():
        name, _, value = line.rpartition(' ')
        if name in series:
            gauges[series[name]] = float(value)
    return gauges


def _await_one_parked_carrier(executor):
    deadline = time.monotonic() + 5
    while not (executor.idle_retirement_snapshot(1e-9)['due']
               and executor.scheduling_snapshot()['residentThreads'] == 1):
        assert time.monotonic() < deadline, 'executor did not settle to one parked carrier'
        time.sleep(0.001)


def test_jobs_publish_no_executor_state_and_scrapes_read_exact_gauges(monkeypatch):
    from lib import observability
    pool_name = 'scrape-exact-test'
    published = []
    publish = observability.publish_executor_state

    def record(pool, **state):
        if pool == pool_name:
            published.append(state)
        publish(pool, **state)

    monkeypatch.setattr(observability, 'publish_executor_state', record)
    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=0, max_abandoned_workers=1,
        thread_name_prefix=pool_name, metric_pool=pool_name)
    release, started = threading.Event(), threading.Event()
    try:
        for _ in range(64):
            assert executor.submit(int).result(timeout=2) == 0
        assert published == [], 'accepted jobs published executor state'
        _await_one_parked_carrier(executor)
        assert _scraped_executor_gauges(pool_name) == {
            'workers': 1, 'queued': 0, 'active': 0, 'resident_threads': 1, 'abandoned': 0}
        running = executor.submit(lambda: (started.set(), release.wait(5)))
        assert started.wait(2)
        assert _scraped_executor_gauges(pool_name) == {
            'workers': 1, 'queued': 0, 'active': 1, 'resident_threads': 1, 'abandoned': 0}
        release.set()
        running.result(timeout=2)
        _await_one_parked_carrier(executor)
        assert _scraped_executor_gauges(pool_name)['active'] == 0
    finally:
        release.set()
        executor.shutdown(wait=True)
    # A stopped pool keeps no owner, so its series reports zero, not its idle hint.
    assert _scraped_executor_gauges(pool_name) == dict.fromkeys(_EXECUTOR_GAUGES, 0)


def test_completion_callback_can_submit_and_wait_without_occupying_idle_worker():
    release = threading.Event()
    started = threading.Event()
    callback_done = threading.Event()
    results = []
    executor = RecoverableAgentExecutor(
        max_workers=1, queue_capacity=0, max_abandoned_workers=1)
    try:
        parent = executor.submit_task(
            'parent', lambda: (started.set(), release.wait(3)))
        assert started.wait(1)
        def on_done(_future):
            try:
                child = executor.submit_task('child', lambda: 'child result')
                results.append(child.result(1))
            except Exception as error:
                results.append(error)
            finally:
                callback_done.set()
        parent.add_done_callback(on_done)
        release.set()
        assert callback_done.wait(2)
        assert results == ['child result']
    finally:
        release.set()
        executor.shutdown()

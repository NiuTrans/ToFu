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
        ensure_workers = executor._reserve_workers_locked
        monkeypatch.setattr(executor, '_reserve_workers_locked', lambda: ())
        queued = executor.submit_task('task-next', lambda: 'next')
        monkeypatch.setattr(executor, '_reserve_workers_locked', ensure_workers)

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
    monkeypatch.setattr(executor, '_reserve_workers_locked', lambda: ())
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

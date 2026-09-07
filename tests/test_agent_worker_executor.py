"""Bounded Agent scheduling and wedged-thread capacity recovery contracts."""

from __future__ import annotations

import threading
import time

import pytest

from lib.agent_core.worker_executor import (
    AgentExecutorQueueFull,
    RecoverableAgentExecutor,
)


pytestmark = pytest.mark.unit


def test_agent_executor_queue_is_finite_and_pending_work_is_cancellable():
    release = threading.Event()
    started = threading.Event()
    executor = RecoverableAgentExecutor(
        max_workers=1,
        queue_capacity=1,
        max_abandoned_workers=1,
        thread_name_prefix='test-agent-bounded',
    )
    try:
        first = executor.submit_task(
            'task-running', lambda: (started.set(), release.wait(timeout=3)))
        assert started.wait(timeout=1)
        queued = executor.submit_task('task-queued', lambda: 'never-ran')

        with pytest.raises(AgentExecutorQueueFull, match='queue is full'):
            executor.submit_task('task-rejected', lambda: None)

        snapshot = executor.scheduling_snapshot('task-queued')
        queued_for_seconds = snapshot.pop('queuedForSeconds')
        assert 0 <= queued_for_seconds < 1
        assert snapshot == {
            'capacity': 1,
            'active': 1,
            'queued': 1,
            'available': 0,
            'queueCapacity': 1,
            'abandoned': 0,
            'replacementCapacity': 1,
            'residentThreads': 1,
            'taskState': 'queued',
            'queuePosition': 1,
        }
        assert executor.cancel_task('task-queued') is True
        assert queued.cancelled()
        assert executor.scheduling_snapshot()['queued'] == 0

        release.set()
        first.result(timeout=2)
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
        assert not replacement_started.wait(timeout=0.05)

        assert executor.abandon_task('task-wedged') is True
        assert replacement_started.wait(timeout=1)
        replacement.result(timeout=1)
        snapshot = executor.scheduling_snapshot('task-wedged')
        assert snapshot['capacity'] == 1
        assert snapshot['active'] == 0
        assert snapshot['abandoned'] == 1
        assert snapshot['residentThreads'] == 2
        assert snapshot['taskState'] == 'abandoned'

        release_wedge.set()
        wedged.result(timeout=2)
        for _ in range(100):
            if executor.scheduling_snapshot()['residentThreads'] == 1:
                break
            time.sleep(0.01)
        assert executor.scheduling_snapshot()['residentThreads'] == 1
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
        queued = executor.submit_task('task-next', lambda: 'next')

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

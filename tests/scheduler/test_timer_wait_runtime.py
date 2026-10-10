"""Shared timer wait loop owns physical callbacks through stop and cancellation."""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def test_blocked_callbacks_do_not_queue_healthy_work_and_shutdown_drains():
    from lib.scheduler.timer._runtime import TimerWatcherRuntime, TimerWatcherHandle, run_blocking
    runtime = TimerWatcherRuntime()
    barrier = threading.Barrier(33)
    release = threading.Event()
    finished = []
    owner = contextvars.ContextVar('test_timer_owner')
    def blocked():
        assert owner.get() == 7
        barrier.wait(5)
        assert release.wait(10)
    async def target():
        token = owner.set(7)
        try:
            await run_blocking(blocked)
        except asyncio.CancelledError:
            pass  # Cooperative shutdown after the physical call settles.
        finally:
            owner.reset(token)
            finished.append(1)
    handles = [TimerWatcherHandle(target=target, name=f'test-{i}', runtime=runtime) for i in range(32)]
    stopped = threading.Event()
    shutdown = threading.Thread(target=lambda: (runtime.shutdown(), stopped.set()))
    try:
        for handle in handles:
            handle.start()
        barrier.wait(5)
        async def healthy():
            assert await run_blocking(lambda: 42) == 42
        healthy_handle = TimerWatcherHandle(target=healthy, name='healthy', runtime=runtime)
        started = time.monotonic()
        healthy_handle.start()
        healthy_handle.join(2)
        assert not healthy_handle.is_alive()
        assert time.monotonic() - started < 2
        shutdown.start()
        assert not stopped.wait(1.1)
        assert not finished
        assert all(handle.is_alive() for handle in handles)
    finally:
        release.set()
        if shutdown.ident is not None:
            shutdown.join(10)
        runtime.shutdown()
    assert stopped.is_set()
    assert len(finished) == 32
    assert runtime.snapshot() == {'watchers': 0, 'wait_threads': 0}
    assert not runtime._executor._threads


def test_stop_retains_watcher_lease_until_poll_physically_returns(monkeypatch):
    from lib.scheduler.timer import _loop, _runtime
    runtime = _runtime.TimerWatcherRuntime()
    monkeypatch.setattr(_runtime, '_runtime', runtime)
    row = {'id': 'owned-drain', 'user_id': 7, 'status': 'active',
           'poll_interval': .001, 'max_polls': 1, 'poll_count': 0}
    entered, release, lease_released = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: row)
    monkeypatch.setattr(_loop, 'claim_watcher_lease', lambda *a: 'owned-token')
    monkeypatch.setattr(_loop, 'renew_watcher_lease', lambda *a: True)
    monkeypatch.setattr(_loop, 'release_watcher_lease', lambda *a: lease_released.set())
    def poll(*a, **k):
        entered.set()
        assert release.wait(10)
        return False, 'wait', 0, False, False, '', 'test', [], ''
    monkeypatch.setattr(_loop, 'poll_timer', poll)
    records = []
    monkeypatch.setattr(_loop, '_record_poll', lambda *a, **k: records.append(1))
    try:
        assert _loop.start_timer_loop(row['id'], user_id=7)
        assert entered.wait(5)
        with _loop._timers_lock:
            handle = _loop._active_timers[row['id']]
        handle.stop()
        handle.join(.1)
        assert handle.is_alive()
        assert not lease_released.is_set()
    finally:
        release.set()
        runtime.shutdown()
    assert lease_released.is_set()
    assert not records  # No new poll effects after stop was requested.
    assert row['id'] not in _loop._active_timers


def test_closed_runtime_rejects_start_without_new_threads():
    from lib.scheduler.timer._runtime import TimerWatcherRuntime, TimerWatcherHandle
    runtime = TimerWatcherRuntime()
    assert runtime.snapshot() == {'watchers': 0, 'wait_threads': 0}
    runtime.shutdown()
    async def target():
        pytest.fail('closed runtime ran work')
    handle = TimerWatcherHandle(target=target, name='closed', runtime=runtime)
    with pytest.raises(RuntimeError, match='shutting down'):
        handle.start()
    runtime.shutdown()  # Idempotent joined close.
    assert runtime.snapshot() == {'watchers': 0, 'wait_threads': 0}


def test_task_cancellation_drains_callback_before_cleanup():
    from lib.scheduler.timer._runtime import TimerWatcherRuntime, TimerWatcherHandle, run_blocking
    runtime = TimerWatcherRuntime()
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    tasks = []
    def blocked():
        entered.set()
        assert release.wait(10)
    async def target():
        tasks.append(asyncio.current_task())
        try:
            await run_blocking(blocked)
        finally:
            await run_blocking(cleaned.set, _allow_stopping=True)
    handle = TimerWatcherHandle(target=target, name='cancel-drain', runtime=runtime)
    try:
        handle.start()
        assert entered.wait(5)
        runtime._loop.call_soon_threadsafe(tasks[0].cancel)
        handle.join(.1)
        assert handle.is_alive()
        assert not cleaned.is_set()
    finally:
        release.set()
        runtime.shutdown()
    assert cleaned.is_set()
    assert not handle.is_alive()


def test_shutdown_start_failure_releases_local_registry_and_lease(monkeypatch):
    from lib.scheduler.timer import _loop, _runtime
    runtime = _runtime.TimerWatcherRuntime()
    runtime.shutdown()
    monkeypatch.setattr(_runtime, '_runtime', runtime)
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: {'id': 'closing'})
    monkeypatch.setattr(_loop, 'claim_watcher_lease', lambda *a: 'token')
    released = []
    monkeypatch.setattr(_loop, 'release_watcher_lease', lambda *a: released.append(a))
    with pytest.raises(RuntimeError, match='shutting down'):
        _loop.start_timer_loop('closing', user_id=7)
    assert released == [('closing', 'token')]
    assert 'closing' not in _loop._active_timers

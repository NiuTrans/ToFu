"""Contracts for the resources owned by Hypercorn's serving loop."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from lib.observability import InstrumentedThreadPoolExecutor
from lib.server_loop_runtime import (
    ServingLoopRuntime,
    _executor_idle_seconds,
    _worker_count,
)


pytestmark = pytest.mark.unit


def test_worker_pool_sizes_are_bounded(monkeypatch, caplog):
    monkeypatch.setattr('lib.server_loop_runtime.os.cpu_count', lambda: 256)
    logger = logging.getLogger('test.server-loop-runtime')

    assert _worker_count('POOL', {}, logger) == 16
    assert _worker_count('POOL', {'POOL': '3'}, logger) == 3
    assert _worker_count('POOL', {'POOL': '900'}, logger) == 512
    assert _worker_count('POOL', {'POOL': 'invalid'}, logger) == 16


def test_executor_idle_budget_is_bounded_and_zero_disables(monkeypatch):
    logger = logging.getLogger('test.server-loop-runtime')
    monkeypatch.setattr(
        'lib.server_loop_runtime.deployment_resource_default',
        lambda key, _environment: 600)

    assert _executor_idle_seconds({}, logger) == 600
    assert _executor_idle_seconds(
        {'TOFU_EXECUTOR_IDLE_SECONDS': '1'}, logger) == 60
    assert _executor_idle_seconds(
        {'TOFU_EXECUTOR_IDLE_SECONDS': '999999'}, logger) == 86400
    assert _executor_idle_seconds(
        {'TOFU_EXECUTOR_IDLE_SECONDS': 'bad'}, logger) == 600
    assert _executor_idle_seconds(
        {'TOFU_EXECUTOR_IDLE_SECONDS': '0'}, logger) == 0


def test_cancelled_queued_job_balances_retirement_accounting():
    release = threading.Event()
    started = threading.Event()
    pool = InstrumentedThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix='test-retire-balance',
        metric_pool='test-retire-balance',
        idle_retain_threads=0,
    )
    try:
        first = pool.submit(
            lambda: (started.set(), release.wait(timeout=2)))
        assert started.wait(timeout=1)
        cancelled = pool.submit(lambda: None)
        assert cancelled.cancel() is True
        snapshot = pool.idle_retirement_snapshot(60)
        assert snapshot['active'] == 1
        assert snapshot['pending'] == 0
        assert snapshot['due'] is False
        release.set()
        first.result(timeout=2)
        assert pool.idle_retirement_snapshot(60)['due'] is False
        with pool._lifecycle_lock:
            pool._last_excess_activity = time.monotonic() - 61
        assert pool.idle_retirement_snapshot(60)['due'] is True
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_runtime_stops_reaper_and_detaches_loop_owned_globals():
    import lib.tasks_pkg.spawn as tasks_pkg
    from lib.agent_core.push import hub

    async def exercise():
        loop = asyncio.get_running_loop()
        runtime = ServingLoopRuntime(
            loop,
            threading.Event(),
            environ={
                'TOFU_SYNC_WORKERS': '2',
                'TOFU_AGENT_WORKERS': '2',
                'TOFU_TASK_CLEANUP_INTERVAL': '3600',
            },
        ).start()
        reaper = runtime.reaper_task
        executor_reaper = runtime.executor_reaper_task
        agent_executor = runtime.agent_executor
        owned_started = asyncio.Event()

        async def owned_background():
            owned_started.set()
            await asyncio.Event().wait()

        owned = runtime.create_task(
            owned_background(), name='test-owned-background')
        await owned_started.wait()

        assert reaper is not None and not reaper.done()
        assert executor_reaper is not None and not executor_reaper.done()
        assert tasks_pkg._serving_loop is loop
        assert tasks_pkg._agent_executor is agent_executor
        assert hub._loop is loop

        await runtime.stop()
        await runtime.stop()

        assert reaper.cancelled()
        assert executor_reaper.cancelled()
        assert tasks_pkg._serving_loop is None
        assert tasks_pkg._agent_executor is None
        assert hub._loop is None
        assert agent_executor is not None and agent_executor._shutdown
        assert owned.cancelled()
        hub.stop()

    asyncio.run(exercise())


def test_runtime_retires_burst_threads_without_reducing_capacity():
    import lib.tasks_pkg.spawn as tasks_pkg
    from lib.agent_core.push import hub
    from lib.observability import prometheus_lines, reset_for_tests

    async def exercise():
        reset_for_tests()
        loop = asyncio.get_running_loop()
        runtime = ServingLoopRuntime(
            loop,
            threading.Event(),
            environ={
                'TOFU_SYNC_WORKERS': '4',
                'TOFU_AGENT_WORKERS': '3',
                'TOFU_TASK_CLEANUP_INTERVAL': '0',
                'TOFU_EXECUTOR_IDLE_SECONDS': '0',
            },
        ).start()
        old_sync = runtime.sync_executor
        old_agent = runtime.agent_executor
        assert old_sync is not None and old_agent is not None

        release = threading.Event()
        started = threading.Event()
        started_lock = threading.Lock()
        started_count = 0

        def burst_worker():
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == 7:
                    started.set()
            release.wait(timeout=3)

        futures = [old_sync.submit(burst_worker) for _ in range(4)]
        futures += [old_agent.submit(burst_worker) for _ in range(3)]
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        release.set()
        await asyncio.gather(*(
            asyncio.wrap_future(future) for future in futures))

        with old_sync._lifecycle_lock:
            old_sync._last_excess_activity = time.monotonic() - 61
        with old_agent._lifecycle_lock:
            old_agent._last_excess_activity = time.monotonic() - 61

        retired = runtime._retire_idle_executors(60)
        assert retired == {'sync': 4, 'agent': 3}
        assert runtime.sync_executor is not old_sync
        assert runtime.agent_executor is not old_agent
        assert runtime.sync_executor._max_workers == 4
        assert runtime.agent_executor._max_workers == 3
        assert len(runtime.sync_executor._threads) == 0
        assert len(runtime.agent_executor._threads) == 0
        assert tasks_pkg._agent_executor is runtime.agent_executor
        assert old_sync._shutdown and old_agent._shutdown

        for _ in range(40):
            if all(not thread.is_alive()
                   for thread in (*old_sync._threads, *old_agent._threads)):
                break
            await asyncio.sleep(0.01)
        assert all(not thread.is_alive()
                   for thread in (*old_sync._threads, *old_agent._threads))

        metrics = '\n'.join(prometheus_lines())
        assert 'tofu_executor_idle_retirements_total' in metrics
        assert 'tofu_executor_idle_retired_threads_total' in metrics
        await runtime.stop()
        hub.stop()

    asyncio.run(exercise())


def test_partial_start_failure_rolls_back_agent_owner(monkeypatch):
    import lib.tasks_pkg.spawn as tasks_pkg
    from lib.agent_core.push import hub

    async def exercise():
        loop = asyncio.get_running_loop()
        runtime = ServingLoopRuntime(
            loop,
            threading.Event(),
            environ={
                'TOFU_SYNC_WORKERS': '1',
                'TOFU_AGENT_WORKERS': '1',
                'TOFU_TASK_CLEANUP_INTERVAL': '0',
            },
        )
        monkeypatch.setattr(
            hub, 'set_loop', lambda _loop: (_ for _ in ()).throw(
                RuntimeError('injected push startup failure')))

        with pytest.raises(RuntimeError, match='injected push startup failure'):
            runtime.start()

        assert tasks_pkg._serving_loop is None
        assert tasks_pkg._agent_executor is None
        assert runtime.agent_executor is None
        assert hub._loop is None

    asyncio.run(exercise())

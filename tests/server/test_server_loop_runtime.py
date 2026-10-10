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
    _agent_stuck_replacements,
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


def test_stuck_replacement_budget_is_bounded():
    logger = logging.getLogger('test.server-loop-runtime')

    assert _agent_stuck_replacements(4, {}, logger) == 1
    assert _agent_stuck_replacements(
        4, {'TOFU_AGENT_STUCK_REPLACEMENTS': '99'}, logger) == 4


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
        assert pool.idle_retirement_snapshot(60, now=time.monotonic()+61)['due'] is True
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
                'TOFU_PROCESS_RSS_RECYCLE_MB': '8192',
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
                'TOFU_PROCESS_RSS_RECYCLE_MB': '8192',
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

        original_sync_snapshot = old_sync.idle_retirement_snapshot
        old_sync.idle_retirement_snapshot = lambda idle: original_sync_snapshot(
            idle, now=time.monotonic()+61)
        with old_agent._lifecycle_lock:
            old_agent._last_excess_activity = time.monotonic() - 61

        # Future completion wakes the loop before the carrier finishes its
        # callbacks. Retirement must wait for that physical ownership too.
        for _ in range(200):
            if (old_sync.idle_retirement_snapshot(60)['due'] and
                    old_agent.idle_retirement_snapshot(60)['due']):
                break
            await asyncio.sleep(0.01)
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


def test_asyncio_default_executor_starts_sixty_four_sync_calls_with_one_idle_hint():
    import time
    gate=threading.Barrier(64)
    pool=InstrumentedThreadPoolExecutor(max_workers=1,metric_pool='sync-demand-test',idle_retain_threads=0)
    began=time.monotonic()
    async def run():
        loop=asyncio.get_running_loop()
        loop.set_default_executor(pool)
        def work(index):
            gate.wait(timeout=5)
            return index
        assert await asyncio.gather(*(loop.run_in_executor(None,work,i) for i in range(64)))==list(range(64))
    asyncio.run(run())
    assert len(pool._threads)==0
    print(f'sync-demand asyncio_calls=64 idle_hint=1 settled_ms={(time.monotonic()-began)*1000:.2f} retained_threads=0')


def test_sync_initializer_runs_once_per_carrier_and_keeps_thread_local_state():
    gate=threading.Barrier(8);local=threading.local();initialized=[];lock=threading.Lock()
    def initialize(value):
        local.value=value
        with lock:initialized.append(threading.get_ident())
    with InstrumentedThreadPoolExecutor(max_workers=1,metric_pool='sync-init-test',
            initializer=initialize,initargs=('owned',)) as pool:
        def run():
            gate.wait(timeout=3)
            return local.value
        calls=[pool.submit(run) for _ in range(8)]
        assert [f.result(timeout=5) for f in calls]==['owned']*8
    assert len(initialized)==len(set(initialized))==8
    assert not pool._threads


def test_sync_initializer_failure_settles_and_preserves_cancelled_work():
    from concurrent.futures import CancelledError
    from concurrent.futures.thread import BrokenThreadPool
    entered=threading.Event();release=threading.Event();ran=[]
    def initialize():
        entered.set()
        assert release.wait(timeout=3)
        raise RuntimeError('initializer unavailable')
    pool=InstrumentedThreadPoolExecutor(max_workers=1,metric_pool='sync-broken-test',initializer=initialize)
    try:
        first=pool.submit(lambda:ran.append('first'))
        assert entered.wait(timeout=2)
        second=pool.submit(lambda:ran.append('second'))
        assert second.cancel()
        release.set()
        with pytest.raises(BrokenThreadPool):first.result(timeout=3)
        with pytest.raises(CancelledError):second.result(timeout=3)
        with pytest.raises(BrokenThreadPool):pool.submit(lambda:None)
        assert ran==[]
    finally:
        release.set();pool.shutdown(wait=True)
    assert not pool._threads


def test_sync_compat_interpreter_exit_drains_unawaited_write(tmp_path):
    import subprocess
    import sys
    import os
    from pathlib import Path
    root=Path(__file__).resolve().parents[2];target=tmp_path/'ack.txt'
    script="""import sys,time
from pathlib import Path
from lib.observability import InstrumentedThreadPoolExecutor
pool=InstrumentedThreadPoolExecutor(max_workers=1,metric_pool='sync-exit-test')
def write():
    time.sleep(.05)
    Path(sys.argv[1]).write_text('completed')
pool.submit(write)
"""
    env=dict(os.environ,PYTHONPATH=str(root))
    result=subprocess.run([sys.executable,'-c',script,str(target)],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert target.read_text()=='completed'


def test_compat_constructor_failure_retires_its_observation(monkeypatch):
    import lib.observability as observations
    monkeypatch.setattr(observations,'_SYNC_COMPAT_EXITING',True)
    with pytest.raises(RuntimeError,match='interpreter shutdown'):
        observations.InstrumentedThreadPoolExecutor(max_workers=1,metric_pool='failed-construct')
    assert not any(owner.name=='failed-construct' for owner in observations._EXECUTOR_OBSERVATIONS)

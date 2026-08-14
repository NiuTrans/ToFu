"""Contracts for the resources owned by Hypercorn's serving loop."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from lib.server_loop_runtime import ServingLoopRuntime, _worker_count


pytestmark = pytest.mark.unit


def test_worker_pool_sizes_are_bounded(monkeypatch, caplog):
    monkeypatch.setattr('lib.server_loop_runtime.os.cpu_count', lambda: 256)
    logger = logging.getLogger('test.server-loop-runtime')

    assert _worker_count('POOL', {}, logger) == 16
    assert _worker_count('POOL', {'POOL': '3'}, logger) == 3
    assert _worker_count('POOL', {'POOL': '900'}, logger) == 512
    assert _worker_count('POOL', {'POOL': 'invalid'}, logger) == 16


def test_runtime_stops_reaper_and_detaches_loop_owned_globals():
    import lib.tasks_pkg as tasks_pkg
    from lib.push import hub

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
        agent_executor = runtime.agent_executor
        owned_started = asyncio.Event()

        async def owned_background():
            owned_started.set()
            await asyncio.Event().wait()

        owned = runtime.create_task(
            owned_background(), name='test-owned-background')
        await owned_started.wait()

        assert reaper is not None and not reaper.done()
        assert tasks_pkg._serving_loop is loop
        assert tasks_pkg._agent_executor is agent_executor
        assert hub._loop is loop

        await runtime.stop()
        await runtime.stop()

        assert reaper.cancelled()
        assert tasks_pkg._serving_loop is None
        assert tasks_pkg._agent_executor is None
        assert hub._loop is None
        assert agent_executor is not None and agent_executor._shutdown
        assert owned.cancelled()
        hub.stop()

    asyncio.run(exercise())


def test_partial_start_failure_rolls_back_agent_owner(monkeypatch):
    import lib.tasks_pkg as tasks_pkg
    from lib.push import hub

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


def test_production_entry_delegates_loop_resources_to_runtime_owner():
    source = (Path(__file__).resolve().parents[1]
              / 'lib/serving_loop_lifecycle.py').read_text()

    assert 'ServingLoopRuntime(' in source
    assert "name='tofu.serving-loop.shutdown'" in source
    assert 'InstrumentedThreadPoolExecutor' not in source
    assert 'async def _task_reaper' not in source
    assert "name='tofu-deferred-boot-dispatch'" in source
    assert "name='tofu-orphan-queue-redispatch'" in source

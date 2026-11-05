"""Headless execution reuses application boot and proves joined shutdown."""
import asyncio
from types import SimpleNamespace
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('boot_fails', [False, True])
def test_execution_host_awaits_common_boot_and_physical_shutdown(boot_fails):
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        calls = []
        ready, stopped, draining, joined = (asyncio.Event() for _ in range(4))
        shutdown = threading.Event()
        runner = SimpleNamespace(status='starting', task=None)

        async def bootstrap():
            runner.status = 'failed' if boot_fails else 'ready'

        async def startup():
            calls.append('startup')
            runner.task = asyncio.create_task(bootstrap())

        async def close():
            assert shutdown.is_set()
            calls.append('drain')
            draining.set()
            await joined.wait()
            calls.append('joined')

        app = SimpleNamespace(startup=startup, shutdown=close, extensions={
            'tofu_boot_runner': runner, 'tofu_shutdown_requested': shutdown})
        host = asyncio.create_task(run_execution_lifespan(app, stopped, ready.set))
        if not boot_fails:
            await ready.wait()
            stopped.set()
        await draining.wait()
        assert not host.done()
        joined.set()
        if boot_fails:
            with pytest.raises(RuntimeError, match='bootstrap'):
                await host
            assert not ready.is_set()
        else:
            await host
        assert calls == ['startup', 'drain', 'joined']

    asyncio.run(exercise())


def test_shutdown_before_startup_never_constructs_owned_work():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        stop = asyncio.Event()
        stop.set()
        await run_execution_lifespan(None, stop, lambda: pytest.fail('ready'))

    asyncio.run(exercise())


def test_stop_during_boot_is_owned_and_joined_by_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        started, stopped = asyncio.Event(), asyncio.Event()
        shutdown = threading.Event()
        runner = SimpleNamespace(status='starting', task=None)

        async def startup():
            runner.task = asyncio.create_task(asyncio.Event().wait())
            started.set()

        async def close():
            assert shutdown.is_set()
            runner.task.cancel()
            await asyncio.gather(runner.task, return_exceptions=True)

        app = SimpleNamespace(startup=startup, shutdown=close, extensions={
            'tofu_boot_runner': runner, 'tofu_shutdown_requested': shutdown})
        host = asyncio.create_task(run_execution_lifespan(
            app, stopped, lambda: pytest.fail('ready while interrupted')))
        await started.wait()
        stopped.set()
        await host
        assert runner.task.done()

    asyncio.run(exercise())


def test_stop_during_construction_closes_the_constructed_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        stopped = asyncio.Event()
        stopped.set()
        shutdown = threading.Event()
        closed = []

        async def startup():
            pytest.fail('startup after owner exit')

        async def close():
            assert shutdown.is_set()
            closed.append(True)

        app = SimpleNamespace(startup=startup, shutdown=close,
            extensions={'tofu_shutdown_requested': shutdown})
        await run_execution_lifespan(app, stopped, lambda: pytest.fail('ready'))
        assert closed == [True]

    asyncio.run(exercise())

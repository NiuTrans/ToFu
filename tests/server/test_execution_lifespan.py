"""Headless execution reuses application boot and proves joined shutdown."""
import asyncio
import logging
from types import SimpleNamespace
import threading

import pytest

pytestmark = pytest.mark.unit


def test_control_shutdown_failure_still_closes_application():
    from serverctl_pkg.execution_lifespan import run_execution_lifespan
    async def exercise():
        stopped = asyncio.Event()
        calls = []
        async def startup():
            pass
        async def close_controls():
            calls.append('controls')
            raise RuntimeError('control-close-failed')
        async def close_application():
            calls.append('application')
        app = SimpleNamespace(startup=startup, shutdown=close_application, extensions={
            'tofu_boot_runner': SimpleNamespace(status='ready', task=None),
            'tofu_shutdown_requested': threading.Event()})
        with pytest.raises(RuntimeError, match='control-close-failed'):
            await run_execution_lifespan(app, stopped, stopped.set,
                                          before_shutdown=close_controls)
        assert calls == ['controls', 'application']
    asyncio.run(exercise())


@pytest.mark.parametrize('boot_fails', [False, True])
def test_execution_host_awaits_common_boot_and_physical_shutdown(boot_fails):
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        calls = []
        ready, stopped, draining, joined = (asyncio.Event() for _ in range(4))
        shutdown = threading.Event()
        runner = SimpleNamespace(status='starting', task=None, snapshot=lambda: {})

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


def test_failed_execution_boot_preserves_the_common_phase_error():
    from quart import Quart
    from lib.production_lifecycle import BootPhase, BootRunner
    from serverctl_pkg.execution_lifespan import run_execution_lifespan

    async def exercise():
        app = Quart(__name__)
        shutdown = threading.Event()
        closed = []

        async def refuse_storage():
            raise RuntimeError('storage cutover refused: lib/business.py')

        runner = BootRunner(app, phase_total=1, inline_phases=(),
            background_phases=(BootPhase(1, 'storage_boundary',
                'Exclusive storage boundary', refuse_storage),),
            stop_event=shutdown, logger=logging.getLogger(__name__),
            boot=lambda *args: None)

        async def startup():
            await runner.run_inline()
            runner.start_background()

        async def close():
            assert shutdown.is_set()
            await runner.stop()
            closed.append(True)

        app.extensions.update(tofu_boot_runner=runner, tofu_shutdown_requested=shutdown)
        host = SimpleNamespace(startup=startup, shutdown=close, extensions=app.extensions)
        with pytest.raises(RuntimeError) as failure:
            await run_execution_lifespan(host, asyncio.Event(),
                lambda: pytest.fail('failed bootstrap published readiness'))

        assert runner.status == 'failed'
        assert runner.snapshot()['error'] in str(failure.value)
        assert 'storage cutover refused: lib/business.py' in str(failure.value)
        assert closed == [True]

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


@pytest.mark.parametrize('before_boot', [True, False])
def test_source_recovery_witness_cannot_be_created_after_application_start(
        tmp_path, monkeypatch, before_boot):
    import os
    from serverctl_pkg import execution_host
    from serverctl_pkg.execution_plan import ExecutionSourceChanged

    async def exercise():
        reader, writer = os.pipe()
        shutdown = threading.Event()
        closed = []
        record = {'plan': {}, 'serviceId': 'owned-execution'}
        args = SimpleNamespace(manager_fd=reader, manager_pid=os.getpid(),
            manager_start=1, state_file=str(tmp_path / 'execution.json'))
        monkeypatch.setattr(execution_host, 'process_matches', lambda *args: True)

        async def startup():
            raise ExecutionSourceChanged('source changed after startup entered')

        async def close():
            closed.append(True)

        def construct(stop, plan):
            if before_boot:
                raise ExecutionSourceChanged('source changed before startup')
            return SimpleNamespace(startup=startup, shutdown=close,
                extensions={'tofu_shutdown_requested': shutdown})

        try:
            with pytest.raises(ExecutionSourceChanged):
                await execution_host._run_application(
                    args, record, asyncio.Event(), shutdown, construct)
            assert record.get('failureKind') == ('source_changed_before_boot' if before_boot else None)
            assert closed == ([] if before_boot else [True])
        finally:
            os.close(reader)
            os.close(writer)

    asyncio.run(exercise())

"""Run the common production application lifespan without an HTTP listener.

The application owns boot phases, worker admission and physical cleanup. This
host adapter only connects its stop signal and readiness to that same owner.
"""
import asyncio
import inspect


async def run_execution_lifespan(app, stopped, publish_ready, *, before_shutdown=None):
    if stopped.is_set():
        if app is not None:
            app.extensions['tofu_shutdown_requested'].set()
            await app.shutdown()
        return
    shutdown_requested = app.extensions['tofu_shutdown_requested']

    async def request_shutdown():
        await stopped.wait()
        shutdown_requested.set()

    stopping = asyncio.create_task(request_shutdown(), name='execution-owner-stop')
    try:
        # startup starts the existing background BootRunner. Completion of
        # startup alone does not certify storage, imports or worker readiness.
        await app.startup()
        runner = app.extensions['tofu_boot_runner']
        boot = runner.task
        if boot is not None:
            await asyncio.wait((boot, stopping), return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set():
            return
        if boot is not None:
            await boot
        if runner.status != 'ready':
            detail = runner.snapshot().get('error') or runner.status
            raise RuntimeError(f'Execution bootstrap did not become ready: {detail}')
        ready = publish_ready()
        if inspect.isawaitable(ready):
            await ready
        await stopping
    finally:
        shutdown_requested.set()
        try:
            try:
                if before_shutdown is not None:
                    await before_shutdown()
            finally:
                # Cleanup failure cannot skip the common owner's physical joins.
                await app.shutdown()
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

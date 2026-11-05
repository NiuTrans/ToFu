"""Run the common production application lifespan without an HTTP listener.

The application owns boot phases, worker admission and physical cleanup. This
host adapter only connects its stop signal and readiness to that same owner.
"""
import asyncio


async def run_execution_lifespan(app, stopped, publish_ready):
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
            raise RuntimeError(f'Execution bootstrap did not become ready: {runner.status}')
        publish_ready()
        await stopping
    finally:
        shutdown_requested.set()
        try:
            # A returned handler, timeout or cancelled lease cannot substitute
            # for the common shutdown owner's physical worker joins.
            await app.shutdown()
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

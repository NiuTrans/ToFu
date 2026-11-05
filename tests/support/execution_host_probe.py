"""Disposable application with a real worker whose exit requires SIGUSR1.

Used only by resident-host process tests. The public execution host and manager
lease are unchanged; the application factory supplies a controlled owned thread.
"""
import asyncio
import os
from pathlib import Path
import signal
import threading
from types import SimpleNamespace

release = threading.Event()


def application(shutdown, _plan):
    root = Path(os.environ['TOFU_PROJECT_PATH'])
    entered = threading.Event()
    runner = SimpleNamespace(status='starting', task=None)

    def work():
        (root / 'entered').touch()
        entered.set()
        shutdown.wait()
        (root / 'draining').touch()
        release.wait()
        (root / 'exited').touch()

    worker = threading.Thread(target=work, name='owned-execution-probe')

    async def boot():
        worker.start()
        await asyncio.to_thread(entered.wait)
        runner.status = 'ready'

    async def startup():
        runner.task = asyncio.create_task(boot())

    async def close():
        if worker.ident is not None:
            await asyncio.to_thread(worker.join)

    return SimpleNamespace(startup=startup, shutdown=close, extensions={
        'tofu_shutdown_requested': shutdown, 'tofu_boot_runner': runner})


if __name__ == '__main__':
    from serverctl_pkg.execution_host import main
    signal.signal(signal.SIGUSR1, lambda *_: release.set())
    raise SystemExit(main(application_factory=application))

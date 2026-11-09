"""Disposable execution application whose own close fails, as on 2026-11-06 14:10.

Used only by resident-host process tests: startup becomes ready at once, and the
application shutdown raises the named timeout that the real shutdown stack now
reports when a background owner outlives its join budget.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

CAUSE = ('process background services did not stop within 10s; '
         'still stopping: proactive scheduler')


def application(shutdown, _plan):
    root = Path(os.environ['TOFU_PROJECT_PATH'])
    runner = SimpleNamespace(status='starting', task=None)

    async def boot():
        runner.status = 'ready'

    async def startup():
        runner.task = asyncio.create_task(boot())

    async def close():
        (root / f'close-failed-{os.getpid()}').touch()
        raise TimeoutError(CAUSE)

    return SimpleNamespace(startup=startup, shutdown=close, extensions={
        'tofu_shutdown_requested': shutdown, 'tofu_boot_runner': runner})


if __name__ == '__main__':
    from serverctl_pkg.execution_host import main
    raise SystemExit(main(application_factory=application))

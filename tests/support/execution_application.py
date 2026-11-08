"""Real production HTTP and resident lifespans with private qualification receipts.

Only observation is test-owned. Admission, models, tools, storage, cancellation,
signals and shutdown use the normal application composition.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys


def observe(app):
    directory = Path(os.environ['TOFU_DATA_DIR'])
    role = os.environ['TOFU_PROCESS_ROLE']

    @app.before_serving
    async def ready():
        await asyncio.wait_for(app.extensions['tofu_production_startup_gate'].wait(), 90)
        catalog = [task for task in asyncio.all_tasks()
                   if task.get_name() == 'tofu-post-ready-catalog-sync']
        if catalog:
            await asyncio.wait_for(asyncio.gather(*catalog), 30)
        (directory / (role + '-ready')).write_text(str(os.getpid()))

    @app.after_serving
    async def closed():
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        service = app.extensions.get('tofu_durable_workers')
        value = dict(workers=service.snapshot() if service else None,
            activeTasks=[task['id'] for task in chat_task_runtime.snapshot()
                         if task.get('_activeWorkerCount', 0) or task.get('_terminalPersistencePending')])
        (directory / (role + '-closed.json')).write_text(json.dumps(value))
    return app


def execution_application(shutdown, plan):
    from serverctl_pkg.execution_host import create_application
    return observe(create_application(shutdown, plan))


def http_application(port):
    from server import create_production_app
    from lib.hypercorn_runtime import build_hypercorn_config, serve
    from lib.server.server_shutdown import (
        http_keep_alive_timeout_seconds, request_graceful_shutdown, wait_for_shutdown_trigger)
    app = observe(create_production_app())
    config = build_hypercorn_config('127.0.0.1', port,
        keep_alive_timeout=http_keep_alive_timeout_seconds())
    config.accesslog, config.errorlog = None, '-'

    async def run():
        stop = app.extensions['tofu_shutdown_requested']
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, request_graceful_shutdown, stop)
        await serve(app, config, shutdown_trigger=lambda: wait_for_shutdown_trigger(stop))
    asyncio.run(run())


if __name__ == '__main__':
    if sys.argv[1] == '--http':
        http_application(int(sys.argv[2]))
    else:
        from serverctl_pkg.execution_host import main
        raise SystemExit(main(application_factory=execution_application))

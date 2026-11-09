"""Resident headless application host, bound to its verified lifecycle manager.

This process uses the common application lifespan. It creates no HTTP listener,
task registry or invocation loop. The manager alone holds an inherited pipe's
writer; EOF wakes shutdown on manager exit and manager exec preserves it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import threading

from serverctl_pkg.execution_service import RECORD_FORMAT
from serverctl_pkg.execution_plan import (
    ExecutionSourceChanged, SOURCE_CHANGED_BEFORE_BOOT, require_plan_match,
)
from serverctl_pkg.resident_service import (
    await_published_record, manager_control_matches, process_matches, write_record,
)


def create_application(shutdown_requested, plan):
    from serverctl_pkg.execution_plan import prepare_plan
    project = Path(os.environ['TOFU_PROJECT_PATH'])
    environment = dict(os.environ)
    require_plan_match(plan, prepare_plan(project, environment), stage='after preparation')
    from runtime_guards import load_deployment_configuration
    if load_deployment_configuration().process_role != 'execution':
        raise RuntimeError('Resident execution requires the execution process role')
    import server
    from lib.runtime.pinned_import_source import PinnedImportSource
    pins = PinnedImportSource(server._APPLICATION_IMPORT_ROOTS)
    pins.install()
    try:
        retained = pins.freeze()
        require_plan_match(plan, {**plan, **retained}, stage='while retaining source')
        app = server.create_app()
        server.register_server_runtime_lifecycle(app, shutdown_requested=shutdown_requested)
        # Same source owner as server.py/asgi.py. Preserve __file__ paths and
        # lazy optional imports; finish current sweeps before admitting work.
        pins.join()
        require_plan_match(plan, prepare_plan(project, environment), stage='during application construction')
        app.extensions['tofu_execution_import_source'] = pins
        return app
    except BaseException:
        pins.join()
        pins.uninstall()
        raise


async def _run_application(args, record, stopped, shutdown_requested, application_factory):
    from serverctl_pkg.execution_lifespan import run_execution_lifespan
    loop = asyncio.get_running_loop()
    owner_fd = args.manager_fd
    command_controls = None

    def request_stop():
        loop.remove_reader(owner_fd)
        shutdown_requested.set()
        stopped.set()

    async def ready():
        nonlocal command_controls
        from lib.tasks_pkg.command_control_transport import CommandControlServer
        from lib.tasks_pkg.command_controls import apply_command_control
        command_controls = CommandControlServer(record['serviceId'], apply_command_control)
        record['commandControl'] = await command_controls.start()
        record['state'] = 'ready'
        record['error'] = ''
        write_record(Path(args.state_file), record)

    async def close_controls():
        if command_controls is not None:
            await command_controls.close()

    try:
        loop.add_reader(owner_fd, request_stop)
        if not process_matches(args.manager_pid, args.manager_start):
            request_stop()
        if stopped.is_set():
            return
        # Imports and app construction may touch the source filesystem. Keep
        # owner-exit notification live; asyncio.run joins this construction.
        try:
            app = await asyncio.to_thread(application_factory, shutdown_requested, record['plan'])
        except ExecutionSourceChanged:
            # This catch ends before startup can admit tasks. A later boot or
            # invocation failure cannot issue the same no-entry witness.
            record['failureKind'] = SOURCE_CHANGED_BEFORE_BOOT
            raise
        await run_execution_lifespan(app, stopped, ready, before_shutdown=close_controls)
    finally:
        loop.remove_reader(owner_fd)


def serve(args, *, application_factory=create_application):
    shutdown_requested = threading.Event()
    stopped = asyncio.Event()
    event_loop = None

    def request_stop(*_args):
        shutdown_requested.set()
        if event_loop is None:
            stopped.set()
        else:
            # A raw Python signal handler does not wake an asyncio selector.
            # Enqueue through the loop's self-pipe even on the main thread.
            event_loop.call_soon_threadsafe(stopped.set)

    async def run():
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
        try:
            await _run_application(args, record, stopped, shutdown_requested, application_factory)
        finally:
            event_loop = None

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    record = {}
    try:
        os.fstat(args.lease_fd)
        record = await_published_record(Path(args.state_file), service_id=args.service_id,
            manager_pid=args.manager_pid, manager_start=args.manager_start,
            record_format=RECORD_FORMAT, stopped=shutdown_requested)
        if not record:
            return 1
        if not manager_control_matches(record, args.manager_fd, write=False):
            raise RuntimeError('Execution manager control identity does not match its record')
        asyncio.run(run())
        record['state'], record['error'] = 'stopped', ''
        write_record(Path(args.state_file), record)
        return 0
    except BaseException as error:
        if record:
            record['state'] = 'failed'
            record['error'] = f'{type(error).__name__}: {str(error)[:300]}'
            write_record(Path(args.state_file), record)
        logging.exception('resident execution host failed')
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(args.lease_fd)
        os.close(args.manager_fd)


def main(*, application_factory=create_application):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--plan-output')
    parser.add_argument('--service-id')
    parser.add_argument('--manager-pid', type=int)
    parser.add_argument('--manager-start', type=int)
    parser.add_argument('--manager-fd', type=int)
    parser.add_argument('--lease-fd', type=int)
    parser.add_argument('--state-file')
    args = parser.parse_args()
    if args.plan:
        from serverctl_pkg.execution_plan import prepare_plan
        plan = prepare_plan(Path(os.environ.get('TOFU_PROJECT_PATH', os.getcwd())), dict(os.environ))
        if args.plan_output:
            from lib.json_store import write_json_atomic
            write_json_atomic(args.plan_output, plan, mode=0o600)
        else:
            print(json.dumps(plan))
        return 0
    if any(getattr(args, name) is None for name in (
            'service_id', 'manager_pid', 'manager_start', 'manager_fd', 'lease_fd', 'state_file')):
        parser.error('service ownership arguments are required')
    from serverctl_pkg.runtime_replica import hold_process_leases
    hold_process_leases()
    return serve(args, application_factory=application_factory)


if __name__ == '__main__':
    raise SystemExit(main())

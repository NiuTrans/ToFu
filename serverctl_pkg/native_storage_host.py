"""Manager-owned native runtime; CLI plan and serve entry points.

The application owns no child here. StorageRuntime remains the sole owner of
native restart, crash budgets, shutdown and token rotation. The manager's PID
and start identity survive exec; actual manager death drains this host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time

from serverctl_pkg.native_storage_service import (
    process_matches, read_record, write_record,
)
from serverctl_pkg.restart_budget import NATIVE_DAEMON_STOP_SECONDS

ROOT = Path(__file__).resolve().parents[1]
_TRANSPORT_KEYS = frozenset({
    'TOFU_STORAGE_CONNECTION_FILE', 'TOFU_STORAGE_PARENT_PID', 'TOFU_STORAGE_TOKEN',
    'TOFU_STORAGE_SERVICE_ID', 'TOFU_STORAGE_SERVICE_STATE',
})
_AUTHORITY_KEYS = frozenset({
    'TOFU_PROJECT_PATH', 'TOFU_DATA_DIR', 'TOFU_DATA_LAYOUT',
    'TOFU_DEPLOYMENT_MODE', 'TOFU_PROCESS_ROLE', 'TOFU_PERSONAL_STORAGE_BACKEND',
    'XDG_DATA_HOME', 'LOCALAPPDATA',
})
# Candidate roots for automatic native fastpath placement, which the host
# plans at every daemon launch from the environment it inherited at spawn.
_PLACEMENT_KEYS = frozenset({'TOFU_LOCAL_DIR', 'XDG_STATE_HOME'})


def prepare_plan(supervisor) -> dict:
    """Use the existing backend, transport and binary admission owners."""
    if supervisor._configured_backend() != 'tofudb':
        return {}
    if supervisor._resolve_transport() != 'daemon':
        return {}
    supervisor._resolve_native_daemon_binary()
    binary, digest = supervisor._admitted_native_artifact
    from lib.storage_authority_sql.projection_paths import resolve_search_projection_paths
    from runtime_guards import _persistent_data_path
    projection = resolve_search_projection_paths(_persistent_data_path(dict(os.environ)))
    environment = {
        key: value for key, value in os.environ.items()
        if key not in _TRANSPORT_KEYS and (
            key in _AUTHORITY_KEYS or key in _PLACEMENT_KEYS
            or key.startswith(('TOFU_STORAGE_', 'TOFUDB_', 'TOFU_DB_')))
    }
    identity = hashlib.sha256(json.dumps(environment, sort_keys=True).encode())
    # Changes to the host's lifecycle implementation, including every module
    # it runs while launching the daemon, also require a drained replacement.
    # App-only changes leave this identity and the engine intact.
    for relative in (
        'serverctl_pkg/native_storage_host.py', 'serverctl_pkg/native_storage_service.py',
        'lib/storage/runtime.py', 'lib/storage/supervisor.py', 'lib/storage/connection_file.py',
        'lib/storage/admitted_engine.py',
        'lib/storage_authority_sql/native_launch.py', 'lib/storage_authority_sql/preflight.py',
        'contracts/native_storage_connection_v2.json',
        'lib/storage_authority_sql/native_fastpath_auto.py', 'serverctl_pkg/local_placement.py',
        'lib/storage_authority_sql/projection_paths.py',
        'lib/storage/startup_budget.py', 'lib/storage/generated_v2_semantics.py',
    ):
        identity.update(relative.encode())
        identity.update((ROOT / relative).read_bytes())
    return {'binary': str(binary), 'binarySha256': digest,
            'configurationSha256': identity.hexdigest(), 'searchProjectionDirectory': str(projection.directory)}


def serve(args) -> int:
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    state_path = Path(args.state_file)
    owner_alive = lambda: process_matches(args.manager_pid, args.manager_start)
    # Parent publishes PID/start identity after Popen. The inherited exclusive
    # service lease closes the gap before this record exists.
    deadline = time.monotonic() + 10.0
    record = {}
    while owner_alive() and not stopped.is_set() and time.monotonic() < deadline:
        record = read_record(state_path)
        if record.get('serviceId') == args.service_id and record.get('pid') == os.getpid():
            break
        stopped.wait(0.05)
    else:
        return 1
    if (record.get('managerPid') != args.manager_pid
            or record.get('managerStartTime') != args.manager_start):
        return 1
    # Keep the inherited lock for the entire runtime, including startup/close.
    os.fstat(args.lease_fd)
    os.environ['TOFU_STORAGE_SERVICE_ID'] = args.service_id
    os.environ['TOFU_STORAGE_SERVICE_STATE'] = str(state_path)
    os.environ.pop('TOFU_STORAGE_CONNECTION_FILE', None)
    from lib.storage.connection_file import remove_connection_file, write_connection_document
    from lib.storage.runtime import StorageRuntime
    from lib.storage.supervisor import StorageSupervisor

    # Preserve the configured data-layout authority. project_root is a scratch
    # override in StorageSupervisor and must not replace TOFU_DATA_DIR here.
    supervisor = StorageSupervisor(connection_file='')
    runtime = StorageRuntime(supervisor)
    connection = Path(record['connectionFile'])
    startup_done = threading.Event()
    errors = []
    token = ''

    def start():
        try:
            if prepare_plan(supervisor) != record['plan']:
                raise RuntimeError('native storage configuration changed after preparation')
            if not stopped.is_set() and owner_alive():
                runtime.start()
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {str(exc)[:300]}')
            logging.exception('resident native startup failed')
        finally:
            startup_done.set()

    starter = threading.Thread(target=start, name='native-storage-start')
    starter.start()
    published = None
    try:
        while not stopped.is_set() and owner_alive():
            status = runtime.status()
            state = 'failed' if errors or status.get('degraded') else status['state']
            if status.get('ready') and not errors:
                try:
                    document = supervisor.native_connection_document(service_id=args.service_id)
                except RuntimeError:
                    # Crash between the status read and export: do not publish
                    # a stale endpoint; the existing runtime owns its retry.
                    stopped.wait(0.05)
                    continue
                if document != published:
                    write_connection_document(connection, document)
                    token, published = document['token'], document
                    logging.info('resident native ready pid=%s service=%s', document['pid'], args.service_id)
            elif token:
                remove_connection_file(connection, token=token)
                token, published = '', None
            if record.get('state') != state:
                record['state'] = state
                record['error'] = errors[-1] if errors else status.get('last_error', '')
                record['nativePid'] = status.get('pid')
                write_record(state_path, record)
            stopped.wait(0.1)
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {str(exc)[:300]}')
        logging.exception('resident native host failed')
        raise
    finally:
        normal_stop = stopped.is_set() or not owner_alive()
        stopped.set()
        if token:
            remove_connection_file(connection, token=token)
        # start() performs admission before publishing its child; wait until
        # that boundary so stop cannot be cleared by a late runtime.start().
        while not startup_done.is_set() and supervisor.status().get('pid') is None:
            startup_done.wait(0.05)
        runtime.stop(timeout=NATIVE_DAEMON_STOP_SECONDS)
        starter.join(timeout=5.0)
        if starter.is_alive():
            raise RuntimeError('native storage startup did not join shutdown')
        record['state'] = 'stopped' if normal_stop else 'failed'
        record['error'] = errors[-1] if errors else ''
        write_record(state_path, record)
        os.close(args.lease_fd)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--plan-output')
    parser.add_argument('--service-id')
    parser.add_argument('--manager-pid', type=int)
    parser.add_argument('--manager-start', type=int)
    parser.add_argument('--lease-fd', type=int)
    parser.add_argument('--state-file')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.plan:
        from lib.storage.supervisor import StorageSupervisor
        supervisor = StorageSupervisor(connection_file='')
        plan = prepare_plan(supervisor)
        if args.plan_output:
            from lib.json_store import write_json_atomic
            write_json_atomic(args.plan_output, plan, mode=0o600)
        else:
            print(json.dumps(plan))
        return 0
    if any(getattr(args, name) is None for name in (
            'service_id', 'manager_pid', 'manager_start', 'lease_fd', 'state_file')):
        parser.error('service ownership arguments are required')
    return serve(args)


if __name__ == '__main__':
    raise SystemExit(main())

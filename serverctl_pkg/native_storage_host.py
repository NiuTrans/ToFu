"""Manager-owned selected storage runtime; CLI plan and serve entry points.

The application owns no child here. StorageRuntime remains the sole owner of
storage restart, crash budgets, shutdown and token rotation. The manager's PID
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
import select
import threading

from serverctl_pkg.native_storage_service import (
    RECORD_FORMAT, write_record,
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
    'TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE',
    'XDG_DATA_HOME', 'LOCALAPPDATA',
})
# Candidate roots for automatic native fastpath placement, which the host
# plans at every daemon launch from the environment it inherited at spawn.
_PLACEMENT_KEYS = frozenset({'TOFU_LOCAL_DIR', 'XDG_STATE_HOME'})


def prepare_plan(supervisor) -> dict:
    """Use the existing backend, transport and binary admission owners."""
    backend = supervisor._configured_backend()
    artifact = {}
    if backend == 'tofudb':
        if supervisor._resolve_transport() != 'daemon':
            return {}
        supervisor._resolve_native_daemon_binary()
        binary, digest = supervisor._admitted_native_artifact
        artifact = {'binary': str(binary), 'binarySha256': digest}
    elif backend not in {'sqlite', 'postgres'}:
        raise RuntimeError('Unsupported resident storage authority')
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
    if backend != 'tofudb':
        from lib.runtime.pinned_import_source import _importable_sources
        sources = {ROOT / 'runtime_guards.py', ROOT / 'runtime_path_policy.py'}
        for relative in ('lib/storage', 'lib/storage_authority_sql'):
            sources.update(Path(path) for path in _importable_sources(
                str(ROOT / relative), recurse=True, ignore_errors=False))
        for source in sorted(sources):
            identity.update(str(source.relative_to(ROOT)).encode())
            identity.update(source.read_bytes())
        # Secret-file replacement is an explicit authority configuration
        # change. Only its hash reaches the manager's private plan.
        for key in ('TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE',
                    'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'):
            if environment.get(key):
                identity.update(Path(environment[key]).read_bytes())
    # Changes to the host's lifecycle implementation, including every module
    # it runs while launching the daemon, also require a drained replacement.
    # App-only changes leave this identity and the engine intact.
    for relative in (
        'runtime_guards.py', 'runtime_path_policy.py',
        'bootstrap_pkg/process_roles.py', 'bootstrap_pkg/resource_policy_generated.py',
        'serverctl_pkg/native_storage_host.py', 'serverctl_pkg/native_storage_service.py',
        'serverctl_pkg/resident_service.py',
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
    return {'backend': backend, **artifact,
            'configurationSha256': identity.hexdigest(), 'searchProjectionDirectory': str(projection.directory)}


def _new_supervisor():
    from lib.storage.supervisor import StorageSupervisor
    return StorageSupervisor(connection_file='')


def serve(args, *, supervisor_factory=_new_supervisor) -> int:
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    state_path = Path(args.state_file)
    # Parent publishes PID/start identity after Popen. The inherited exclusive
    # service lease closes the gap before this record exists.
    from serverctl_pkg.resident_service import await_published_record, manager_control_matches
    record = await_published_record(state_path, service_id=args.service_id,
        manager_pid=args.manager_pid, manager_start=args.manager_start,
        record_format=RECORD_FORMAT, stopped=stopped)
    if not record:
        return 1
    if not manager_control_matches(record, args.manager_fd, write=False):
        raise RuntimeError('Native manager control identity does not match its record')
    owner_alive = lambda: not select.select([args.manager_fd], [], [], 0)[0]
    # Keep the inherited lock for the entire runtime, including startup/close.
    os.fstat(args.lease_fd)
    os.environ['TOFU_STORAGE_SERVICE_ID'] = args.service_id
    os.environ['TOFU_STORAGE_SERVICE_STATE'] = str(state_path)
    os.environ.pop('TOFU_STORAGE_CONNECTION_FILE', None)
    from lib.storage.connection_file import remove_connection_file, write_connection_document
    from lib.storage.runtime import StorageRuntime

    # Preserve the configured data-layout authority. project_root is a scratch
    # override in StorageSupervisor and must not replace TOFU_DATA_DIR here.
    supervisor = supervisor_factory()
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
                    document = supervisor.connection_document(service_id=args.service_id)
                except RuntimeError:
                    # Crash between the status read and export: do not publish
                    # a stale endpoint; the existing runtime owns its retry.
                    stopped.wait(0.05)
                    continue
                if document != published:
                    write_connection_document(connection, document)
                    token, published = document['token'], document
                    logging.info('resident storage ready backend=%s pid=%s service=%s',
                                 document['backend'], status.get('pid'), args.service_id)
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
        stop_error = ''
        try:
            if token:
                remove_connection_file(connection, token=token)
            # start() performs admission before publishing its child; wait until
            # that boundary so stop cannot be cleared by a late runtime.start().
            while not startup_done.is_set() and supervisor.status().get('pid') is None:
                startup_done.wait(0.05)
            try:
                runtime.stop(timeout=NATIVE_DAEMON_STOP_SECONDS)
            except Exception as exc:
                # The terminal record is the evidence the manager verifies
                # before replacing this host. A failed stop must still publish
                # it: otherwise the record keeps the stale 'ready' state and
                # the manager can only report an unconfirmed clean close, then
                # latch the crash loop. The error stays in the record so the
                # next explicit restart sees why the drain failed.
                stop_error = f'{type(exc).__name__}: {str(exc)[:300]}'
                errors.append(stop_error)
                logging.exception('resident native storage shutdown failed')
            starter.join(timeout=5.0)
            if starter.is_alive():
                stop_error = 'native storage startup did not join shutdown'
                errors.append(stop_error)
            record['state'] = (
                'stopped' if normal_stop and not stop_error else 'failed')
            record['error'] = errors[-1] if errors else ''
            write_record(state_path, record)
        finally:
            for descriptor in (args.lease_fd, args.manager_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return 0


def main(*, supervisor_factory=_new_supervisor) -> int:
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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.plan:
        supervisor = supervisor_factory()
        plan = prepare_plan(supervisor)
        if args.plan_output:
            from lib.json_store import write_json_atomic
            write_json_atomic(args.plan_output, plan, mode=0o600)
        else:
            print(json.dumps(plan))
        return 0
    if any(getattr(args, name) is None for name in (
            'service_id', 'manager_pid', 'manager_start', 'manager_fd', 'lease_fd', 'state_file')):
        parser.error('service ownership arguments are required')
    return serve(args, supervisor_factory=supervisor_factory)


if __name__ == '__main__':
    raise SystemExit(main())

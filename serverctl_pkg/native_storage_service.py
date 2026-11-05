"""Native storage policy on the shared resident-process lifecycle owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS
from serverctl_pkg import resident_service as resident
from serverctl_pkg.resident_service import (
    ResidentService, acquire_service_lock, process_matches, write_record,
)

HOST_MODULE = 'serverctl_pkg.native_storage_host'
RECORD_FORMAT = 'tofu.native-storage-service/v1'

__all__ = ['NativeStorageService', 'acquire_service_lock', 'process_matches',
           'write_record', 'read_record', 'host_matches', 'serves_web_owner']


def read_record(path: Path) -> dict:
    return resident.read_record(path, record_format=RECORD_FORMAT)


def host_matches(record: dict) -> bool:
    return resident.host_matches(record, host_module=HOST_MODULE)


def serves_web_owner(state_path: Path, service_id: str, host_pid: int, web_pid: int) -> bool:
    """Fence native respawn against a Web owner of another service/project."""
    record = read_record(state_path)
    if (record.get('serviceId') != service_id or record.get('pid') != host_pid
            or not host_matches(record)):
        return False
    if not process_matches(record.get('managerPid'), record.get('managerStartTime')):
        return False
    try:
        raw = Path(f'/proc/{web_pid}/environ').read_bytes()
        env = dict(part.split(b'=', 1) for part in raw.split(b'\0') if b'=' in part)
        return (env.get(b'TOFU_STORAGE_CONNECTION_FILE') == record['connectionFile'].encode()
                and env.get(b'TOFU_PROJECT_PATH') == record['projectPath'].encode())
    except (OSError, KeyError, ValueError):
        return False


class NativeStorageService(ResidentService):
    service_name = 'native storage'
    state_name = 'native-storage-service'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

    def __init__(self, project: str, python: str):
        super().__init__(project, python)
        self.connection_path = self.project / 'data/native-storage-connection.json'

    def identity_fields(self) -> dict:
        return {'connectionFile': str(self.connection_path)}

    def result_path(self) -> str:
        return str(self.connection_path)

    def host_matches(self, record: dict) -> bool:
        return host_matches(record)

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        env.pop('TOFU_STORAGE_CONNECTION_FILE', None)
        return env

    def shared_environment(self, environment: dict[str, str]) -> dict[str, str]:
        """Independent managed clients need the selected authority's RPC owner.

        This changes only automatic native transport selection. Explicit
        embedded topology and the configured storage backend remain intact.
        """
        env = dict(environment)
        if (env.get('TOFUDB_TRANSPORT') or 'auto').strip().lower() == 'auto':
            env['TOFUDB_TRANSPORT'] = 'daemon'
        return env

    def stop(self, timeout: float = NATIVE_SERVICE_STOP_SECONDS) -> None:
        super().stop(timeout)

    def prepare(self, environment: dict[str, str]) -> dict | None:
        # Other projects and explicitly supplied co-containers keep their owner.
        if (not sys.platform.startswith('linux') or environment.get('TOFU_STORAGE_CONNECTION_FILE')
                or not (self.project / 'contracts/native_storage_connection_v2.json').is_file()):
            return None
        from serverctl_pkg.native_engine import _run
        # _run owns bounded descendant cancellation and a combined diagnostic
        # stream. Keep the structured result separate from those diagnostics.
        with tempfile.TemporaryDirectory(prefix='tofu-native-plan-') as directory:
            result = Path(directory) / 'plan.json'
            _run([self.python, '-m', self.host_module, '--plan', '--plan-output', str(result)],
                 cwd=self.project, environment=environment, timeout=NATIVE_SERVICE_PREPARATION_SECONDS)
            plan = json.loads(result.read_text(encoding='utf-8'))
        return plan or None

    def owns_lease(self, lease: dict) -> bool:
        record = self.record()
        if (not lease.get('held') or not lease.get('holderVerified')
                or lease.get('kind') != 'storage_authority_sql' or not host_matches(record)):
            return False
        if (record.get('managerPid') != os.getpid()
                or not process_matches(record['managerPid'], record.get('managerStartTime'))):
            return False
        try:
            raw = Path(f'/proc/{int(lease["pid"])}/stat').read_text()
            parent = int(raw[raw.rfind(')') + 2:].split()[1])
            return parent == record['pid']
        except (OSError, KeyError, ValueError, IndexError, TypeError):
            return False

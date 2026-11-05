"""Lifecycle-manager ownership of a resident native storage host.

Only standard-library dependencies are loaded in the manager. The host owns
StorageRuntime, its crash budget and the native child's EOF channel. Private
records are reconstructible process identities, never authority or readiness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS

HOST_MODULE = 'serverctl_pkg.native_storage_host'
RECORD_FORMAT = 'tofu.native-storage-service/v1'


def read_record(path: Path) -> dict:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or not 0 < info.st_size <= 65536):
            return {}
        record = json.loads(path.read_text(encoding='utf-8'))
        return record if record.get('format') == RECORD_FORMAT else {}
    except (OSError, ValueError, AttributeError):
        return {}


def process_matches(pid: int, ticks: int) -> bool:
    from server_manager import pid_is_alive, proc_start_ticks
    return (type(pid) is int and type(ticks) is int and pid > 0 and ticks > 0
            and pid_is_alive(pid) and proc_start_ticks(pid) == ticks)


def host_matches(record: dict) -> bool:
    if record.get('host') != socket.gethostname():
        return False
    if not process_matches(record.get('pid'), record.get('processStartTime')):
        return False
    try:
        args = Path(f'/proc/{record["pid"]}/cmdline').read_bytes().split(b'\0')
        index = args.index(b'--service-id')
        return (HOST_MODULE.encode() in args
                and args[index + 1].decode() == record.get('serviceId'))
    except (OSError, ValueError, IndexError, UnicodeError):
        return False


def write_record(path: Path, record: dict) -> None:
    from lib.json_store import write_json_atomic
    write_json_atomic(str(path), record, mode=0o600)


def acquire_service_lock(path: Path) -> int:
    """Hold the host lease through PID publication and final native shutdown."""
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('native storage service lock is not private')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('native storage owner is still draining') from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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


class NativeStorageService:
    """One service per project, retained across worker restart and manager exec."""

    def __init__(self, project: str, python: str):
        self.project = Path(project).resolve()
        self.python = python
        self.state_path = self.project / 'data/native-storage-service.json'
        self.connection_path = self.project / 'data/native-storage-connection.json'
        self.lock_path = self.project / 'data/.native-storage-service.lock'
        self._process: subprocess.Popen | None = None

    def record(self) -> dict:
        record = read_record(self.state_path)
        if (record.get('projectPath') != str(self.project)
                or record.get('connectionFile') != str(self.connection_path)):
            return {}
        return record

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
            _run([self.python, '-m', HOST_MODULE, '--plan', '--plan-output', str(result)],
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

    def needs_replacement(self, plan: dict | None, *, reset_failed: bool = False) -> bool:
        record = self.record()
        return bool(record and (not plan or record.get('plan') != plan
                                or (reset_failed and record.get('state') == 'failed')))

    def ensure(self, plan: dict, environment: dict[str, str]) -> str:
        record = self.record()
        if record.get('host') not in (None, socket.gethostname()):
            raise RuntimeError('native storage service belongs to another host')
        if host_matches(record):
            if (record.get('managerPid') != os.getpid()
                    or not process_matches(record['managerPid'], record.get('managerStartTime'))
                    or record.get('plan') != plan or record.get('state') == 'failed'):
                raise RuntimeError('native storage replacement requires a drained worker')
            return str(self.connection_path)
        if record.get('state') == 'failed':
            raise RuntimeError('native storage service failed; explicit restart required')
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = acquire_service_lock(self.lock_path)
        try:
            from server_manager import proc_start_ticks
            service_id = uuid.uuid4().hex
            manager_ticks = proc_start_ticks(os.getpid())
            if manager_ticks is None:
                raise RuntimeError('native storage requires verifiable manager process identity')
            env = dict(environment)
            env.pop('TOFU_STORAGE_CONNECTION_FILE', None)
            # Reuse the manager's rotated console family; no unbounded new log.
            log = self.project / 'logs/server-console.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('ab') as output:
                process = subprocess.Popen([
                    self.python, '-m', HOST_MODULE, '--service-id', service_id,
                    '--manager-pid', str(os.getpid()), '--manager-start', str(manager_ticks),
                    '--lease-fd', str(descriptor), '--state-file', str(self.state_path),
                ], cwd=self.project, env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, pass_fds=(descriptor,))
            self._process = process
            record = {
                'format': RECORD_FORMAT, 'host': socket.gethostname(),
                'projectPath': str(self.project), 'connectionFile': str(self.connection_path),
                'serviceId': service_id, 'pid': process.pid,
                'processStartTime': proc_start_ticks(process.pid),
                'managerPid': os.getpid(), 'managerStartTime': manager_ticks,
                'plan': plan, 'state': 'starting',
            }
            try:
                write_record(self.state_path, record)
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise
            return str(self.connection_path)
        finally:
            os.close(descriptor)

    def stop(self, timeout: float = NATIVE_SERVICE_STOP_SECONDS) -> None:
        record = self.record()
        if record.get('host') not in (None, socket.gethostname()):
            raise RuntimeError('native storage service belongs to another host')
        if host_matches(record):
            # A different manager must first observe its predecessor's exit.
            if (record.get('managerPid') != os.getpid()
                    and process_matches(record.get('managerPid'), record.get('managerStartTime'))):
                raise RuntimeError('native storage service belongs to another live manager')
            os.kill(record['pid'], signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while host_matches(record) and time.monotonic() < deadline:
                time.sleep(0.05)
            if host_matches(record):
                raise RuntimeError('native storage service has not finished draining')
            stopped = self.record()
            if (stopped.get('serviceId') != record.get('serviceId')
                    or stopped.get('state') != 'stopped'):
                detail = str(stopped.get('error') or 'clean close was not confirmed')[:300]
                raise RuntimeError(f'native storage shutdown failed: {detail}')
        if self._process is not None and self._process.poll() is not None:
            self._process.wait()
            self._process = None
        # Missing/corrupt PID metadata cannot certify release of a live host.
        if self.lock_path.exists():
            descriptor = acquire_service_lock(self.lock_path)
            try:
                if record and self.record().get('serviceId') == record.get('serviceId'):
                    self.state_path.unlink(missing_ok=True)
            finally:
                os.close(descriptor)

"""Manager ownership of resident process identity, launch lease and joined stop.

Entry: ResidentService. Resource policies supply preparation and launch ports;
private state is reconstructible process evidence, never task/storage authority.
Only standard-library dependencies load in the manager.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time
import uuid

def read_record(path: Path, *, record_format: str) -> dict:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or not 0 < info.st_size <= 65536):
            return {}
        record = json.loads(path.read_text(encoding='utf-8'))
        return record if record.get('format') == record_format else {}
    except (OSError, ValueError, AttributeError):
        return {}


def process_matches(pid: int, ticks: int) -> bool:
    from server_manager import pid_is_alive, proc_start_ticks
    return (type(pid) is int and type(ticks) is int and pid > 0 and ticks > 0
            and pid_is_alive(pid) and proc_start_ticks(pid) == ticks)


def manager_control_matches(record: dict, descriptor: int, *, write: bool) -> bool:
    """Verify an inherited endpoint against its private kernel pipe identity."""
    import fcntl
    control = record.get('managerControl')
    if not isinstance(control, dict) or type(descriptor) is not int or descriptor < 0:
        return False
    try:
        info = os.fstat(descriptor)
        mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        return (stat.S_ISFIFO(info.st_mode) and info.st_dev == control.get('device')
                and info.st_ino == control.get('inode')
                and mode == (os.O_WRONLY if write else os.O_RDONLY))
    except OSError:
        return False


def close_manager_control(record: dict) -> None:
    """Only this manager may close the exact writer it published, including after exec."""
    if (record.get('managerPid') != os.getpid()
            or not process_matches(os.getpid(), record.get('managerStartTime'))):
        return
    control = record.get('managerControl') or {}
    if not isinstance(control, dict):
        return
    descriptor = control.get('fd')
    if manager_control_matches(record, descriptor, write=True):
        os.close(descriptor)


def host_matches(record: dict, *, host_module: str) -> bool:
    if record.get('host') != socket.gethostname():
        return False
    if not process_matches(record.get('pid'), record.get('processStartTime')):
        return False
    try:
        args = Path(f'/proc/{record["pid"]}/cmdline').read_bytes().split(b'\0')
        index = args.index(b'--service-id')
        return (host_module.encode() in args
                and args[index + 1].decode() == record.get('serviceId'))
    except (OSError, ValueError, IndexError, UnicodeError):
        return False


def write_record(path: Path, record: dict) -> None:
    from lib.json_store import write_json_atomic
    write_json_atomic(str(path), record, mode=0o600)


def await_published_record(state_path: Path, *, service_id: str, manager_pid: int,
                           manager_start: int, record_format: str, stopped) -> dict:
    """Join the existing post-Popen publication handshake before owning work.

    The original storage-host protocol grants ten seconds for the parent's
    private PID record. The inherited lease protects that publication gap.
    """
    deadline = time.monotonic() + 10.0
    while (process_matches(manager_pid, manager_start) and not stopped.is_set()
           and time.monotonic() < deadline):
        record = read_record(state_path, record_format=record_format)
        if record.get('serviceId') == service_id and record.get('pid') == os.getpid():
            if (record.get('managerPid') == manager_pid
                    and record.get('managerStartTime') == manager_start):
                return record
            return {}
        stopped.wait(0.05)
    return {}


def acquire_service_lock(path: Path) -> int:
    """Hold the host lease through PID publication and final owned-runtime shutdown."""
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('resident service lock is not private')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('resident service owner is still draining') from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class ResidentService:
    """One process lease retained across API restart and manager exec.

    Subclasses declare identity, launch module and resource-specific preparation.
    This owner alone publishes PID evidence, signals and proves physical drain.
    """
    service_name: str
    state_name: str
    record_format: str
    host_module: str

    def __init__(self, project: str, python: str):
        self.project = Path(project).resolve()
        self.python = python
        self.state_path = self.project / 'data' / f'{self.state_name}.json'
        self.lock_path = self.project / 'data' / f'.{self.state_name}.lock'
        self._process: subprocess.Popen | None = None

    def identity_fields(self) -> dict:
        return {}

    def result_path(self) -> str:
        return str(self.state_path)

    def host_matches(self, record: dict) -> bool:
        return host_matches(record, host_module=self.host_module)

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        return dict(environment)

    def working_directory(self, plan: dict) -> Path:
        return self.project

    def record(self) -> dict:
        record = read_record(self.state_path, record_format=self.record_format)
        if (record.get('projectPath') != str(self.project)
                or any(record.get(key) != value for key, value in self.identity_fields().items())):
            return {}
        return record

    def needs_replacement(self, plan: dict | None, *, reset_failed: bool = False) -> bool:
        record = self.record()
        return bool(record and (not plan or record.get('plan') != plan
                                or (reset_failed and record.get('state') == 'failed')))

    def ensure(self, plan: dict, environment: dict[str, str]) -> str:
        record = self.record()
        if record.get('host') not in (None, socket.gethostname()):
            raise RuntimeError(f'{self.service_name} service belongs to another host')
        if self.host_matches(record):
            if (record.get('managerPid') != os.getpid()
                    or not process_matches(record['managerPid'], record.get('managerStartTime'))
                    or record.get('plan') != plan or record.get('state') == 'failed'):
                raise RuntimeError(f'{self.service_name} replacement requires a drained worker')
            control = record.get('managerControl')
            if control is not None and (not isinstance(control, dict)
                    or not manager_control_matches(record, control.get('fd'), write=True)):
                raise RuntimeError(f'{self.service_name} manager control was lost; drain the host')
            return self.result_path()
        if record.get('state') == 'failed':
            raise RuntimeError(f'{self.service_name} service failed; explicit restart required')
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = acquire_service_lock(self.lock_path)
        manager_read = manager_write = None
        published = False
        try:
            close_manager_control(record)
            manager_read, manager_write = os.pipe()
            # Only the manager keeps the writer. It survives manager exec;
            # Popen's close_fds excludes it from every child.
            os.set_inheritable(manager_write, True)
            control_info = os.fstat(manager_write)
            from server_manager import proc_start_ticks
            service_id = uuid.uuid4().hex
            manager_ticks = proc_start_ticks(os.getpid())
            if manager_ticks is None:
                raise RuntimeError(f'{self.service_name} requires verifiable manager process identity')
            env = self.process_environment(environment)
            # Reuse the manager's rotated console family; no unbounded new log.
            log = self.project / 'logs/server-console.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('ab') as output:
                process = subprocess.Popen([
                    self.python, '-m', self.host_module, '--service-id', service_id,
                    '--manager-pid', str(os.getpid()), '--manager-start', str(manager_ticks),
                    '--manager-fd', str(manager_read),
                    '--lease-fd', str(descriptor), '--state-file', str(self.state_path),
                ], cwd=self.working_directory(plan), env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True, pass_fds=(descriptor, manager_read))
            self._process = process
            record = {
                'format': self.record_format, 'host': socket.gethostname(),
                'projectPath': str(self.project), **self.identity_fields(),
                'serviceId': service_id, 'pid': process.pid,
                'processStartTime': proc_start_ticks(process.pid),
                'managerPid': os.getpid(), 'managerStartTime': manager_ticks,
                'managerControl': {'fd': manager_write, 'device': control_info.st_dev,
                                   'inode': control_info.st_ino},
                'plan': plan, 'state': 'starting',
            }
            try:
                write_record(self.state_path, record)
                published = True
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise
            return self.result_path()
        finally:
            os.close(descriptor)
            if manager_read is not None:
                os.close(manager_read)
            if manager_write is not None and not published:
                os.close(manager_write)

    def stop(self, timeout: float) -> None:
        record = self.record()
        if record.get('host') not in (None, socket.gethostname()):
            raise RuntimeError(f'{self.service_name} service belongs to another host')
        if self.host_matches(record):
            # A different manager must first observe its predecessor's exit.
            if (record.get('managerPid') != os.getpid()
                    and process_matches(record.get('managerPid'), record.get('managerStartTime'))):
                raise RuntimeError(f'{self.service_name} service belongs to another live manager')
            os.kill(record['pid'], signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while self.host_matches(record) and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.host_matches(record):
                raise RuntimeError(f'{self.service_name} service has not finished draining')
            stopped = self.record()
            if (stopped.get('serviceId') != record.get('serviceId')
                    or stopped.get('state') != 'stopped'):
                detail = str(stopped.get('error') or 'clean close was not confirmed')[:300]
                raise RuntimeError(f'{self.service_name} shutdown failed: {detail}')
        if self._process is not None and self._process.poll() is not None:
            self._process.wait()
            self._process = None
        # Missing/corrupt PID metadata cannot certify release of a live host.
        if self.lock_path.exists():
            descriptor = acquire_service_lock(self.lock_path)
            try:
                if record and self.record().get('serviceId') == record.get('serviceId'):
                    close_manager_control(record)
                    self.state_path.unlink(missing_ok=True)
            finally:
                os.close(descriptor)

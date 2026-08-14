#!/usr/bin/env python3
"""Single-owner lifecycle state machine for the Tofu server worker.

This module intentionally uses only the Python standard library.  The manager
must remain observable and able to stop/recover the worker even when the
application dependency graph or database cannot be imported.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


STATE_VERSION = 1
DEFAULT_SERVER_PORT = 15000
DEFAULT_MONITOR_INTERVAL = 5.0
DEFAULT_BOOT_GRACE = 180.0
DEFAULT_WEDGE_STALE = 180.0
DEFAULT_WEDGE_STREAK = 120.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_FAILURE_WINDOW = 120.0
DEFAULT_FAILURE_HISTORY_KEEP = 50
DEFAULT_WORKER_RSS_RECYCLE_MB = 8192.0
DEFAULT_WORKER_RSS_CGROUP_FRACTION = 0.70
MIB = 1024 * 1024
CGROUP_OOM_EVENT_PATHS = (
    '/sys/fs/cgroup/memory.events',
    '/sys/fs/cgroup/memory/memory.oom_control',
)
CGROUP_MEMORY_LIMIT_PATHS = (
    '/sys/fs/cgroup/memory.max',
    '/sys/fs/cgroup/memory/memory.limit_in_bytes',
)
SERVER_ENV_KEYS = frozenset({
    'PORT', 'BIND_HOST', 'TOFU_TLS', 'TLS_CERTFILE', 'TLS_KEYFILE',
    'TOFU_PROCESS_RSS_RECYCLE_MB',
})

logger = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ''


def cgroup_oom_kill_count() -> int | None:
    """Read the shared cgroup's cumulative OOM-kill counter, if exposed."""
    for path in CGROUP_OOM_EVENT_PATHS:
        try:
            lines = Path(path).read_text(encoding='utf-8').splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == 'oom_kill':
                try:
                    return int(fields[1])
                except ValueError:
                    break
    return None


def cgroup_memory_limit_bytes() -> int | None:
    """Return the effective cgroup memory ceiling when it is finite."""
    for path in CGROUP_MEMORY_LIMIT_PATHS:
        try:
            raw = Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if not raw or raw == 'max':
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 uses a huge page-aligned sentinel for "unlimited".
        if 0 < value < (1 << 60):
            return value
    return None


def proc_rss_bytes(pid: int) -> int | None:
    """Read one process's resident set without importing application code."""
    try:
        fields = Path(f'/proc/{int(pid)}/statm').read_text(
            encoding='utf-8').split()
        return int(fields[1]) * int(os.sysconf('SC_PAGE_SIZE'))
    except (OSError, ValueError, IndexError, TypeError):
        return None


def worker_rss_recycle_limit_bytes(raw_mb: str | None = None) -> int:
    """Resolve the manager's external worker RSS ceiling; zero disables it."""
    if raw_mb not in (None, ''):
        try:
            configured_mb = float(raw_mb)
            if configured_mb == 0:
                return 0
            if configured_mb > 0:
                return max(1, int(configured_mb * MIB))
            raise ValueError('must be non-negative')
        except (TypeError, ValueError):
            logger.warning(
                'invalid TOFU_PROCESS_RSS_RECYCLE_MB=%r; using adaptive default',
                raw_mb)
    default = int(DEFAULT_WORKER_RSS_RECYCLE_MB * MIB)
    cgroup_limit = cgroup_memory_limit_bytes()
    if cgroup_limit is not None:
        default = min(
            default, int(cgroup_limit * DEFAULT_WORKER_RSS_CGROUP_FRACTION))
    return max(1, default)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    with tmp.open('w', encoding='utf-8') as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding='utf-8') as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def proc_start_ticks(pid: int) -> int | None:
    """Return Linux /proc start ticks, which disambiguate PID reuse."""
    try:
        raw = Path(f'/proc/{int(pid)}/stat').read_text(encoding='utf-8')
        # comm is parenthesized and may contain spaces; fields after its final
        # ')' begin with field 3.  starttime is field 22 => remainder index 19.
        rest = raw[raw.rfind(')') + 2:].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def proc_start_epoch(pid: int) -> float | None:
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return None
    try:
        boot = None
        for line in Path('/proc/stat').read_text(encoding='utf-8').splitlines():
            if line.startswith('btime '):
                boot = float(line.split()[1])
                break
        if boot is None:
            return None
        return boot + (ticks / float(os.sysconf('SC_CLK_TCK')))
    except (OSError, ValueError, TypeError):
        return None


def proc_env_value(pid: int, name: str) -> str | None:
    try:
        for item in Path(f'/proc/{int(pid)}/environ').read_bytes().split(b'\0'):
            key, sep, value = item.partition(b'=')
            if sep and key.decode(errors='ignore') == name:
                return value.decode('utf-8', errors='replace')
    except (OSError, ValueError, TypeError):
        pass
    return None


def proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode(
            'utf-8', errors='replace').strip()
    except (OSError, ValueError, TypeError):
        return None


def proc_cwd(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f'/proc/{int(pid)}/cwd'))
    except (OSError, ValueError, TypeError):
        return None


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text(encoding='utf-8')
        rest = stat[stat.rfind(')') + 2:].split()
        if rest and rest[0] == 'Z':
            return False
    except OSError:
        pass
    return True


def pid_is_server(pid: int) -> bool:
    if not pid_is_alive(pid):
        return False
    cmdline = proc_cmdline(pid)
    # An unreadable live process is ambiguous: report it as live so start
    # fails closed.  Stop applies stricter identity checks before signalling.
    return cmdline is None or 'server.py' in cmdline


def read_lock_status(project_path: str) -> dict[str, Any]:
    project = os.path.realpath(project_path)
    lock_path = Path(project) / 'data' / '.server.lock'
    result: dict[str, Any] = {
        'projectPath': project,
        'running': False,
        'pid': None,
        'host': None,
        'sameHost': None,
        'lockPresent': lock_path.is_file(),
        'stale': False,
        'processStartTime': None,
        'processStartedAt': None,
        'processCwd': None,
        'projectMatches': None,
        'cmdline': None,
        'externalOwner': None,
    }
    if not result['lockPresent']:
        return result
    try:
        entry = (lock_path.read_text(encoding='utf-8').splitlines() or [''])[0].strip()
    except OSError:
        result['stale'] = True
        return result
    if '@' not in entry:
        result['stale'] = True
        return result
    raw_pid, _, host = entry.partition('@')
    if not raw_pid.isdigit():
        result['stale'] = True
        return result
    pid = int(raw_pid)
    result['pid'] = pid
    result['host'] = host or None
    result['sameHost'] = (host == _hostname()) if host else None
    result['processStartTime'] = proc_start_ticks(pid)
    result['processStartedAt'] = proc_start_epoch(pid)
    result['processCwd'] = proc_cwd(pid)
    result['projectMatches'] = (
        result['processCwd'] == project if result['processCwd'] is not None else None)
    result['cmdline'] = proc_cmdline(pid)
    result['externalOwner'] = proc_env_value(pid, 'TOFU_MANAGED_BY')
    if result['sameHost'] is False:
        return result
    if pid_is_server(pid):
        result['running'] = True
    else:
        result['stale'] = True
    return result


def listener_pids(port: int) -> list[int]:
    """Return listener PIDs visible through ss; [] also means unavailable."""
    try:
        out = subprocess.run(
            ['ss', '-ltnp'], capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: set[int] = set()
    suffix = f':{int(port)}'
    for line in out.splitlines():
        cols = line.split()
        if not any(col.endswith(suffix) for col in cols[:6]):
            continue
        marker = 'pid='
        start = 0
        while True:
            at = line.find(marker, start)
            if at < 0:
                break
            digits = []
            for ch in line[at + len(marker):]:
                if not ch.isdigit():
                    break
                digits.append(ch)
            if digits:
                found.add(int(''.join(digits)))
            start = at + len(marker)
    return sorted(found)


def port_accepts(port: int, host: str = '127.0.0.1', timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _server_port(args: list[str], env: dict[str, str] | None = None) -> int:
    for index, arg in enumerate(args):
        if arg == '--port' and index + 1 < len(args):
            try:
                return int(args[index + 1])
            except ValueError:
                break
        if arg.startswith('--port='):
            try:
                return int(arg.partition('=')[2])
            except ValueError:
                break
    source = env if env is not None else os.environ
    try:
        return int(source.get('PORT', DEFAULT_SERVER_PORT))
    except (ValueError, TypeError):
        return DEFAULT_SERVER_PORT


def project_server_env(project_path: str) -> dict[str, str]:
    """Read lifecycle-relevant values from the project's simple .env file."""
    result: dict[str, str] = {}
    try:
        lines = (Path(project_path) / '.env').read_text(encoding='utf-8').splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key in SERVER_ENV_KEYS:
            result[key] = value.strip().strip('"').strip("'")
    return result


class LifecycleManager:
    """Own exactly one project's desired/observed server state."""

    def __init__(self, project_path: str, python_exe: str | None = None,
                 *, monitor_interval: float | None = None) -> None:
        self.project = os.path.realpath(project_path)
        self.python = python_exe or os.environ.get('TOFU_SUPERVISOR_PYTHON') or sys.executable
        self.data_dir = Path(self.project) / 'data'
        self.logs_dir = Path(self.project) / 'logs'
        self.state_path = self.data_dir / 'server-manager-state.json'
        self.worker_log = self.logs_dir / 'server-console.log'
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self._project_env = project_server_env(self.project)
        loaded = _read_json(self.state_path)
        self._had_state = loaded.get('version') == STATE_VERSION
        try:
            configured = float(os.environ.get('TOFU_MANAGER_INTERVAL', '') or
                               (monitor_interval or DEFAULT_MONITOR_INTERVAL))
        except (ValueError, TypeError):
            configured = DEFAULT_MONITOR_INTERVAL
        self.monitor_interval = max(0.2, configured)
        try:
            failure_window = float(
                os.environ.get('TOFU_MANAGER_FAILURE_WINDOW_SECS', '') or
                DEFAULT_FAILURE_WINDOW)
        except (ValueError, TypeError):
            failure_window = DEFAULT_FAILURE_WINDOW
        self.failure_window = max(30.0, min(3600.0, failure_window))
        try:
            max_failures = int(
                os.environ.get('TOFU_MANAGER_MAX_FAILURES', '') or
                DEFAULT_MAX_FAILURES)
        except (ValueError, TypeError):
            max_failures = DEFAULT_MAX_FAILURES
        self.max_failures = max(2, min(100, max_failures))
        rss_limit_mb = (
            os.environ.get('TOFU_PROCESS_RSS_RECYCLE_MB')
            or self._project_env.get('TOFU_PROCESS_RSS_RECYCLE_MB'))
        self.worker_rss_recycle_bytes = worker_rss_recycle_limit_bytes(
            rss_limit_mb)
        self._state = self._load_state()
        self._adopt_existing()

    def _default_state(self) -> dict[str, Any]:
        default_env = dict(self._project_env)
        return {
            'version': STATE_VERSION,
            'projectPath': self.project,
            'desired': 'stopped',
            'observed': 'stopped',
            'worker': {},
            'serverArgs': [],
            'serverEnv': default_env,
            'port': _server_port([], {**os.environ, **default_env}),
            'restartCount': 0,
            'consecutiveFailures': 0,
            'failureHistory': [],
            'activeFailureAt': 0.0,
            'lastFailureAt': 0.0,
            'lastFailureReason': '',
            'lastExitCause': '',
            'lastCgroupOomDelta': 0,
            'lastCgroupOomKillCount': None,
            'workerRssBytes': None,
            'lastMemoryRecycleAt': 0.0,
            'lastMemoryRecycleRssBytes': None,
            'lastRecoveredAt': 0.0,
            'lastRecoverySeconds': None,
            'nextRetryAt': 0.0,
            'wedgeSince': 0.0,
            'lastError': '',
            'lastTransitionAt': _now(),
            'launchSource': '',
            'updatedAt': _now(),
        }

    def _load_state(self) -> dict[str, Any]:
        loaded = _read_json(self.state_path)
        state = self._default_state()
        if loaded.get('version') == STATE_VERSION:
            state.update(loaded)
        state['projectPath'] = self.project
        if state.get('desired') not in ('running', 'stopped'):
            state['desired'] = 'stopped'
        if not isinstance(state.get('serverArgs'), list):
            state['serverArgs'] = []
        if not isinstance(state.get('serverEnv'), dict):
            state['serverEnv'] = {}
        if not isinstance(state.get('failureHistory'), list):
            state['failureHistory'] = []
        return state

    def _recent_failure_times(self, now: float | None = None) -> list[float]:
        stamp = _now() if now is None else float(now)
        cutoff = stamp - self.failure_window
        values: list[float] = []
        for raw in self._state.get('failureHistory') or []:
            try:
                value = float(raw)
            except (ValueError, TypeError):
                continue
            if cutoff <= value <= stamp + 1.0:
                values.append(value)
        return sorted(values)[-DEFAULT_FAILURE_HISTORY_KEEP:]

    def _clear_failure_budget(self) -> None:
        """Clear crash-loop gating only after an explicit human start/restart."""
        self._state['consecutiveFailures'] = 0
        self._state['failureHistory'] = []
        self._state['activeFailureAt'] = 0.0
        self._state['nextRetryAt'] = 0.0

    def _save(self) -> None:
        self._state['updatedAt'] = _now()
        _atomic_json(self.state_path, self._state)

    def _set_observed(self, observed: str, error: str = '') -> None:
        if self._state.get('observed') != observed:
            self._state['observed'] = observed
            self._state['lastTransitionAt'] = _now()
        if error:
            self._state['lastError'] = error

    def _remember_worker(self, status: dict[str, Any], source: str = '') -> None:
        if not status.get('pid'):
            return
        self._state['worker'] = {
            'pid': status['pid'],
            'host': status.get('host') or _hostname(),
            'processStartTime': status.get('processStartTime'),
            'processCwd': status.get('processCwd'),
            'spawnedAt': self._state.get('worker', {}).get('spawnedAt') or _now(),
        }
        if source:
            self._state['launchSource'] = source

    def _adopt_existing(self) -> None:
        with self._lock:
            status = read_lock_status(self.project)
            if status.get('running'):
                identity_error = self._identity_error(status)
                if identity_error:
                    self._set_observed('conflict', identity_error)
                    self._save()
                    return
                # Fresh install: adopt without a restart. Recovery of an
                # existing desired=stopped record is different: stop won
                # before the manager died, so never resurrect it on boot.
                if not (self._had_state and self._state.get('desired') == 'stopped'):
                    self._state['desired'] = 'running'
                target = ('stopping' if self._state.get('desired') == 'stopped'
                          else ('running' if port_accepts(self.port) else 'starting'))
                self._set_observed(target)
                self._remember_worker(status, self._state.get('launchSource') or 'adopted')
                self._disable_legacy_guard()
                self._save()

    @property
    def port(self) -> int:
        try:
            return int(self._state.get('port') or DEFAULT_SERVER_PORT)
        except (ValueError, TypeError):
            return DEFAULT_SERVER_PORT

    def _disable_legacy_guard(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / '.tofu_guard_disabled').touch()
        except OSError:
            pass

    def start_monitor(self) -> None:
        with self._lock:
            if self._monitor and self._monitor.is_alive():
                return
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name=f'tofu-manager-{Path(self.project).name}', daemon=True)
            self._monitor.start()

    def close(self) -> None:
        self._stop_event.set()
        thread = self._monitor
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.monitor_interval + 1.0))

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.monitor_interval):
            try:
                self.reconcile()
            except Exception as exc:
                with self._lock:
                    self._set_observed('degraded', f'monitor error: {exc}')
                    try:
                        self._save()
                    except OSError:
                        pass

    def _launcher_is_alive(self) -> bool:
        worker = self._state.get('worker') or {}
        pid = worker.get('pid')
        if not isinstance(pid, int) or not pid_is_server(pid):
            return False
        expected = worker.get('processStartTime')
        actual = proc_start_ticks(pid)
        return expected is None or actual is None or expected == actual

    def _http_healthy(self, timeout: float = 2.0) -> bool:
        mode = ''
        try:
            mode = (self.data_dir / '.last_serve_mode').read_text().strip()
        except OSError:
            pass
        schemes = ['https', 'http'] if mode == 'https' else ['http', 'https']
        context = ssl._create_unverified_context()
        for scheme in schemes:
            try:
                with urllib.request.urlopen(
                    f'{scheme}://127.0.0.1:{self.port}/api/health', timeout=timeout,
                    context=context if scheme == 'https' else None,
                ) as response:
                    if 200 <= response.status < 300:
                        payload = json.loads(response.read().decode('utf-8') or '{}')
                        if isinstance(payload, dict):
                            return True
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                continue
        return False

    def _heartbeat_age(self, pid: int) -> float | None:
        root = (os.environ.get('TOFU_HEARTBEAT_DIR') or '').strip()
        if root:
            path = Path(root) / 'server.heartbeat'
        else:
            local_root = (os.environ.get('TOFU_DB_LOCAL_ROOT') or '').strip() or '/tmp/tofu'
            path = Path(local_root) / 'heartbeat' / 'server.heartbeat'
        value = _read_json(path)
        try:
            if int(value.get('pid')) != int(pid):
                return None
            age = _now() - float(value.get('ts'))
            return age if age >= 0 else None
        except (ValueError, TypeError):
            return None

    def _identity_error(self, status: dict[str, Any]) -> str | None:
        pid = status.get('pid')
        if not isinstance(pid, int) or not status.get('running'):
            return None
        if status.get('sameHost') is False:
            return f'lock belongs to host {status.get("host")}'
        cmdline = status.get('cmdline')
        if cmdline is None:
            return f'cannot verify cmdline for pid {pid}'
        if 'server.py' not in cmdline:
            return f'pid {pid} is not server.py'
        if status.get('projectMatches') is False:
            return f'pid {pid} cwd is {status.get("processCwd")}, not this project'
        external_owner = status.get('externalOwner')
        if external_owner and external_owner != 'supervisor':
            return f'pid {pid} is owned by {external_owner}, not tofu-manager'
        remembered = self._state.get('worker') or {}
        if remembered.get('pid') == pid:
            expected = remembered.get('processStartTime')
            actual = status.get('processStartTime')
            if expected is not None and actual is not None and expected != actual:
                return f'pid {pid} was reused (process start time changed)'
        return None

    def _port_conflict(self, status: dict[str, Any]) -> tuple[bool, list[int]]:
        pids = listener_pids(self.port)
        worker_pid = status.get('pid') if status.get('running') else None
        # During a saturated boot the listener can become visible a moment
        # before the lock reader can fully verify /proc and report running.
        # Do not call our own just-spawned worker a foreign conflict, but only
        # after every independent identity field matches. Any missing/mismatched
        # signal keeps the existing fail-closed conflict behavior.
        if worker_pid is None and len(pids) == 1:
            remembered = self._state.get('worker') or {}
            candidate = pids[0]
            expected_start = remembered.get('processStartTime')
            actual_start = proc_start_ticks(candidate)
            if (remembered.get('pid') == candidate
                    and expected_start is not None
                    and actual_start is not None
                    and expected_start == actual_start
                    and proc_cwd(candidate) == self.project
                    and 'server.py' in (proc_cmdline(candidate) or '')
                    and proc_env_value(candidate, 'TOFU_MANAGED_BY') == 'supervisor'):
                worker_pid = candidate
        foreign = [pid for pid in pids if pid != worker_pid]
        unknown_listener = worker_pid is None and not pids and port_accepts(self.port)
        owners = foreign if worker_pid is not None else pids
        return bool(foreign or (pids and worker_pid is None) or unknown_listener), owners

    def _remembered_worker_status(self) -> dict[str, Any] | None:
        worker = self._state.get('worker') or {}
        pid = worker.get('pid')
        if not isinstance(pid, int) or not pid_is_alive(pid):
            return None
        cwd = proc_cwd(pid)
        cmdline = proc_cmdline(pid)
        return {
            'projectPath': self.project,
            'running': True,
            'pid': pid,
            'host': worker.get('host') or _hostname(),
            'sameHost': (worker.get('host') or _hostname()) == _hostname(),
            'lockPresent': False,
            'stale': False,
            'processStartTime': proc_start_ticks(pid),
            'processStartedAt': proc_start_epoch(pid),
            'processCwd': cwd,
            'projectMatches': cwd == self.project if cwd is not None else None,
            'cmdline': cmdline,
            'externalOwner': proc_env_value(pid, 'TOFU_MANAGED_BY'),
        }

    def status(self, *, probe_health: bool = False) -> dict[str, Any]:
        with self._lock:
            low = read_lock_status(self.project)
            conflict, foreign = self._port_conflict(low)
            worker = dict(self._state.get('worker') or {})
            recent_failure_count = len(self._recent_failure_times())
            observed = self._state.get('observed') or 'stopped'
            identity_error = self._identity_error(low) if low.get('running') else None
            if conflict or identity_error:
                observed = 'conflict'
            elif low.get('running'):
                if self._state.get('desired') == 'stopped':
                    observed = 'stopping'
                else:
                    observed = 'running' if port_accepts(self.port) else 'starting'
            elif self._state.get('desired') == 'stopped':
                observed = 'stopped'
            elif self._launcher_is_alive():
                observed = 'starting'
            elif (int(self._state.get('consecutiveFailures') or 0) >= self.max_failures
                  or recent_failure_count >= self.max_failures):
                observed = 'crashloop'
            health = None
            if probe_health and low.get('running') and not conflict:
                health = self._http_healthy()
                if not health and observed == 'running':
                    observed = 'degraded'
            last_error = self._state.get('lastError') or ''
            if health is True:
                # The returned snapshot should never contradict its own live
                # probe while the background reconcile is between ticks.
                last_error = ''
            return {
                **low,
                'desired': self._state.get('desired'),
                'observed': observed,
                'port': self.port,
                'health': health,
                'owner': 'tofu-manager',
                'managerPid': os.getpid(),
                'worker': worker,
                'restartCount': int(self._state.get('restartCount') or 0),
                'consecutiveFailures': int(self._state.get('consecutiveFailures') or 0),
                'recentFailureCount': recent_failure_count,
                'failureWindowSeconds': self.failure_window,
                'maxFailures': self.max_failures,
                'lastFailureAt': float(self._state.get('lastFailureAt') or 0),
                'lastFailureReason': self._state.get('lastFailureReason') or '',
                'lastExitCause': self._state.get('lastExitCause') or '',
                'lastCgroupOomDelta': int(
                    self._state.get('lastCgroupOomDelta') or 0),
                'cgroupOomKillCount': self._state.get(
                    'lastCgroupOomKillCount'),
                'workerRssBytes': self._state.get('workerRssBytes'),
                'workerRssRecycleBytes': self.worker_rss_recycle_bytes,
                'workerRssGuardEnabled': bool(self.worker_rss_recycle_bytes),
                'lastMemoryRecycleAt': float(
                    self._state.get('lastMemoryRecycleAt') or 0),
                'lastMemoryRecycleRssBytes': self._state.get(
                    'lastMemoryRecycleRssBytes'),
                'lastRecoveredAt': float(self._state.get('lastRecoveredAt') or 0),
                'lastRecoverySeconds': self._state.get('lastRecoverySeconds'),
                'nextRetryAt': float(self._state.get('nextRetryAt') or 0),
                'lastError': last_error,
                'launchSource': self._state.get('launchSource') or '',
                'serverArgs': list(self._state.get('serverArgs') or []),
                'serverEnv': dict(self._state.get('serverEnv') or {}),
                'foreignListenerPids': foreign,
                'workerLog': str(self.worker_log),
                'managerLog': str(self.logs_dir / 'server-manager.log'),
                'stateFile': str(self.state_path),
            }

    def _spawn(self, source: str) -> dict[str, Any]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._disable_legacy_guard()
        env = os.environ.copy()
        env['TOFU_SERVER_WORKER'] = '1'
        env['TOFU_SERVER_PROCESS'] = '1'
        env['TOFU_MANAGED_BY'] = 'supervisor'
        env['PORT'] = str(self.port)
        env.update({str(key): str(value)
                    for key, value in (self._state.get('serverEnv') or {}).items()})
        args = [str(item) for item in self._state.get('serverArgs') or []]
        try:
            log_fh = self.worker_log.open('ab')
        except OSError as exc:
            self._set_observed('crashloop', f'cannot open worker log: {exc}')
            self._save()
            return {'ok': False, 'message': str(exc)}
        try:
            proc = subprocess.Popen(
                [self.python, 'server.py', *args], cwd=self.project, env=env,
                stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
        except Exception as exc:
            self._set_observed('crashloop', f'spawn failed: {exc}')
            self._save()
            return {'ok': False, 'message': f'spawn failed: {exc}'}
        finally:
            log_fh.close()
        self._state['worker'] = {
            'pid': proc.pid,
            'host': _hostname(),
            'processStartTime': proc_start_ticks(proc.pid),
            'processCwd': self.project,
            'spawnedAt': _now(),
        }
        self._state['launchSource'] = source
        self._state['lastError'] = ''
        self._set_observed('starting')
        self._save()
        return {
            'ok': True,
            'alreadyRunning': False,
            'launcherPid': proc.pid,
            'message': 'started; poll /status for readiness',
        }

    def start(self, *, server_args: list[str] | None = None,
              server_env: dict[str, str] | None = None,
              source: str = 'cli', explicit: bool = True) -> dict[str, Any]:
        with self._lock:
            if server_args is not None and not isinstance(server_args, list):
                return {'ok': False, 'message': 'serverArgs must be a JSON array'}
            if server_env is not None and not isinstance(server_env, dict):
                return {'ok': False, 'message': 'serverEnv must be a JSON object'}
            status = read_lock_status(self.project)
            if status.get('running'):
                identity_error = self._identity_error(status)
                conflict, foreign = self._port_conflict(status)
                if identity_error or conflict:
                    error = identity_error or (
                        f'port {self.port} also owned by pid(s) {foreign}')
                    self._set_observed('conflict', error)
                    self._save()
                    return {'ok': False, 'alreadyRunning': False,
                            'launcherPid': None, 'message': error,
                            **self.status()}
                # Idempotent start never reconfigures a live worker. Applying
                # different argv/env behind its back makes status probe the
                # wrong port; configuration changes require explicit restart.
                self._state['desired'] = 'running'
                if explicit:
                    self._clear_failure_budget()
                self._remember_worker(
                    status, self._state.get('launchSource') or 'adopted')
                self._set_observed('running' if port_accepts(self.port) else 'starting')
                self._disable_legacy_guard()
                self._save()
                return {'ok': True, 'alreadyRunning': True,
                        'launcherPid': status.get('pid'),
                        'message': ('already running; supplied server options were ignored'
                                    if server_args or server_env else 'already running'),
                        **self.status()}

            if server_args is not None:
                cleaned = [str(arg) for arg in server_args]
                if len(cleaned) > 32 or any(len(arg) > 4096 for arg in cleaned):
                    return {'ok': False, 'message': 'serverArgs exceeds safety limit'}
                self._state['serverArgs'] = cleaned
            if server_env is not None:
                unsafe = sorted(set(server_env) - SERVER_ENV_KEYS)
                if unsafe:
                    return {'ok': False,
                            'message': f'unsupported serverEnv key(s): {", ".join(unsafe)}'}
                self._project_env = project_server_env(self.project)
                forwarded_env = {
                    str(key): str(value) for key, value in server_env.items()
                    if value is not None and str(value) != ''}
                self._state['serverEnv'] = {**self._project_env, **forwarded_env}
            if server_args is not None or server_env is not None:
                effective_env = dict(os.environ)
                effective_env.update(self._state.get('serverEnv') or {})
                self._state['port'] = _server_port(
                    self._state.get('serverArgs') or [], effective_env)
            status = read_lock_status(self.project)
            conflict, foreign = self._port_conflict(status)
            if conflict:
                self._state['desired'] = 'running'
                owner = f'pid(s) {foreign}' if foreign else 'an unknown process'
                self._set_observed('conflict',
                                   f'port {self.port} owned by {owner}')
                self._save()
                return {'ok': False, 'alreadyRunning': False,
                        'launcherPid': None, 'message': self._state['lastError'],
                        **self.status()}
            self._state['desired'] = 'running'
            if explicit:
                self._clear_failure_budget()
            if self._launcher_is_alive():
                self._set_observed('starting')
                self._save()
                return {'ok': True, 'alreadyRunning': True,
                        'launcherPid': (self._state.get('worker') or {}).get('pid'),
                        'message': 'startup already in progress', **self.status()}
            return self._spawn(source)

    def _terminate(self, status: dict[str, Any], *, timeout: float = 12.0) -> tuple[bool, bool, str]:
        pid = status.get('pid')
        if not isinstance(pid, int) or not status.get('running'):
            return True, False, 'nothing running'
        error = self._identity_error(status)
        if error:
            return False, False, error
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True, False, 'already stopped'
        except OSError as exc:
            return False, False, f'SIGTERM failed: {exc}'
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not pid_is_alive(pid):
                return True, False, 'stopped cleanly'
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True, False, 'stopped cleanly'
        except OSError as exc:
            return False, False, f'SIGKILL failed: {exc}'
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not pid_is_alive(pid):
                return True, True, 'stopped with SIGKILL'
            time.sleep(0.1)
        return False, True, f'pid {pid} survived SIGKILL'

    def _enforce_worker_rss_limit(self, status: dict[str, Any]) -> bool:
        """Recycle an owned worker before its RSS can reach the cgroup ceiling.

        Returns true when reconciliation was consumed by a recycle attempt.
        The in-process guard can free caches first once the new worker code is
        loaded; this manager-side hard stop also protects an already-running
        older worker and remains independent of application health.
        """
        pid = status.get('pid')
        if not isinstance(pid, int):
            self._state['workerRssBytes'] = None
            return False
        rss_bytes = proc_rss_bytes(pid)
        self._state['workerRssBytes'] = rss_bytes
        limit_bytes = self.worker_rss_recycle_bytes
        if (not limit_bytes or rss_bytes is None
                or rss_bytes < limit_bytes):
            return False
        message = (
            f'manager RSS ceiling exceeded: worker pid {pid} used '
            f'{rss_bytes / MIB:.1f} MiB >= {limit_bytes / MIB:.1f} MiB')
        now = _now()
        self._state['lastMemoryRecycleAt'] = now
        self._state['lastMemoryRecycleRssBytes'] = rss_bytes
        self._set_observed('stopping', message)
        self._save()
        ok, killed, error = self._terminate(status)
        if not ok:
            self._set_observed('conflict', f'{message}; {error}')
            self._save()
            logger.error('%s; termination failed: %s', message, error)
            return True
        self._state['worker'] = {}
        self._state['lastError'] = (
            f'{message}; {"forced SIGKILL" if killed else "graceful SIGTERM"}')
        logger.warning('%s; worker stopped (%s)', message, error)
        self._record_failure()
        return True

    def stop(self, *, source: str = 'cli', keep_desired_running: bool = False) -> dict[str, Any]:
        with self._lock:
            status = read_lock_status(self.project)
            if not status.get('running'):
                status = self._remembered_worker_status() or status
            was_running = bool(status.get('running'))
            self._state['desired'] = 'running' if keep_desired_running else 'stopped'
            self._state['launchSource'] = source
            self._set_observed('stopping' if was_running else 'stopped')
            self._disable_legacy_guard()
            self._save()  # desired=stopped must win before the signal is sent
            ok, killed, message = self._terminate(status)
            if ok:
                self._state['worker'] = {}
                self._set_observed('stopped')
                self._state['lastError'] = ''
            else:
                self._set_observed('conflict', message)
            self._save()
            return {
                'ok': ok,
                'wasRunning': was_running,
                'exitCode': 2 if killed and ok else (0 if ok else 1),
                'message': message,
                **self.status(),
            }

    def restart(self, *, server_args: list[str] | None = None,
                server_env: dict[str, str] | None = None,
                source: str = 'cli') -> dict[str, Any]:
        with self._lock:
            if server_args is not None and not isinstance(server_args, list):
                return {'ok': False, 'message': 'serverArgs must be a JSON array'}
            if server_env is not None and not isinstance(server_env, dict):
                return {'ok': False, 'message': 'serverEnv must be a JSON object'}
            if server_args is not None:
                self._state['serverArgs'] = [str(arg) for arg in server_args]
            if server_env is not None:
                unsafe = sorted(set(server_env) - SERVER_ENV_KEYS)
                if unsafe:
                    return {'ok': False,
                            'message': f'unsupported serverEnv key(s): {", ".join(unsafe)}'}
                self._project_env = project_server_env(self.project)
                forwarded_env = {
                    str(key): str(value) for key, value in server_env.items()
                    if value is not None and str(value) != ''}
                self._state['serverEnv'] = {**self._project_env, **forwarded_env}
            if server_args is not None or server_env is not None:
                effective_env = dict(os.environ)
                effective_env.update(self._state.get('serverEnv') or {})
                self._state['port'] = _server_port(
                    self._state.get('serverArgs') or [], effective_env)
            stopped = self.stop(source=source, keep_desired_running=True)
            if not stopped.get('ok'):
                return stopped
            self._clear_failure_budget()
            return self.start(server_args=self._state.get('serverArgs') or [],
                              server_env=self._state.get('serverEnv') or {},
                              source=source, explicit=True)

    def reconcile(self) -> None:
        with self._lock:
            status = read_lock_status(self.project)
            if not status.get('running') and self._state.get('desired') == 'stopped':
                status = self._remembered_worker_status() or status
            desired = self._state.get('desired')
            identity_error = self._identity_error(status) if status.get('running') else None
            if identity_error:
                self._set_observed('conflict', identity_error)
                self._save()
                return
            conflict, foreign = self._port_conflict(status)
            if conflict:
                owner = f'pid(s) {foreign}' if foreign else 'an unknown process'
                self._set_observed('conflict',
                                   f'port {self.port} owned by {owner}')
                self._save()
                return
            if desired == 'stopped':
                # A worker surviving a manager crash after stop was persisted is
                # completed here. Unknown identity fails closed in _terminate.
                if status.get('running'):
                    self._set_observed('stopping')
                    self._save()
                    ok, _, error = self._terminate(status)
                    if not ok:
                        self._set_observed('conflict', error)
                        self._save()
                        return
                self._state['worker'] = {}
                self._set_observed('stopped')
                self._save()
                return
            if status.get('running'):
                self._remember_worker(status)
                if self._enforce_worker_rss_limit(status):
                    return
                healthy = self._http_healthy()
                if healthy:
                    recovered_from = float(self._state.get('activeFailureAt') or 0)
                    now = _now()
                    if recovered_from:
                        elapsed = max(0.0, now - recovered_from)
                        self._state['lastRecoveredAt'] = now
                        self._state['lastRecoverySeconds'] = round(elapsed, 3)
                        self._state['activeFailureAt'] = 0.0
                        logger.info(
                            'worker recovered pid=%s in %.3fs after failure',
                            status.get('pid'), elapsed)
                    self._state['failureHistory'] = self._recent_failure_times(now)
                    oom_count = cgroup_oom_kill_count()
                    if oom_count is not None:
                        self._state['lastCgroupOomKillCount'] = oom_count
                    self._state['consecutiveFailures'] = 0
                    self._state['nextRetryAt'] = 0.0
                    self._state['wedgeSince'] = 0.0
                    self._state['lastError'] = ''
                    self._set_observed('running')
                    self._save()
                    return
                age = self._heartbeat_age(int(status['pid']))
                if age is None or age < DEFAULT_WEDGE_STALE:
                    self._set_observed('degraded', 'HTTP health failed; heartbeat not stale')
                    self._save()
                    return
                first = float(self._state.get('wedgeSince') or 0)
                if not first:
                    self._state['wedgeSince'] = _now()
                    self._set_observed('degraded',
                                       f'possible wedge; heartbeat stale {age:.0f}s')
                    self._save()
                    return
                if _now() - first < DEFAULT_WEDGE_STREAK:
                    self._save()
                    return
                ok, _, error = self._terminate(status, timeout=3.0)
                if not ok:
                    self._set_observed('conflict', error)
                    self._save()
                    return
                self._state['worker'] = {}
                self._state['wedgeSince'] = 0.0
                self._state['lastError'] = f'recovered wedged worker pid {status["pid"]}'
                self._record_failure()
                return
            if self._launcher_is_alive():
                spawned = float((self._state.get('worker') or {}).get('spawnedAt') or 0)
                if not spawned or _now() - spawned <= DEFAULT_BOOT_GRACE:
                    self._set_observed('starting')
                    self._save()
                    return
            self._record_failure()

    def _record_failure(self) -> None:
        retry_at = float(self._state.get('nextRetryAt') or 0)
        if retry_at:
            if _now() < retry_at:
                self._save()
                return
            self._state['nextRetryAt'] = 0.0
            self._spawn('automatic-recovery')
            return
        now = _now()
        history = self._recent_failure_times(now)
        history.append(now)
        history = history[-DEFAULT_FAILURE_HISTORY_KEEP:]
        recent_failures = len(history)
        failures = int(self._state.get('consecutiveFailures') or 0) + 1
        prior_oom = self._state.get('lastCgroupOomKillCount')
        current_oom = cgroup_oom_kill_count()
        oom_delta = 0
        if isinstance(prior_oom, int) and isinstance(current_oom, int):
            oom_delta = max(0, current_oom - prior_oom)
        if current_oom is not None:
            self._state['lastCgroupOomKillCount'] = current_oom
        prior_error = self._state.get('lastError') or ''
        if prior_error.startswith('manager RSS ceiling exceeded'):
            exit_cause = 'manager_rss_recycle'
            failure_reason = prior_error
        elif oom_delta:
            exit_cause = 'cgroup_oom_event'
            failure_reason = (
                f'worker disappeared while shared cgroup oom_kill advanced '
                f'by {oom_delta}')
        elif prior_error.startswith('recovered wedged worker'):
            exit_cause = 'health_wedge'
            failure_reason = prior_error
        else:
            exit_cause = 'unexpected_exit'
            failure_reason = prior_error or 'worker exited unexpectedly'
        self._state['failureHistory'] = history
        self._state['activeFailureAt'] = (
            float(self._state.get('activeFailureAt') or 0) or now)
        self._state['lastFailureAt'] = now
        self._state['lastFailureReason'] = failure_reason
        self._state['lastExitCause'] = exit_cause
        self._state['lastCgroupOomDelta'] = oom_delta
        self._state['consecutiveFailures'] = failures
        self._state['restartCount'] = int(self._state.get('restartCount') or 0) + 1
        self._state['worker'] = {}
        logger.warning(
            'worker failure %d/%d in %.0fs window (consecutive=%d, cause=%s): %s',
            recent_failures, self.max_failures, self.failure_window,
            failures, exit_cause, failure_reason)
        if recent_failures >= self.max_failures:
            self._set_observed('crashloop',
                               f'worker failed {recent_failures} times in '
                               f'{self.failure_window:.0f}s; recovery paused')
            self._state['nextRetryAt'] = 0.0
            self._save()
            logger.error(
                'worker recovery paused after %d failures in %.0fs; explicit '
                'start/restart is required to reset the crash-loop budget',
                recent_failures, self.failure_window)
            return
        delay = min(60.0, float(2 ** max(failures, recent_failures)))
        self._state['nextRetryAt'] = now + delay
        self._set_observed('starting', f'worker exited; retrying in {delay:.0f}s')
        self._save()

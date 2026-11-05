#!/usr/bin/env python3
"""Single-owner lifecycle state machine for the Tofu server worker.

This module intentionally uses only the Python standard library.  The manager
must remain observable and able to stop/recover the worker even when the
application dependency graph or database cannot be imported.
"""

from __future__ import annotations

import errno as errno
import http.client  # noqa: F401 - dependency availability check
import json
import logging
import math
import os
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref

from collections.abc import Callable, Collection, Iterable
from tofu_db._loopback import create_connection, urlopen as loopback_urlopen
from pathlib import Path
from typing import Any

from runtime_guards import (
    RESOURCE_BUDGET_AUTOMATIC_ENV,
    RESOURCE_BUDGET_ENV_KEYS,
    RESOURCE_BUDGET_POLICY_ENV,
    RESOURCE_BUDGET_POLICY_VERSION,
    _persistent_data_path,
    install_process_resource_defaults,
    pid_is_alive,
    proc_stat_fields,
)
from tofu_dotenv import read_dotenv_values
from serverctl_pkg.restart_budget import ARTIFACT_PREPARATION_SECONDS


STATE_VERSION = 1
DEFAULT_SERVER_PORT = 15000
DEFAULT_MONITOR_INTERVAL = 5.0
DEFAULT_BOOT_GRACE = 180.0
DEFAULT_WEDGE_STALE = 180.0
DEFAULT_WEDGE_STREAK = 120.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_FAILURE_WINDOW = 120.0
DEFAULT_FAILURE_HISTORY_KEEP = 50
DEFAULT_SIDECAR_LEASE_RELEASE_WAIT = 10.0
DEFAULT_ORPHAN_SIDECAR_REAP_WAIT = 15.0
DEFAULT_STORAGE_LEASE_POLL_INTERVAL = 0.1
# SIGKILL escalation must outlast the worker's own graceful-shutdown hard
# budget (TOFU_SHUTDOWN_HARD_SECS, default 30s in lib/server/server_shutdown.py):
# killing the worker mid-sequence skips its bounded storage-sidecar stop and
# orphans the authority's project lease.  The manager mirrors the worker's
# configured budget — deployments may raise it well past 30s — and adds a
# fixed margin so the worker's own deadline always fires first.
DEFAULT_WORKER_SHUTDOWN_HARD_SECS = 30.0
WORKER_TERMINATE_TIMEOUT_MARGIN = 5.0
DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT = ARTIFACT_PREPARATION_SECONDS
DEFAULT_EXIT_INTENT_WINDOW = 120.0
# Grace for a worker whose heartbeat declares the `booting` phase. Mirrors
# lib/server_boot/lock.py's `_boot_heartbeat_stale_threshold` so the manager's
# live-wedge detector cannot kill a legitimately slow FUSE boot: startup plus
# storage recovery routinely runs 500-1000 s here, far beyond the 180 s
# serving-loop wedge threshold. Env `TOFU_BOOT_HEARTBEAT_GRACE_SECS`, clamped
# to [60, 900] exactly like the lock module.
# Reconcile uses stale heartbeats for diagnosis, never permission to terminate
# a live worker. Dependency recovery must retain in-flight task ownership.
DEFAULT_BOOT_HEARTBEAT_GRACE = 180.0
BOOT_HEARTBEAT_GRACE_MIN = 60.0
BOOT_HEARTBEAT_GRACE_MAX = 900.0
CLEAN_WORKER_EXIT_REASONS = frozenset(
    {'manual', 'signal', 'restart', 'memory_recycle'})
WORKER_BUDGET_STATUS_KEYS = (
    'TOFU_MAX_INFLIGHT_TASKS',
    'TOFU_AGENT_WORKERS',
)
CGROUP_OOM_EVENT_PATHS = (
    '/sys/fs/cgroup/memory.events',
    '/sys/fs/cgroup/memory/memory.oom_control',
)
SERVER_ENV_KEYS = frozenset({
    'PORT', 'BIND_HOST', 'TOFU_TLS', 'TLS_CERTFILE', 'TLS_KEYFILE',
    'TOFU_DEPLOYMENT_MODE',
    'TOFU_DATA_DIR', 'TOFU_DATA_LAYOUT',
    'XDG_DATA_HOME', 'LOCALAPPDATA',
    'TOFU_SERVER_PYTHON_CACHE', 'TOFU_SERVER_PYTHON_CACHE_DIR',
    # Local placement of the worker runtime replica and of task checkouts is
    # decided by the manager before spawn and by the worker's workspace control.
    'TOFU_RUNTIME_REPLICA', 'TOFU_LOCAL_WORKSPACES', 'TOFU_LOCAL_DIR',
    # Agent scheduling is created before request handling starts. Preserve
    # explicit project overrides across manager-owned worker generations.
    'TOFU_AGENT_QUEUE_CAPACITY', 'TOFU_AGENT_STUCK_REPLACEMENTS',
    # The manager mirrors the worker's graceful-shutdown hard budget when
    # sizing its own SIGKILL escalation (see _worker_shutdown_hard_seconds).
    'TOFU_SHUTDOWN_HARD_SECS',
    'TOFU_HTTP_WRITE_STALL_SECS',
    # These must exist before the Python worker starts. Loading them later in
    # server.py's dotenv phase cannot reconfigure glibc or already-imported
    # BLAS/OpenMP runtimes.
    'TOFU_MALLOC_ARENA_MAX', 'TOFU_NUMERIC_THREADS',
    'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
    'NUMEXPR_NUM_THREADS',
}) | RESOURCE_BUDGET_ENV_KEYS

_DATA_LOCATION_ENV_KEYS = (
    'TOFU_DATA_DIR',
    'XDG_DATA_HOME',
    'LOCALAPPDATA',
)
_PYTEST_CONTEXT_ENV_KEYS = (
    'PYTEST_CURRENT_TEST',
    'TOFU_PYTEST_RUN_ROOT',
)
# The interpreter and dynamic loader consume these before any Python runs.
# A project .env reached them only through the worker's post-start boot
# loader, which can steer child processes but never the worker interpreter;
# the exec-time .env overlay keeps that boundary.
_DOTENV_EXEC_EXCLUDED_KEYS = frozenset({
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
    'LD_PRELOAD', 'LD_LIBRARY_PATH',
})
_DOTENV_EXEC_EXCLUDED_PREFIXES = ('DYLD_',)
# Unreadable-.env failures already warned in this process, oldest first.
# Each lifecycle operation re-reads the file several times; one warning per
# distinct (path, failure) is enough until a successful read clears it.
_DOTENV_WARNING_MEMORY = 32
_dotenv_warned_failures: dict[tuple[str, str], None] = {}
_dotenv_warned_failures_lock = threading.Lock()

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


def _boot_heartbeat_grace_seconds() -> float:
    """Manager-side mirror of lib/server_boot/lock.py's boot stale threshold.

    One source of truth is the env var; the parsing is duplicated here only
    so the manager's live-wedge detector does not import a private symbol from
    the lock module. A worker that declares the `booting` heartbeat phase is
    judged against this grace (default 180 s, clamped [60, 900]) instead of
    the 180 s serving-loop wedge threshold, so a slow FUSE boot is never
    mistaken for a wedge.
    """
    try:
        value = float(
            os.environ.get('TOFU_BOOT_HEARTBEAT_GRACE_SECS', '') or
            str(DEFAULT_BOOT_HEARTBEAT_GRACE))
    except (ValueError, TypeError):
        value = DEFAULT_BOOT_HEARTBEAT_GRACE
    return max(BOOT_HEARTBEAT_GRACE_MIN, min(BOOT_HEARTBEAT_GRACE_MAX, value))


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


def proc_exit_observation(worker: dict[str, Any]) -> dict[str, Any]:
    """Read an identity-verified Linux zombie's exit status without reaping it.

    No signal sender is inferred. A core-file flag says what wait status
    reported, not whether the host saved a complete core. Missing/reused PIDs
    and non-Linux hosts produce an unknown observation rather than a guess.
    """
    try:
        pid = int(worker['pid'])
        expected_ticks = int(worker['processStartTime'])
        if pid <= 0 or expected_ticks <= 0:
            return {}
        raw = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        fields = raw[raw.rfind(')') + 2:].split()
        if fields[0] != 'Z' or int(fields[19]) != expected_ticks:
            return {}
        status = int(fields[49])
        signum = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        try:
            signal_name = signal.Signals(signum).name if signum else ''
        except ValueError:
            signal_name = f'SIGNAL_{signum}'
        return {
            'pid': pid, 'processStartTime': expected_ticks,
            'source': 'linux_proc_stat', 'rawStatus': status,
            'signal': signum, 'signalName': signal_name,
            'exitCode': os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
            'coreDumpFlag': bool(os.WCOREDUMP(status)) if signum else False,
        }
    except (OSError, ValueError, IndexError, TypeError, KeyError, AttributeError):
        return {}


def popen_exit_observation(
        pid: int, start_ticks: Any, returncode: int) -> dict[str, Any]:
    """Exit evidence from a reaped ``Popen.returncode`` where /proc is absent.

    The return code carries no core-dump flag or raw wait status, so both are
    unknown (None) rather than guessed.
    """
    signum = -returncode if returncode < 0 else None
    try:
        signal_name = signal.Signals(signum).name if signum else ''
    except ValueError:
        signal_name = f'SIGNAL_{signum}'
    return {
        'pid': pid, 'processStartTime': start_ticks,
        'source': 'popen_returncode', 'rawStatus': None,
        'signal': signum, 'signalName': signal_name,
        'exitCode': returncode if returncode >= 0 else None,
        'coreDumpFlag': None,
    }


def reap_exited_child(pid: Any, start_ticks: Any) -> int | None:
    """Reap one identity-verified exited child of this process, never another.

    Waits (WNOHANG) only for a zombie whose parent is this process and whose
    start ticks match the recorded generation, so every other Popen object in
    the process keeps its own exit status. Returns the raw wait status, or
    None for a running, reused, foreign or already-reaped PID.
    """
    try:
        pid, start_ticks = int(pid), int(start_ticks)
    except (TypeError, ValueError):
        return None
    fields = proc_stat_fields(pid) if pid > 0 else None
    if (fields is None or fields[0] != 'Z'
            or fields[1] != str(os.getpid())
            or fields[19] != str(start_ticks)):
        return None
    try:
        reaped, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    return status if reaped == pid else None


def child_process_identities() -> list[tuple[int, int]]:
    """(pid, start ticks) of every direct child of this process.

    Linux lists children per creating thread; an exec leaves one thread,
    which inherits every child of the previous image. Empty without /proc
    child lists.
    """
    identities: list[tuple[int, int]] = []
    try:
        threads = os.listdir('/proc/self/task')
    except OSError:
        return identities
    for thread in threads:
        try:
            children = Path(
                f'/proc/self/task/{thread}/children').read_bytes().split()
        except OSError:
            continue
        for child in children:
            start_ticks = proc_start_ticks(int(child))
            if start_ticks is not None:
                identities.append((int(child), start_ticks))
    return identities


def reap_inherited_children(
    inherited: Iterable[tuple[int, int]],
    pending_worker_pids: Collection[int] = (),
) -> list[tuple[int, int]]:
    """Reap exited children that an earlier image of this process left behind.

    ``os.exec*`` keeps the PID and its children but discards the Popen objects
    that would have waited for them. *inherited* holds identities captured
    before this image started any child, so no Popen here owns them. A worker
    in *pending_worker_pids* is left to its manager, which records the exit
    evidence and then reaps it. Returns the identities still running or
    pending; children already reaped or whose PID was reused are dropped.
    """
    remaining = []
    for pid, start_ticks in inherited:
        fields = proc_stat_fields(pid)
        if (fields is None or fields[1] != str(os.getpid())
                or fields[19] != str(start_ticks)):
            continue
        if fields[0] == 'Z' and pid not in pending_worker_pids:
            status = reap_exited_child(pid, start_ticks)
            if status is not None:
                logger.info(
                    'reaped child pid=%s inherited from an earlier '
                    'Supervisor image (wait status %s)', pid, status)
                continue
        remaining.append((pid, start_ticks))
    return remaining


# supervisor.py imports this module before it starts any child and re-execs
# itself in place on /reload, so children present at import belong to an
# earlier image whose Popen objects (and their pending waits) the exec dropped.
_inherited_children = child_process_identities()
_inherited_children_lock = threading.Lock()
_lifecycle_managers: weakref.WeakSet[LifecycleManager] = weakref.WeakSet()


def reap_children_of_previous_image() -> None:
    """Reap the exited inherited children of this process, once per tick.

    Every manager's recorded worker stays pending: its manager records the
    exit evidence first and reaps it afterwards.
    """
    with _inherited_children_lock:
        if not _inherited_children:
            return
        pending = {
            (manager._state.get('worker') or {}).get('pid')
            for manager in list(_lifecycle_managers)}
        _inherited_children[:] = reap_inherited_children(
            _inherited_children, pending)


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


def worker_resource_budget_snapshot(pid: int | None) -> dict[str, Any]:
    """Expose a credential-free subset of the live worker's boot budget."""
    if not isinstance(pid, int) or pid <= 0:
        return {}
    wanted = {
        *WORKER_BUDGET_STATUS_KEYS,
        RESOURCE_BUDGET_POLICY_ENV,
        RESOURCE_BUDGET_AUTOMATIC_ENV,
    }
    found: dict[str, str] = {}
    try:
        for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
            key_bytes, separator, value_bytes = item.partition(b'=')
            if not separator:
                continue
            key = key_bytes.decode('utf-8', errors='ignore')
            if key in wanted:
                found[key] = value_bytes.decode('utf-8', errors='replace')
    except (OSError, ValueError, TypeError):
        return {}
    automatic = sorted({
        name for name in found.get(
            RESOURCE_BUDGET_AUTOMATIC_ENV, '').split(',')
        if name in RESOURCE_BUDGET_ENV_KEYS
    })
    policy_version = found.get(RESOURCE_BUDGET_POLICY_ENV, '')
    return {
        'policyVersion': policy_version or None,
        'currentPolicyVersion': RESOURCE_BUDGET_POLICY_VERSION,
        'policyCurrent': policy_version == RESOURCE_BUDGET_POLICY_VERSION,
        'automatic': automatic,
        'values': {
            name: found[name]
            for name in WORKER_BUDGET_STATUS_KEYS
            if name in found
        },
    }


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


def pid_is_server(pid: int) -> bool:
    if not pid_is_alive(pid):
        return False
    cmdline = proc_cmdline(pid)
    # An unreadable live process is ambiguous: report it as live so start
    # fails closed.  Stop applies stricter identity checks before signalling.
    return cmdline is None or 'server.py' in cmdline


# git replaces a changed file by unlink() then create, and a FUSE checkout can
# stretch that gap. No git operation removes or replaces the checkout root, so
# its absence needs only one confirming poll.
SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS = 10.0
SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS = 0.5


def _path_state(path: str) -> tuple[str, tuple[int, int] | None]:
    """('present', (st_dev, st_ino)), ('missing', None) or ('unknown', None)."""
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return 'missing', None
    except OSError:
        return 'unknown', None
    return 'present', (info.st_dev, info.st_ino)


class SupervisorOwnership:
    """Decide when the checkout or declared owner of a Supervisor is gone.

    The checkout owns the Supervisor while its source file exists. ``git
    merge``/``checkout`` replace a changed file by unlinking it and creating a
    new one, so one missing observation is not loss: the file must stay
    missing for ``SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS``, or for
    ``SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS`` once the checkout directory
    is gone (deleted or moved) or the path names a different directory than
    at start (moved away and recreated, e.g. by a later state write). Only
    ENOENT/ENOTDIR count as missing; an unreadable path neither confirms nor
    clears loss. The optional declared owner process is gone once it exited,
    an unreaped zombie included, or its PID names a process with other start
    ticks.
    """

    def __init__(self, source_file: str, owner_pid: int | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.source_file = os.path.realpath(source_file)
        self.checkout_dir = os.path.dirname(self.source_file)
        self.owner_pid = owner_pid
        self._owner_start_ticks = (
            proc_start_ticks(owner_pid) if owner_pid else None)
        self._checkout_identity = _path_state(self.checkout_dir)[1]
        self._clock = clock
        self._missing_since: float | None = None

    def lost(self) -> str:
        """'' while owned; otherwise why ownership is lost for good."""
        owner = self._owner_lost()
        if owner:
            return owner
        source, _identity = _path_state(self.source_file)
        if source == 'unknown':
            return ''
        now = self._clock()
        if source == 'present':
            if self._missing_since is not None:
                logger.info(
                    'supervisor ownership sentinel reappeared after %.2fs: %s',
                    now - self._missing_since, self.source_file)
                self._missing_since = None
            return ''
        checkout, identity = _path_state(self.checkout_dir)
        if checkout == 'missing':
            where = 'checkout directory is gone'
        elif (checkout == 'present' and self._checkout_identity is not None
                and identity != self._checkout_identity):
            where = 'checkout directory was replaced'
        else:
            where = 'checkout directory still exists'
        grace = (SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS
                 if where == 'checkout directory still exists'
                 else SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS)
        if self._missing_since is None:
            self._missing_since = now
            logger.warning(
                'supervisor ownership sentinel missing: %s (%s); ownership is '
                'lost only if it stays missing for %.1fs',
                self.source_file, where, grace)
            return ''
        missing_for = now - self._missing_since
        if missing_for < grace:
            return ''
        return (f'supervisor ownership sentinel disappeared: '
                f'{self.source_file} missing for {missing_for:.1f}s ({where})')

    def _owner_lost(self) -> str:
        if not self.owner_pid:
            return ''
        if not pid_is_alive(self.owner_pid):
            return f'supervisor owner process disappeared: pid={self.owner_pid}'
        ticks = proc_start_ticks(self.owner_pid)
        if (self._owner_start_ticks is not None and ticks is not None
                and ticks != self._owner_start_ticks):
            return (f'supervisor owner process disappeared: '
                    f'pid={self.owner_pid} now names a different process')
        return ''


_OFFLINE_STORAGE_COMMAND_LABELS = (
    ('scripts/storage_deep_clean.py', 'SQLite deep clean'),
    ('scripts/migrate_sqlite_to_postgres.py',
     'SQLite to PostgreSQL migration'),
)
_OFFLINE_STORAGECTL_COMMANDS = frozenset({
    'baseline', 'integrity-check', 'restore', 'handoff',
})


def _legacy_storage_lease_owner(cmdline: str | None) -> tuple[str, str]:
    """Classify old lease stamps without exposing the holder's command line."""
    command = str(cmdline or '')
    for marker, label in _OFFLINE_STORAGE_COMMAND_LABELS:
        if marker in command:
            return 'offline_maintenance', label
    if 'scripts/storagectl.py' in command:
        try:
            arguments = set(shlex.split(command))
        except ValueError:
            arguments = set(command.split())
        operation = next(
            (item for item in _OFFLINE_STORAGECTL_COMMANDS
             if item in arguments),
            None,
        )
        if operation:
            return 'offline_maintenance', f'Storage {operation}'
    if '-m lib.storage_authority_sql' in command:
        return 'storage_authority_sql', 'Storage sidecar'
    # The native Tofu-DB authority (``tofu-db serve``) is exec'd by
    # lib.storage_authority_sql.native_launch; it is the same child sidecar role,
    # never an independent daemon.
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if (len(tokens) >= 2
            and os.path.basename(tokens[0]).lower() in ('tofu-db', 'tofu-db.exe')
            and tokens[1] == 'serve'):
        return 'storage_authority_sql', 'Storage sidecar'
    return 'unknown', 'Storage operation'


def read_storage_lease_status(data_dir: str | Path) -> dict[str, Any]:
    """Inspect one storage lease using its OS lock as the sole authority.

    The JSON stamp is diagnostic metadata only.  A stale ``status=running``
    stamp never blocks startup; conversely, an unreadable held lock remains a
    fail-closed unknown owner.  Returned data deliberately excludes command
    lines, lease IDs and authority paths so the manager API cannot leak
    operator arguments or credentials.
    """
    root = Path(data_dir)
    lock_path = root / '.storage-sidecar.lock'
    lease_path = root / '.storage-sidecar-lease.json'
    result: dict[str, Any] = {
        'held': False,
        'kind': None,
        'label': None,
        'pid': None,
        'host': None,
        'startedAt': None,
        'ageSeconds': None,
        'holderVerified': False,
    }
    if not lock_path.is_file():
        return result
    try:
        handle = lock_path.open('r+b')
    except OSError:
        return {
            **result,
            'held': True,
            'kind': 'unknown',
            'label': 'Storage operation',
        }
    try:
        try:
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return result
        except (OSError, BlockingIOError):
            result['held'] = True
    finally:
        handle.close()

    stamp = _read_json(lease_path)
    host = str(stamp.get('host') or '').strip()[:255] or None
    try:
        pid = int(stamp.get('pid'))
        if pid <= 1:
            pid = None
    except (TypeError, ValueError):
        pid = None
    try:
        started_at = float(stamp.get('started_unix_ms')) / 1000.0
        if started_at <= 0:
            started_at = None
    except (TypeError, ValueError):
        started_at = None

    kind = str(stamp.get('owner_kind') or '').strip().lower()
    label = ' '.join(str(stamp.get('owner_label') or '').split())[:120]
    # ProjectLease marks the native Tofu-DB authority 'tofudb'; normalize it
    # to the sidecar role it plays so drain grace and orphan reaping apply.
    known_kinds = {
        'offline_maintenance': 'offline_maintenance',
        'storage_authority_sql': 'storage_authority_sql',
        'storage_operation': 'storage_operation',
        'tofudb': 'storage_authority_sql',
    }
    kind = known_kinds.get(kind, '')
    same_host = bool(host and host == _hostname())
    process_alive = bool(pid and same_host and pid_is_alive(pid))
    if process_alive:
        inferred_kind, inferred_label = _legacy_storage_lease_owner(
            proc_cmdline(pid))
        if not kind or inferred_kind == 'offline_maintenance':
            kind = inferred_kind
            label = inferred_label
    if not kind:
        kind = 'unknown'
    if not label:
        label = {
            'offline_maintenance': 'Offline storage maintenance',
            'storage_authority_sql': 'Storage sidecar',
            'storage_operation': 'Storage operation',
        }.get(kind, 'Storage operation')
    age_seconds = (
        max(0.0, _now() - started_at) if started_at is not None else None)
    return {
        **result,
        'kind': kind,
        'label': label,
        'pid': pid,
        'host': host,
        'startedAt': started_at,
        'ageSeconds': round(age_seconds, 1) if age_seconds is not None else None,
        'holderVerified': process_alive,
    }


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
        with create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _valid_server_port(raw: Any) -> int | None:
    try:
        port = int(raw)
    except (ValueError, TypeError):
        return None
    return port if 1 <= port <= 65535 else None


HTTP_PROBE_RESPONSE_LIMIT = 64 * 1024


def _read_probe_json(
    url: str, *, timeout: float, context: ssl.SSLContext | None = None,
) -> tuple[int, dict[str, Any]]:
    """Read one bounded JSON probe response, including structured HTTP errors."""
    try:
        with loopback_urlopen(
                url, timeout=timeout, context=context) as response:
            status = int(response.status)
            body = response.read(HTTP_PROBE_RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(HTTP_PROBE_RESPONSE_LIMIT + 1)
        finally:
            exc.close()
    if len(body) > HTTP_PROBE_RESPONSE_LIMIT:
        raise ValueError(
            f'probe response exceeds {HTTP_PROBE_RESPONSE_LIMIT} bytes')
    payload = json.loads(body.decode('utf-8') or '{}')
    if not isinstance(payload, dict):
        raise ValueError('probe response must be a JSON object')
    return status, payload


def _readiness_failure_detail(
    status: int, payload: dict[str, Any],
) -> str:
    error = payload.get('error')
    error_code = ''
    error_message = ''
    if isinstance(error, dict):
        error_code = str(error.get('code') or '').strip()
        error_message = str(error.get('message') or '').strip()
    elif error:
        error_message = str(error).strip()
    message = str(payload.get('message') or error_message or '').strip()
    state = str(payload.get('state') or 'unknown').strip()
    storage = payload.get('storage')
    storage_state = (
        str(storage.get('state') or 'unknown').strip()
        if isinstance(storage, dict) else 'unknown')
    reason = ': '.join(part for part in (error_code, message) if part)
    suffix = f'lifecycle={state}, storage={storage_state}, HTTP {status}'
    return f'{reason}; {suffix}' if reason else suffix


def _settle_readiness_probe(
    result: dict[str, Any],
    *,
    scheme: str,
    port: int,
    status: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Project one already-fetched readiness response onto a live worker."""
    storage = payload.get('storage')
    readiness_state = payload.get('state')
    storage_state = storage.get('state') if isinstance(storage, dict) else None
    ready = bool(
        200 <= status < 300
        and payload.get('ok') is True
        and payload.get('ready') is True)
    result.update({
        'ready': ready,
        'readinessState': readiness_state,
        'storageState': storage_state,
    })
    if ready:
        result['url'] = f'{scheme}://localhost:{port}'
        return result
    detail = 'application readiness failed: ' + _readiness_failure_detail(
        status, payload)
    result.update(readinessError=detail, error=detail)
    return result


def probe_application_readiness(
    port: int,
    expected_pid: int | None,
    *,
    preferred_scheme: str = '',
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Verify loopback worker identity and its dependency readiness.

    ``health`` remains a compatibility alias for identity liveness. It must
    not be used as traffic readiness; callers require both ``liveness`` and
    ``ready``. Current workers return their PID from ``/api/ready``, allowing
    one normal probe. Older workers fall back to the two-endpoint contract.
    """
    result: dict[str, Any] = {
        'health': False,
        'liveness': False,
        'ready': False,
        'scheme': None,
        'url': None,
        'liveUrl': None,
        'payloadPid': None,
        'pidMatches': False,
        'livenessError': '',
        'readinessError': '',
        'readinessState': None,
        'storageState': None,
    }
    checked_port = _valid_server_port(port)
    if checked_port is None:
        result['livenessError'] = f'invalid application port: {port!r}'
        result['error'] = result['livenessError']
        return result
    if not isinstance(expected_pid, int):
        result['livenessError'] = 'locked worker PID is unavailable'
        result['error'] = result['livenessError']
        return result

    schemes = ([preferred_scheme]
               if preferred_scheme in ('http', 'https') else [])
    schemes.extend(
        scheme for scheme in ('http', 'https') if scheme not in schemes)
    last_error = ''
    for scheme in schemes:
        context = ssl._create_unverified_context() if scheme == 'https' else None
        base_url = f'{scheme}://127.0.0.1:{checked_port}'
        ready_status = None
        ready_payload = None
        ready_error = ''
        try:
            ready_status, ready_payload = _read_probe_json(
                base_url + '/api/ready', timeout=timeout, context=context)
        except (OSError, ValueError, urllib.error.URLError,
                json.JSONDecodeError) as exc:
            ready_error = f'{scheme} readiness probe failed: {exc}'

        if ready_payload is not None and ready_payload.get('pid') is not None:
            payload_pid = ready_payload.get('pid')
            if payload_pid != expected_pid:
                last_error = (
                    'readiness response PID does not match the locked worker '
                    f'(expected {expected_pid}, got {payload_pid})')
                continue
            result.update({
                'health': True,
                'liveness': True,
                'scheme': scheme,
                'liveUrl': f'{scheme}://localhost:{checked_port}',
                'payloadPid': payload_pid,
                'pidMatches': True,
                'livenessError': '',
            })
            return _settle_readiness_probe(
                result,
                scheme=scheme,
                port=checked_port,
                status=int(ready_status),
                payload=ready_payload,
            )

        # Compatibility path for a worker generation whose readiness payload
        # predates the PID field, or when readiness transport itself failed.
        try:
            health_status, health_payload = _read_probe_json(
                base_url + '/api/health', timeout=timeout, context=context)
        except (OSError, ValueError, urllib.error.URLError,
                json.JSONDecodeError) as exc:
            last_error = f'{scheme} identity liveness probe failed: {exc}'
            continue

        payload_pid = health_payload.get('pid')
        pid_matches = payload_pid == expected_pid
        live = bool(
            200 <= health_status < 300
            and health_payload.get('ok') is True
            and pid_matches)
        if not live:
            if not pid_matches:
                last_error = (
                    'identity liveness response PID does not match the '
                    f'locked worker (expected {expected_pid}, got {payload_pid})')
            else:
                last_error = (
                    f'identity liveness probe returned HTTP {health_status} '
                    'without ok=true')
            continue

        result.update({
            'health': True,
            'liveness': True,
            'scheme': scheme,
            'liveUrl': f'{scheme}://localhost:{checked_port}',
            'payloadPid': payload_pid,
            'pidMatches': True,
            'livenessError': '',
        })
        if ready_payload is None:
            detail = ready_error or f'{scheme} readiness probe failed'
            result.update(readinessError=detail, error=detail)
            return result
        return _settle_readiness_probe(
            result,
            scheme=scheme,
            port=checked_port,
            status=int(ready_status),
            payload=ready_payload,
        )

    result['livenessError'] = last_error or 'identity liveness probe failed'
    result['error'] = result['livenessError']
    return result


def _explicit_server_port(args: list[str]) -> int | None:
    for index, arg in enumerate(args):
        if arg == '--port' and index + 1 < len(args):
            return _valid_server_port(args[index + 1])
        if arg.startswith('--port='):
            return _valid_server_port(arg.partition('=')[2])
    return None


def _server_port(args: list[str], env: dict[str, str] | None = None) -> int:
    explicit = _explicit_server_port(args)
    if explicit is not None:
        return explicit
    source = env if env is not None else os.environ
    return _valid_server_port(source.get('PORT')) or DEFAULT_SERVER_PORT


def _live_worker_port(status: dict[str, Any]) -> tuple[int | None, str]:
    """Return a verified worker's self-declared bind port and its source.

    The lifecycle state is durable intent, not proof of what an adopted or
    in-place re-executed process actually serves.  Prefer argv because it wins
    over ``PORT`` in ``server.py``; fall back to the process environment for a
    normal manager spawn.  Callers must establish worker identity first.
    """
    cmdline = status.get('cmdline')
    if isinstance(cmdline, str) and cmdline.strip():
        try:
            args = shlex.split(cmdline)
        except ValueError:
            args = cmdline.split()
        explicit = _explicit_server_port(args)
        if explicit is not None:
            return explicit, 'argv'
    pid = status.get('pid')
    if isinstance(pid, int):
        for name in ('_TOFU_RUNTIME_PORT', 'PORT'):
            observed = _valid_server_port(proc_env_value(pid, name))
            if observed is not None:
                return observed, f'env:{name}'
    return None, ''


def _rewrite_explicit_server_port(args: list[str], port: int) -> list[str]:
    """Keep stored argv consistent when an adopted worker corrects its port."""
    rewritten = list(args)
    for index, arg in enumerate(rewritten):
        if arg == '--port' and index + 1 < len(rewritten):
            rewritten[index + 1] = str(port)
            return rewritten
        if arg.startswith('--port='):
            rewritten[index] = f'--port={port}'
            return rewritten
    return rewritten


def _read_project_dotenv(project_path: str) -> dict[str, str]:
    """Read the project's current ``.env`` with the canonical parser.

    Every lifecycle read of the file goes through here, so the allowlisted
    view, the worker composition and preparation see identical values.  An
    unreadable, oversized or non-UTF-8 file reads as empty after a warning
    (once per distinct failure until the file reads again): the manager must
    stay up on inherited values instead of failing its construction, restart
    preparation or spawn.
    """
    dotenv = Path(project_path) / '.env'
    try:
        values = read_dotenv_values(dotenv)
    except OSError as exc:
        failure = (str(dotenv), str(exc))
        with _dotenv_warned_failures_lock:
            first_occurrence = failure not in _dotenv_warned_failures
            if first_occurrence:
                _dotenv_warned_failures[failure] = None
                while len(_dotenv_warned_failures) > _DOTENV_WARNING_MEMORY:
                    del _dotenv_warned_failures[next(iter(_dotenv_warned_failures))]
        if first_occurrence:
            logger.warning(
                'project .env not applied; inherited Supervisor values remain '
                'in effect: %s', exc)
        return {}
    with _dotenv_warned_failures_lock:
        for failure in [failure for failure in _dotenv_warned_failures
                        if failure[0] == str(dotenv)]:
            del _dotenv_warned_failures[failure]
    return values


def project_server_env(project_path: str) -> dict[str, str]:
    """Return the lifecycle-allowlisted values of the project's ``.env``."""
    return {
        key: value
        for key, value in _read_project_dotenv(project_path).items()
        if key in SERVER_ENV_KEYS
    }


def managed_worker_environment(
    project_path: str,
    manager_values: dict[str, str],
    server_env: dict[str, str],
) -> dict[str, str]:
    """Compose the launch environment of one Supervisor-managed worker.

    The long-lived Supervisor's ``os.environ`` is a snapshot of whichever
    shell launched it, and the worker's boot-time ``.env`` load never
    overrides an inherited name.  Every key the project's *current* ``.env``
    declares therefore replaces the ambient value here; undeclared keys keep
    it.  Precedence, lowest first: ambient, current ``.env``, manager-owned
    values, validated ``serverEnv`` overrides, then probed resource defaults
    for keys that are still empty.  An unreadable ``.env`` keeps the
    ambient-only composition after one warning.  Interpreter/loader bootstrap
    names (``_DOTENV_EXEC_EXCLUDED_*``) are not overlaid; the worker's boot
    loader still applies them post-start when they are not inherited.
    """
    env = os.environ.copy()
    env.update({
        key: value
        for key, value in _read_project_dotenv(project_path).items()
        if key not in _DOTENV_EXEC_EXCLUDED_KEYS
        and not key.startswith(_DOTENV_EXEC_EXCLUDED_PREFIXES)
    })
    env.update(manager_values)
    env.update({str(key): str(value) for key, value in server_env.items()})
    install_process_resource_defaults(env)
    return env


def _path_is_pytest_owned(raw_path: object, project_path: str) -> bool:
    """Return whether a path is inside a recognizable pytest-owned root."""
    value = str(raw_path or '').strip()
    if not value:
        return False
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(project_path) / path
        parts = path.resolve(strict=False).parts
    except (OSError, RuntimeError, ValueError):
        return False
    for part in parts:
        if part.startswith(('tofu-pytest-runs-', 'tofu-test-data-',
                            'tofu-test-storage-')):
            return True
        if part.startswith('pytest-of-'):
            return True
        if part.startswith('pytest-') and part[7:].isdigit():
            return True
        if part.startswith('popen-gw') and part[8:].isdigit():
            return True
    return False


def production_server_environment_error(
    project_path: str,
    environment: dict[str, object],
) -> str:
    """Reject test-owned state from a non-test (production) checkout.

    Test projects are allowed to use their disposable data roots. The unsafe
    combination is a durable/real checkout plus either a pytest process marker
    or a data-location variable rooted in pytest's temporary namespace.
    """
    if _path_is_pytest_owned(project_path, project_path):
        return ''
    for key in _DATA_LOCATION_ENV_KEYS:
        if _path_is_pytest_owned(environment.get(key), project_path):
            return (
                f'production lifecycle refused: {key} points into '
                'pytest-owned temporary storage')
    for key in _PYTEST_CONTEXT_ENV_KEYS:
        if str(environment.get(key) or '').strip():
            return (
                f'production lifecycle refused: inherited pytest context '
                f'({key})')
    if str(environment.get('TOFU_TESTING') or '').strip() == '1':
        return 'production lifecycle refused: inherited pytest context (TOFU_TESTING)'
    return ''


def run_frontend_preflight(
    project_path: str,
    python_executable: str,
    environment: dict[str, str],
    operation: str,
) -> tuple[bool, str]:
    """Run the project's repair-capable frontend gate in an isolated process.

    The manager intentionally imports no application dependency graph. The
    child command owns role selection, content-digest validation, the shared
    build lock, and optional Node repair. Its output inherits the bounded
    manager log rather than accumulating in an in-memory capture buffer.
    """
    command_path = Path(project_path) / 'serverctl.py'
    if not command_path.is_file():
        return False, f'frontend preflight command is missing: {command_path}'
    child_environment = dict(environment)
    child_environment['TOFU_PROJECT_PATH'] = str(Path(project_path).resolve())
    # Nested Node builders/canaries must use the same admitted interpreter;
    # a long-lived manager may legitimately have only /usr/bin:/bin in PATH.
    child_environment['TOFU_PYTHON'] = python_executable
    project_before = _read_project_dotenv(project_path)
    logger.info('frontend preflight starting (operation=%s)', operation)
    preflight_started = time.monotonic()
    try:
        from serverctl_pkg.native_engine import _run
        _run(
            [
                python_executable,
                str(command_path),
                'prepare-frontend',
                '--operation',
                str(operation or 'manager worker spawn')[:120],
            ],
            cwd=Path(project_path),
            environment=child_environment,
            timeout=DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT,
            progress='Preparing worker artifacts',
            termination_grace=40,
            failure_subject='Worker artifact preparation',
        )
    except (RuntimeError, KeyboardInterrupt) as exc:
        logger.error('frontend preflight failed after %.1fs (operation=%s): %s',
                     time.monotonic() - preflight_started, operation, exc)
        return False, str(exc) or 'Worker artifact preparation cancelled'
    except OSError as exc:
        return False, f'frontend preflight could not start: {exc}'
    elapsed = time.monotonic() - preflight_started
    # A successful native preparation replaces the project's managed pin.
    # Refresh inherited copies only when they still equal the old project
    # value; explicit external overrides retain their fail-closed semantics.
    project_after = _read_project_dotenv(project_path)
    for key in ('TOFUDB_BIN', 'TOFUDB_EXPECTED_SHA256'):
        if project_after.get(key):
            # Freeze project-managed artifact selection for this exact launch,
            # including pins previously read only from dotenv by the worker.
            if environment.get(key) in (None, project_before.get(key)):
                environment[key] = project_after[key]
            if project_before.get(key) and os.environ.get(key) == project_before[key]:
                os.environ[key] = project_after[key]
    logger.info(
        'frontend preflight passed in %.1fs (operation=%s)', elapsed, operation)
    return True, ''


def _source_interpreter(python: str) -> str:
    """``python``, or its source interpreter when it runs a runtime replica.

    Launchers may pass the serving worker's own interpreter, a replica
    generation that the manager must neither pin nor spawn without a lease.
    """
    try:
        from serverctl_pkg.runtime_replica import authoritative_interpreter
    except Exception as exc:  # a broken helper never blocks lifecycle control
        logger.warning('runtime replica helper unavailable: %s', exc)
        return python
    return authoritative_interpreter(python)


class LifecycleManager:
    """Own exactly one project's desired/observed server state."""

    def __init__(self, project_path: str, python_exe: str | None = None,
                 *, monitor_interval: float | None = None) -> None:
        self.project = os.path.realpath(project_path)
        self.python = _source_interpreter(
            python_exe or os.environ.get('TOFU_SUPERVISOR_PYTHON') or sys.executable)
        self.data_dir = Path(self.project) / 'data'
        self.logs_dir = Path(self.project) / 'logs'
        self.state_path = self.data_dir / 'server-manager-state.json'
        self.worker_log = self.logs_dir / 'server-console.log'
        from serverctl_pkg.native_storage_service import NativeStorageService
        self._native_storage = NativeStorageService(self.project, self.python)
        from serverctl_pkg.execution_service import ExecutionService
        self._execution = ExecutionService(self.project, self.python)
        self._last_log_maintenance_at = 0.0
        self._last_monitor_error = ''
        self._trace_requested_at: float | None = None
        self._monitor_error_count = 0
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self._worker_bytecode_cache_lock_fd: int | None = None
        self._worker_runtime_lock_fds: list[int] = []
        self._pending_runtime_build: tuple[str, list[str]] | None = None
        # Held until the generation is retired: a dropped Popen joins
        # subprocess._active, where the next Popen (``ss``, a preflight) reaps
        # the exited worker before its exit status is read.
        self._worker_process: subprocess.Popen | None = None
        self._state_needs_save = False
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
        self._state = self._load_state()
        if self._state_needs_save:
            self._save()
        _lifecycle_managers.add(self)
        self._adopt_existing()
        self._restore_worker_leases()

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
            # restartCount predates exit-intent accounting and may contain a
            # mixture of crashes and controlled signal/restart drains.  These
            # counters start at this version's accounting epoch instead of
            # laundering that historical ambiguity into precise telemetry.
            'workerFailureCount': 0,
            'executionRecovery': {},
            'plannedExitCount': 0,
            'exitAccountingSince': _now(),
            'lastWorkerExitAt': 0.0,
            'lastWorkerExitKind': '',
            'lastWorkerExitReason': '',
            'lastConsumedExitMarker': '',
            'pendingRecoverySource': '',
            'pendingWorkerExitIntent': {},
            'consecutiveFailures': 0,
            'failureHistory': [],
            'activeFailureAt': 0.0,
            'lastFailureAt': 0.0,
            'lastFailureReason': '',
            'lastExitCause': '',
            'lastCgroupOomDelta': 0,
            'lastCgroupOomKillCount': None,
            'workerBytecodeCache': {},
            'workerRuntime': {},
            'runtimeSuspect': {},
            'lastRecoveredAt': 0.0,
            'lastRecoverySeconds': None,
            'storageBlocker': {},
            'lastPortReconciledAt': 0.0,
            'lastPortReconciledFrom': None,
            'lastPortReconciledSource': '',
            'nextRetryAt': 0.0,
            'wedgeSince': 0.0,
            'lastError': '',
            'environmentQuarantine': {},
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
        normalized_environment = {
            str(key): str(value)
            for key, value in state['serverEnv'].items()
            if key in SERVER_ENV_KEYS
            and value is not None
            and str(value) != ''
        }
        if normalized_environment != state['serverEnv']:
            self._state_needs_save = True
        state['serverEnv'] = normalized_environment
        if not isinstance(state.get('failureHistory'), list):
            state['failureHistory'] = []
        if not isinstance(state.get('storageBlocker'), dict):
            state['storageBlocker'] = {}
        if not isinstance(state.get('workerBytecodeCache'), dict):
            state['workerBytecodeCache'] = {}
        if not isinstance(state.get('workerRuntime'), dict):
            state['workerRuntime'] = {}
        if not isinstance(state.get('runtimeSuspect'), dict):
            state['runtimeSuspect'] = {}
        if not isinstance(state.get('pendingRecoverySource'), str):
            state['pendingRecoverySource'] = ''
        if not isinstance(state.get('pendingWorkerExitIntent'), dict):
            state['pendingWorkerExitIntent'] = {}
        if not isinstance(state.get('environmentQuarantine'), dict):
            state['environmentQuarantine'] = {}

        project_environment_error = production_server_environment_error(
            self.project, self._project_env)
        persisted_environment_error = production_server_environment_error(
            self.project, state['serverEnv'])
        environment_error = project_environment_error or persisted_environment_error
        if environment_error:
            discarded_keys = sorted(state['serverEnv'])
            state['serverArgs'] = []
            if project_environment_error:
                # An explicit unsafe .env must be corrected by the operator;
                # silently falling back to a different authority is surprising.
                state['desired'] = 'stopped'
                state['serverEnv'] = {}
            else:
                # Persisted request provenance is unavailable. Discard the
                # whole forwarded envelope, not only the path that exposed it.
                state['serverEnv'] = dict(self._project_env)
            effective_environment = dict(os.environ)
            effective_environment.update(state['serverEnv'])
            state['port'] = _server_port([], effective_environment)
            state['environmentQuarantine'] = {
                'detectedAt': _now(),
                'reason': environment_error,
                'discardedKeys': discarded_keys,
            }
            state['observed'] = (
                'degraded' if state.get('desired') == 'running' else 'stopped')
            state['lastError'] = environment_error
            state['lastTransitionAt'] = _now()
            self._state_needs_save = True
            logger.error('%s; persisted worker environment was quarantined',
                         environment_error)
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
        self._state['executionRecovery'] = {}

    def _storage_data_dir(self) -> Path:
        """Resolve the worker's declared authority root without app imports."""
        environment = dict(os.environ)
        environment.update({
            str(key): str(value)
            for key, value in (self._state.get('serverEnv') or {}).items()
        })
        environment['TOFU_PROJECT_PATH'] = self.project
        return _persistent_data_path(environment)

    def _active_storage_lease(self) -> dict[str, Any]:
        return read_storage_lease_status(self._storage_data_dir())

    def _storage_lease_blocks_worker(self, lease: dict[str, Any]) -> bool:
        return bool(lease.get('held') and not self._native_storage.owns_lease(lease))

    def _recover_native_storage_service(self) -> None:
        """Recover a departed manager's host without changing a live app's engine."""
        from serverctl_pkg.native_storage_service import host_matches, process_matches, write_record
        record = self._native_storage.record()
        if not record or host_matches(record):
            return
        if record.get('state') == 'failed':
            raise RuntimeError(record.get('error') or 'native storage failed; explicit restart required')
        if self._active_storage_lease().get('held'):
            # The previous host's native child may still be finishing EOF
            # shutdown. Never launch a second host into its authority lease.
            return
        if (record.get('managerPid') == os.getpid()
                and process_matches(record['managerPid'], record.get('managerStartTime'))):
            # Engine exits belong to StorageRuntime's crash budget. A host
            # failure must not create a new runtime/budget on every reconcile.
            record['state'] = 'failed'
            record['error'] = 'native storage host exited unexpectedly; explicit restart required'
            write_record(self._native_storage.state_path, record)
            raise RuntimeError(record['error'])
        environment = self._worker_environment(self._state.get('serverEnv') or {}, self.port)
        plan = self._native_storage.prepare(environment)
        if not plan or record.get('plan') != plan:
            raise RuntimeError('native storage configuration changed; drain/restart the worker to replace it')
        self._native_storage.ensure(plan, environment)

    def _recover_execution_service(self) -> bool:
        """Recover the same execution generation without retiring the live API."""
        record = self._execution.record()
        if not record:
            return True  # Explicit/legacy topology has its own execution owner.
        if self._execution.host_matches(record):
            if record.get('state') == 'ready':
                self._state['executionRecovery'] = {}
                return True
            self._set_observed('degraded', str(record.get('error') or 'execution service is starting'))
            return False
        recovery = self._state.get('executionRecovery') or {}
        if recovery.get('serviceId') != record.get('serviceId'):
            now, recent, consecutive = self._account_failure()
            exhausted = recent >= self.max_failures
            recovery = {
                'serviceId': record.get('serviceId'),
                'retryAt': now + self._failure_retry_delay(consecutive, recent),
                'requiresRestart': exhausted,
                'error': ('execution recovery paused; explicit restart required' if exhausted
                          else 'execution service exited; waiting for recovery'),
            }
            self._state['executionRecovery'] = recovery
            self._state['lastFailureReason'] = recovery['error']
            self._state['lastExitCause'] = 'execution_exit'
        if recovery.get('requiresRestart') or _now() < recovery['retryAt']:
            self._set_observed('crashloop' if recovery.get('requiresRestart') else 'degraded',
                               recovery['error'])
            return False
        try:
            environment = self._worker_environment(self._state.get('serverEnv') or {}, self.port)
            environment.setdefault('TOFU_STORAGE_CONNECTION_FILE', self._native_storage.result_path())
            plan = self._execution.prepare(environment)
            if record.get('plan') != plan:
                raise RuntimeError('execution configuration or code changed; explicit restart required')
            self._execution.stop()
            self._execution.ensure(plan, environment)
        except Exception as exc:
            recovery.update(requiresRestart=True, error=f'execution recovery failed: {exc}')
            self._set_observed('degraded', recovery['error'])
            return False
        self._set_observed('degraded', 'execution service is starting')
        return False

    def _clean_exit_marker_for_remembered_worker(self) -> dict[str, Any]:
        """Return a fresh PID-bound clean-exit certificate, or an empty dict.

        The marker is application-owned and best-effort, so the manager fails
        safe unless every identity field agrees with the worker generation it
        actually supervised.  Timestamp fencing rejects a stale clean marker
        left by PID reuse or an earlier generation.
        """
        worker = self._state.get('worker') or {}
        pid = worker.get('pid')
        if not isinstance(pid, int):
            return {}
        marker = _read_json(
            self._storage_data_dir() / '.server_shutdown.json')
        reason = str(marker.get('reason') or '')
        if (marker.get('state') != 'clean'
                or marker.get('pid') != pid
                or marker.get('host') != _hostname()
                or reason not in CLEAN_WORKER_EXIT_REASONS):
            return {}
        try:
            clean_ts = float(marker.get('clean_ts'))
            spawned_at = float(worker.get('spawnedAt') or 0)
        except (TypeError, ValueError, OverflowError):
            return {}
        now = _now()
        if (not math.isfinite(clean_ts)
                or clean_ts < max(0.0, spawned_at - 1.0)
                or clean_ts > now + 5.0):
            return {}
        marker_key = f'{pid}:{clean_ts:.6f}:{reason}'
        return {
            'pid': pid,
            'reason': reason,
            'cleanTs': clean_ts,
            'markerKey': marker_key,
            'alreadyConsumed': (
                marker_key == self._state.get('lastConsumedExitMarker')),
        }

    def _pending_exit_intent_for_worker(
        self,
        pid: int,
        clean_ts: float,
    ) -> dict[str, Any]:
        """Return one fresh manager-authored intent for the exact worker."""
        intent = self._state.get('pendingWorkerExitIntent') or {}
        reason = str(intent.get('reason') or '')
        if (intent.get('pid') != pid
                or intent.get('host') != _hostname()
                or reason not in {'manual', 'restart'}):
            return {}
        try:
            requested_at = float(intent.get('requestedAt'))
            spawned_at = float(
                (self._state.get('worker') or {}).get('spawnedAt') or 0)
        except (TypeError, ValueError, OverflowError):
            return {}
        now = _now()
        if (not math.isfinite(requested_at)
                or requested_at < max(0.0, spawned_at - 1.0)
                or requested_at > now + 5.0
                or requested_at > clean_ts + 1.0
                or clean_ts - requested_at > DEFAULT_EXIT_INTENT_WINDOW):
            return {}
        return dict(intent)

    def _record_planned_exit(
        self,
        reason: str,
        *,
        pid: int | None,
        marker_key: str,
        recovery_source: str = '',
        exited_at: float | None = None,
    ) -> bool:
        """Consume one controlled exit without spending crash-loop budget."""
        if marker_key and marker_key == self._state.get(
                'lastConsumedExitMarker'):
            return False
        retired_worker = dict(self._state.get('worker') or {})
        now = _now()
        self._state['plannedExitCount'] = int(
            self._state.get('plannedExitCount') or 0) + 1
        self._state['lastWorkerExitAt'] = now if exited_at is None else exited_at
        self._state['lastWorkerExitKind'] = 'planned'
        self._state['lastWorkerExitReason'] = reason
        self._state['lastConsumedExitMarker'] = marker_key
        self._state['pendingRecoverySource'] = recovery_source
        self._state['pendingWorkerExitIntent'] = {}
        self._state['nextRetryAt'] = 0.0
        process_exit = self._worker_exit_observation(retired_worker)
        self._state['lastWorkerProcessExit'] = process_exit
        self._state['worker'] = {}
        self._release_worker_leases()
        logger.info(
            'worker controlled exit recorded pid=%s reason=%s recovery=%s',
            pid, reason, recovery_source or 'none')
        self._log_worker_exit(retired_worker, process_exit, logging.INFO)
        self._save()
        self._reap_retired_worker(retired_worker)
        return True

    def _worker_exit_observation(
            self, worker: dict[str, Any]) -> dict[str, Any]:
        """Exit status of a retired generation, read before it is reaped.

        Linux reads the unreaped zombie of a child this manager spawned or
        inherited across a /reload exec. Without /proc, this image's own
        Popen supplies the return code.
        """
        observation = proc_exit_observation(worker)
        process = self._worker_process
        if observation or process is None or process.pid != worker.get('pid'):
            return observation
        returncode = process.poll()
        if returncode is None:
            return {}
        self._worker_process = None
        return popen_exit_observation(
            process.pid, worker.get('processStartTime'), returncode)

    def _reap_retired_worker(self, worker: dict[str, Any]) -> None:
        """Reap a retired generation once its exit evidence is saved."""
        process = self._worker_process
        if process is not None and process.pid == worker.get('pid'):
            if process.poll() is not None:
                self._worker_process = None
            return
        reap_exited_child(worker.get('pid'), worker.get('processStartTime'))

    def _departed_worker_exit(self) -> dict[str, Any]:
        """Classify how the remembered generation exited once it is gone.

        Evidence: a fresh PID-bound clean marker, the manager's own PID intent
        persisted before it signalled, and the generation's ready certificate.
        ``planned`` names the reason, consumed marker, certificate time and
        the recovery that reason implies; ``failure`` carries the marker's
        classification when one exists, else the failure accounting
        classifies the remaining evidence.
        """
        clean_exit = self._clean_exit_marker_for_remembered_worker()
        if not clean_exit or clean_exit.get('alreadyConsumed'):
            return {'kind': 'failure'}
        reason = str(clean_exit['reason'])
        pid = int(clean_exit['pid'])
        clean_ts = float(clean_exit['cleanTs'])
        pending_intent = self._pending_exit_intent_for_worker(pid, clean_ts)
        if reason == 'signal' and pending_intent:
            reason = str(pending_intent['reason'])
        planned = {
            'kind': 'planned',
            'reason': reason,
            'pid': pid,
            'markerKey': str(clean_exit['markerKey']),
            'exitedAt': clean_ts,
        }
        if reason == 'manual':
            return {**planned, 'recoverySource': ''}
        if reason == 'restart':
            return {**planned, 'recoverySource': (
                str(pending_intent.get('recoverySource') or '')
                or 'clean-restart-recovery')}
        # Generic SIGTERM proves a graceful drain, not who intended
        # it. A manager-authored PID intent or explicit restart marker
        # already bypassed accounting above. A signal that arrives
        # after THIS generation reached readiness is an external drain
        # of a healthy worker, not a crash: restart it without
        # spending crash-loop budget, because on this host a single
        # boot can take 500-1000 s and must not be pushed toward the
        # terminal crashloop latch by an operator/agent restart.
        # Memory recycle still spends budget so a leak/recycle loop
        # cannot run forever.
        ready_logged = float(self._state.get('workerReadyLoggedAt') or 0.0)
        if reason == 'signal' and 0.0 < ready_logged <= clean_ts + 1.0:
            return {**planned, 'recoverySource': 'external-drain-recovery'}
        if reason == 'signal':
            return {
                'kind': 'failure',
                'exitedAt': clean_ts,
                'exitCause': 'unintended_signal',
                'failureReason': (
                    f'worker exited after unplanned graceful signal pid {pid}'),
            }
        return {
            'kind': 'failure',
            'exitedAt': clean_ts,
            'exitCause': 'memory_recycle',
            'failureReason': f'worker requested graceful memory recycle pid {pid}',
        }

    def _settle_departed_worker(self) -> None:
        """Record a gone generation's exit before anything retires its record.

        Reconciliation records an exit within one monitor tick, but an
        explicit start or stop can run first, e.g. as the first request to a
        Supervisor that was down when its worker exited. Replacing or clearing
        the record there loses the exit and status keeps an older one, so
        every retirement records the same classified exit first; recovery
        stays with the caller. Live, foreign-host and settled records are
        left alone.
        """
        worker = self._state.get('worker') or {}
        if (not isinstance(worker.get('pid'), int)
                or (worker.get('host') or _hostname()) != _hostname()
                or self._launcher_is_alive()):
            return
        logger.info(
            'worker pid=%s exited while unobserved; recording its exit before '
            'the record is retired', worker['pid'])
        departed = self._departed_worker_exit()
        if departed['kind'] == 'planned':
            self._record_planned_exit(
                departed['reason'],
                pid=departed['pid'],
                marker_key=departed['markerKey'],
                exited_at=departed['exitedAt'],
            )
            return
        *_, retired_worker = self._account_worker_failure(
            exit_cause=departed.get('exitCause'),
            failure_reason=departed.get('failureReason'),
            exited_at=departed.get('exitedAt'),
        )
        self._save()
        self._reap_retired_worker(retired_worker)

    @staticmethod
    def _log_worker_exit(worker: dict[str, Any], observation: dict[str, Any],
                         level: int) -> None:
        if observation:
            logger.log(level, 'worker process exit observation: %s', observation)
        else:
            logger.log(
                level, 'worker process exit observation unavailable pid=%s: '
                'not an unreaped child of this Supervisor process',
                worker.get('pid'))

    def _foreign_owner(self, status: dict[str, Any]) -> str:
        """Why the worker in question lives where this manager cannot see it.

        Several hosts can share one checkout (a network filesystem). A PID
        recorded on another host is meaningless here, so its absence is not
        an exit; judging it would record a false failure and start a second
        worker beside the live one.
        """
        here = _hostname()
        worker = self._state.get('worker') or {}
        host = worker.get('host')
        if worker.get('pid') and host and host != here:
            return (f'worker pid {worker["pid"]} runs on host {host}; this '
                    f'Supervisor on {here} cannot observe it, so it neither '
                    'records its exit nor starts another worker (start '
                    f'explicitly here only after {host} stopped serving '
                    'this checkout)')
        if status.get('sameHost') is False:
            return (f'instance lock is held by pid {status.get("pid")} on host '
                    f'{status.get("host")}; this Supervisor on {here} cannot '
                    'observe it and starts no worker beside it')
        return ''

    def _wait_for_stopped_sidecar_release(
        self,
        *,
        timeout: float = DEFAULT_SIDECAR_LEASE_RELEASE_WAIT,
    ) -> dict[str, Any]:
        """Let a just-stopped worker's child sidecar release its OS lease.

        A forced or slow worker shutdown can finish a fraction before the
        parent-watched sidecar exits.  Treating that short drain as a foreign
        storage authority makes an otherwise successful restart report 409,
        even though reconciliation starts the worker moments later.  Only the
        known sidecar kind gets this bounded grace; offline maintenance and
        unknown holders retain the normal fail-closed start behavior.
        """
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout))
        while True:
            lease = self._active_storage_lease()
            if self._native_storage.owns_lease(lease):
                return lease
            if (not lease.get('held')
                    or lease.get('kind') != 'storage_authority_sql'):
                waited = time.monotonic() - started
                if waited >= DEFAULT_STORAGE_LEASE_POLL_INTERVAL:
                    logger.info(
                        'stopped storage sidecar released its lease after %.1fs',
                        waited,
                    )
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    'stopped storage sidecar still holds its lease after %.1fs',
                    max(0.0, float(timeout)),
                )
                return self._reap_orphaned_storage_authority_sql(lease)
            time.sleep(min(DEFAULT_STORAGE_LEASE_POLL_INTERVAL, remaining))

    def _reap_orphaned_storage_authority_sql(
        self,
        lease: dict[str, Any],
        *,
        reap_timeout: float = DEFAULT_ORPHAN_SIDECAR_REAP_WAIT,
    ) -> dict[str, Any]:
        """Gracefully terminate an owned sidecar that outlived its worker.

        The storage sidecar is a child authority, never an independent
        daemon: with its worker gone it serves no client, and a fresh worker
        generation cannot start until the OS lease clears.  Reap only when
        the holder is a verified same-host process whose command line still
        matches a storage sidecar (guards against PID reuse); every other
        holder keeps the fail-closed start behavior for a human operator.
        """
        pid = lease.get('pid')
        if self._native_storage.owns_lease(lease):
            return lease
        if (not lease.get('held')
                or lease.get('kind') != 'storage_authority_sql'
                or not lease.get('holderVerified')
                or not isinstance(pid, int)):
            return lease
        inferred_kind, _ = _legacy_storage_lease_owner(proc_cmdline(pid))
        if inferred_kind != 'storage_authority_sql':
            logger.warning(
                'storage lease holder pid=%s no longer matches a sidecar '
                'identity; refusing to reap', pid)
            return lease
        logger.warning(
            'reaping orphaned storage sidecar pid=%s that survived its worker',
            pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return self._active_storage_lease()
        except OSError as exc:
            logger.warning(
                'orphaned storage sidecar pid=%s SIGTERM failed: %s', pid, exc)
            return lease
        deadline = time.monotonic() + max(0.0, float(reap_timeout))
        while time.monotonic() < deadline:
            lease = self._active_storage_lease()
            if not lease.get('held'):
                logger.info(
                    'orphaned storage sidecar pid=%s released its lease '
                    'after SIGTERM', pid)
                return lease
            time.sleep(DEFAULT_STORAGE_LEASE_POLL_INTERVAL)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return self._active_storage_lease()
        except OSError as exc:
            logger.warning(
                'orphaned storage sidecar pid=%s SIGKILL failed: %s', pid, exc)
            return lease
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            lease = self._active_storage_lease()
            if not lease.get('held'):
                logger.info(
                    'orphaned storage sidecar pid=%s released its lease '
                    'after SIGKILL', pid)
                return lease
            time.sleep(DEFAULT_STORAGE_LEASE_POLL_INTERVAL)
        logger.error(
            'orphaned storage sidecar pid=%s survived SIGKILL; '
            'start stays refused', pid)
        return lease

    @staticmethod
    def _storage_blocker_message(lease: dict[str, Any]) -> str:
        label = str(lease.get('label') or 'Storage operation')
        pid = lease.get('pid')
        host = lease.get('host')
        holder = (
            f'{label} (PID {pid})' if isinstance(pid, int)
            else (f'{label} on {host}' if host else label)
        )
        if lease.get('kind') == 'offline_maintenance':
            return (
                f'{holder} holds the storage lease; start is queued and will '
                'resume automatically after maintenance')
        return (
            f'{holder} holds the storage lease; refusing to start a second '
            'storage authority')

    def _defer_for_storage_lease(
        self,
        lease: dict[str, Any],
        *,
        resume_source: str = '',
    ) -> bool:
        """Persist a bounded startup blocker; return true for maintenance."""
        maintenance = lease.get('kind') == 'offline_maintenance'
        self._state['storageBlocker'] = {
            key: lease.get(key)
            for key in (
                'held', 'kind', 'label', 'pid', 'host', 'startedAt',
                'holderVerified',
            )
        }
        if resume_source:
            self._state['storageBlocker']['resumeSource'] = resume_source
        self._release_worker_leases()
        self._state['worker'] = {}
        self._set_observed(
            'maintenance' if maintenance else 'conflict',
            self._storage_blocker_message(lease),
        )
        self._save()
        return maintenance

    def _save(self) -> None:
        self._state['updatedAt'] = _now()
        _atomic_json(self.state_path, self._state)

    def _release_worker_leases(self) -> None:
        """Drop the bytecode-cache and runtime-replica leases of a gone worker."""
        descriptors = [self._worker_bytecode_cache_lock_fd, *self._worker_runtime_lock_fds]
        self._worker_bytecode_cache_lock_fd = None
        self._worker_runtime_lock_fds = []
        for descriptor in descriptors:
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _restore_worker_leases(self) -> None:
        """Protect an adopted worker's exact cache and runtime without pruning."""
        if not self._launcher_is_alive():
            return
        cache_status = self._state.get('workerBytecodeCache') or {}
        if cache_status.get('managed'):
            descriptor = None
            try:
                from serverctl_pkg.python_bytecode_cache import (
                    reacquire_server_python_cache_lease,
                )
                descriptor = reacquire_server_python_cache_lease(
                    self.project,
                    self.python,
                    str(cache_status.get('cacheRoot') or ''),
                    str(cache_status.get('namespace') or ''),
                )
            except Exception as exc:
                logger.warning('server bytecode cache lease restore skipped: %s', exc)
            if descriptor is None:
                logger.warning(
                    'server bytecode cache lease restore could not validate namespace')
            else:
                self._worker_bytecode_cache_lock_fd = descriptor
        runtime = self._state.get('workerRuntime') or {}
        generations = runtime.get('generationPaths') if runtime.get('replica') else None
        for generation in generations if isinstance(generations, list) else []:
            descriptor = None
            try:
                from serverctl_pkg.runtime_replica import lease_generation
                descriptor = lease_generation(str(generation))
            except Exception as exc:
                logger.warning('worker runtime replica lease restore skipped: %s', exc)
            if descriptor is None:
                logger.warning(
                    'worker runtime replica lease restore failed for %s', generation)
            else:
                self._worker_runtime_lock_fds.append(descriptor)

    def _request_bytecode_sweep(self) -> None:
        """Reclaim bytecode of removed source directories, off the launch path.

        Called on every healthy monitor pass; the helper returns at once
        unless the namespace's last sweep is older than its interval.
        """
        cache = self._state.get('workerBytecodeCache') or {}
        if not cache.get('managed'):
            return
        try:
            from serverctl_pkg.python_bytecode_cache import request_bytecode_sweep
            started = request_bytecode_sweep(
                str(cache.get('cacheRoot') or ''), str(cache.get('namespace') or ''),
                python=self.python)
        except Exception as exc:
            logger.warning('server bytecode sweep request failed: %s', exc)
            return
        if started:
            logger.info('server bytecode sweep started for %s', cache.get('namespace'))

    def _request_pending_runtime_build(self) -> None:
        """Copy missing/stale replicas once the worker no longer needs the volume."""
        pending, self._pending_runtime_build = self._pending_runtime_build, None
        if pending is None:
            return
        root, pythons = pending
        try:
            from serverctl_pkg.runtime_replica import request_build
            started = request_build(pythons, root, launcher=self.python)
        except Exception as exc:
            logger.warning('worker runtime replica build request failed: %s', exc)
            return
        if started:
            logger.info('worker runtime replica build started for %s in %s',
                        ', '.join(pythons), root)

    def _note_unready_runtime(self, reason: str) -> None:
        """Attribute a pre-readiness worker failure to its runtime, or not.

        A failure on a replica makes that generation suspect: the next spawn
        runs the source interpreter. A second failure there shows the cause
        is not the replica and clears the suspicion; readiness there
        confirms it (``_settle_runtime_suspect``). One unrelated failure
        therefore never discards a good replica.
        """
        if self._state.get('workerReadyLoggedAt'):
            return
        runtime = self._state.get('workerRuntime') or {}
        generations = runtime.get('generationPaths') or []
        if runtime.get('replica') and generations:
            self._state['runtimeSuspect'] = {
                'generationPath': str(generations[0]), 'reason': str(reason)[:500],
                'since': _now()}
            logger.warning(
                'worker failed before readiness on runtime replica %s; the next '
                'spawn uses the source interpreter to attribute the failure',
                generations[0])
        elif self._state.get('runtimeSuspect'):
            self._state['runtimeSuspect'] = {}
            logger.info('worker also failed before readiness on the source interpreter; '
                        'runtime replica suspicion cleared')

    def _settle_runtime_suspect(self) -> None:
        """The source interpreter became ready where the replica did not."""
        suspect = self._state.get('runtimeSuspect') or {}
        generation = suspect.get('generationPath')
        if not generation or (self._state.get('workerRuntime') or {}).get('replica'):
            return
        self._state['runtimeSuspect'] = {}
        try:
            from serverctl_pkg.runtime_replica import reject_generation
            reject_generation(str(generation), str(suspect.get('reason') or ''))
        except Exception as exc:
            logger.warning('worker runtime replica rejection failed: %s', exc)
            return
        logger.warning(
            'worker runtime replica %s rejected: it failed before readiness and '
            'the source interpreter did not', generation)

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

    def _reconcile_live_worker_port(self, status: dict[str, Any]) -> bool:
        """Adopt the endpoint declared by an already identity-checked worker.

        Direct/manual starts and in-place ``execv`` keep a legitimate worker
        alive without passing through ``_spawn``.  Persisted lifecycle intent
        can therefore drift from the real listener indefinitely, making the
        manager probe an unrelated port and later recover onto that stale
        endpoint.  Adoption means absorbing the live endpoint into both the
        status port and the next-spawn configuration.
        """
        observed, source = _live_worker_port(status)
        if observed is None or observed == self.port:
            return False
        previous = self.port
        self._state['port'] = observed
        server_env = dict(self._state.get('serverEnv') or {})
        server_env['PORT'] = str(observed)
        self._state['serverEnv'] = server_env
        self._state['serverArgs'] = _rewrite_explicit_server_port(
            [str(arg) for arg in self._state.get('serverArgs') or []], observed)
        self._state['lastPortReconciledAt'] = _now()
        self._state['lastPortReconciledFrom'] = previous
        self._state['lastPortReconciledSource'] = source
        logger.warning(
            'reconciled live worker port pid=%s persisted=%s observed=%s source=%s',
            status.get('pid'), previous, observed, source)
        return True

    def _adopt_existing(self) -> None:
        with self._lock:
            status = read_lock_status(self.project)
            if status.get('running'):
                identity_error = self._identity_error(status)
                if identity_error:
                    self._set_observed('conflict', identity_error)
                    self._save()
                    return
                self._reconcile_live_worker_port(status)
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
            self._maintain_process_logs(force=True)
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
                save_error = None
                with self._lock:
                    self._set_observed('degraded', f'monitor error: {exc}')
                    try:
                        self._save()
                    except OSError as state_exc:
                        save_error = state_exc
                signature = f'{type(exc).__name__}: {exc}'[:240]
                if signature == self._last_monitor_error:
                    self._monitor_error_count += 1
                else:
                    self._last_monitor_error = signature
                    self._monitor_error_count = 1
                occurrence = self._monitor_error_count
                if occurrence == 1 or occurrence & (occurrence - 1) == 0:
                    logger.error(
                        'lifecycle monitor reconcile failed '
                        '(occurrences=%d, degraded_state_saved=%s): %s',
                        occurrence, save_error is None, exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    if save_error is not None:
                        logger.warning(
                            'lifecycle monitor could not persist degraded '
                            'state: %s', save_error,
                        )
            else:
                self._last_monitor_error = ''
                self._monitor_error_count = 0

    def _maintain_process_logs(self, *, force: bool = False) -> None:
        """Bound inherited stdout files without application dependencies.

        The real checkout provides the shared policy implementation.  A
        standalone copied manager (used by recovery tooling/tests) deliberately
        degrades to a no-op so lifecycle control remains stdlib-only.
        """
        now = time.monotonic()
        if not force and now - self._last_log_maintenance_at < 900.0:
            return
        self._last_log_maintenance_at = now
        try:
            from lib.logging_pkg.log_policy import stream_backup_count, stream_max_bytes
            from lib.logging_pkg.log_retention import (
                copytruncate_if_oversize, ensure_private_log_directory,
                ensure_private_log_file,
            )
        except Exception:
            return
        ensure_private_log_directory(self.logs_dir)
        for name, path in (
                ('server_console', self.worker_log),
                ('server_manager', self.logs_dir / 'server-manager.log')):
            try:
                ensure_private_log_file(path, create=True)
                copytruncate_if_oversize(
                    path, max_bytes=stream_max_bytes(name),
                    backup_count=stream_backup_count(name))
            except Exception as exc:
                # Observability maintenance must never make start/stop/status
                # unavailable; the server janitor will retry the same policy.
                logger.warning(
                    'lifecycle log maintenance failed for stream=%s path=%s: %s',
                    name, path, exc,
                )
                continue

    def _worker_within_boot_grace(self) -> bool:
        spawned = float((self._state.get('worker') or {}).get('spawnedAt') or 0)
        return not spawned or _now() - spawned <= DEFAULT_BOOT_GRACE

    def _launcher_is_alive(self) -> bool:
        worker = self._state.get('worker') or {}
        pid = worker.get('pid')
        if (not isinstance(pid, int)
                or (worker.get('host') or _hostname()) != _hostname()
                or not pid_is_server(pid)):
            return False
        expected = worker.get('processStartTime')
        actual = proc_start_ticks(pid)
        return expected is None or actual is None or expected == actual

    def _http_probe(
        self, expected_pid: int, timeout: float = 2.0,
    ) -> dict[str, Any]:
        mode = ''
        try:
            mode = (self.data_dir / '.last_serve_mode').read_text().strip()
        except OSError:
            pass
        preferred_scheme = 'https' if mode == 'https' else 'http'
        return probe_application_readiness(
            self.port,
            expected_pid,
            preferred_scheme=preferred_scheme,
            timeout=timeout,
        )

    def _http_healthy(self, expected_pid: int, timeout: float = 2.0) -> bool:
        """Compatibility predicate for the complete readiness contract."""
        probe = self._http_probe(expected_pid, timeout=timeout)
        return bool(probe.get('liveness') and probe.get('ready'))

    def _heartbeat_state(self, pid: int) -> dict[str, Any] | None:
        root = (os.environ.get('TOFU_HEARTBEAT_DIR') or '').strip()
        path = Path(root or '/tmp/tofu/heartbeat') / 'server.heartbeat'
        value = _read_json(path)
        try:
            if int(value.get('pid')) != int(pid):
                return None
        except (ValueError, TypeError):
            return None
        return value

    def _heartbeat_age(self, pid: int) -> float | None:
        value = self._heartbeat_state(pid)
        if value is None:
            return None
        try:
            age = _now() - float(value.get('ts'))
        except (ValueError, TypeError):
            return None
        return age if age >= 0 else None

    def _heartbeat_stale_age(self, pid: int) -> float | None:
        """Age (s) iff the heartbeat proves this generation is wedged, else None.

        Phase-aware, mirroring lib/server_boot/lock.py's
        ``_holder_wedge_age``: a worker that still declares the ``booting``
        heartbeat phase is judged against the boot grace
        (``TOFU_BOOT_HEARTBEAT_GRACE_SECS``), not the serving-loop wedge
        threshold. This is what keeps a 500-1000 s FUSE boot from being
        terminated as a wedge. Every ambiguous reading (missing file,
        mismatched pid, future timestamp) returns None so the caller keeps the
        worker, exactly like the lock module.
        """
        value = self._heartbeat_state(pid)
        if value is None:
            return None
        try:
            age = _now() - float(value.get('ts'))
        except (ValueError, TypeError):
            return None
        if age < 0:
            return None
        threshold = (
            _boot_heartbeat_grace_seconds()
            if str(value.get('phase') or '') == 'booting'
            else DEFAULT_WEDGE_STALE)
        return age if age > threshold else None

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
        worker_environment = {
            key: proc_env_value(pid, key)
            for key in (
                *_DATA_LOCATION_ENV_KEYS,
                *_PYTEST_CONTEXT_ENV_KEYS,
                'TOFU_TESTING',
            )
        }
        environment_error = production_server_environment_error(
            self.project, worker_environment)
        if environment_error:
            return f'pid {pid} has unsafe environment: {environment_error}'
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
        if (not isinstance(pid, int)
                or (worker.get('host') or _hostname()) != _hostname()
                or not pid_is_alive(pid)):
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
            desired = self._state.get('desired')
            launcher_alive = self._launcher_is_alive()
            storage_lease: dict[str, Any] = {}
            if (desired == 'running' and not low.get('running')
                    and not launcher_alive):
                candidate = self._active_storage_lease()
                if self._storage_lease_blocks_worker(candidate):
                    storage_lease = candidate
            identity_error = self._identity_error(low) if low.get('running') else None
            foreign_owner = (
                self._foreign_owner(low)
                if desired == 'running' and not low.get('running')
                and not launcher_alive else '')
            if conflict or identity_error or foreign_owner:
                observed = 'conflict'
            elif low.get('running'):
                if self._state.get('desired') == 'stopped':
                    observed = 'stopping'
                else:
                    observed = 'running' if port_accepts(self.port) else 'starting'
            elif desired == 'stopped':
                observed = 'stopped'
            elif launcher_alive:
                observed = ('starting' if self._worker_within_boot_grace()
                            else 'degraded')
            elif storage_lease:
                observed = (
                    'maintenance'
                    if storage_lease.get('kind') == 'offline_maintenance'
                    else 'conflict')
            elif recent_failure_count >= self.max_failures:
                # Match the latch predicate in _record_failure exactly: only
                # the windowed count latches recovery. A stale
                # consecutiveFailures (failures spread beyond the window, where
                # recovery keeps retrying) must not be reported as crashloop;
                # a genuine latch is preserved because it set observed above
                # and no earlier branch fires.
                observed = 'crashloop'
            probe: dict[str, Any] = {}
            liveness = None
            ready = None
            if probe_health and low.get('running') and not conflict:
                probe = self._http_probe(int(low['pid']))
                liveness = probe.get('liveness') is True
                ready = probe.get('ready') is True
                if liveness and ready and observed == 'starting':
                    # A verified ready worker resolves an inconclusive bare
                    # TCP connect without overriding stopping or conflicts.
                    observed = 'running'
                if not (liveness and ready) and observed == 'running':
                    observed = 'degraded'
            last_error = foreign_owner or self._state.get('lastError') or ''
            if liveness is True and ready is True:
                # The returned snapshot should never contradict its own live
                # probe while the background reconcile is between ticks.
                last_error = ''
            elif probe:
                last_error = str(
                    probe.get('readinessError') if liveness
                    else probe.get('livenessError') or last_error)
            execution_record = self._execution.record()
            execution_error = ''
            if (desired == 'running' and execution_record and
                    (execution_record.get('state') != 'ready'
                     or not self._execution.host_matches(execution_record))):
                execution_error = str((self._state.get('executionRecovery') or {}).get('error')
                                      or execution_record.get('error')
                                      or 'execution service is not ready')
                ready = False
                last_error = execution_error
                if observed not in {'conflict', 'stopping', 'crashloop'}:
                    observed = 'degraded'
            worker_resource_budget = worker_resource_budget_snapshot(
                low.get('pid'))
            return {
                **low,
                'desired': self._state.get('desired'),
                'observed': observed,
                'port': self.port,
                # ``health`` is retained for older CLI consumers and means
                # identity liveness, exactly as it did before readiness became
                # an explicit second probe.
                'health': liveness,
                'liveness': liveness,
                'ready': ready,
                'probeScheme': probe.get('scheme'),
                'livenessError': probe.get('livenessError') or '',
                'readinessError': execution_error or probe.get('readinessError') or '',
                'readinessState': probe.get('readinessState'),
                'storageState': probe.get('storageState'),
                'storageLease': storage_lease or None,
                'nativeStorageService': {
                    key: value for key, value in self._native_storage.record().items()
                    if key in {'state', 'pid', 'nativePid', 'serviceId', 'error'}
                } or None,
                'executionService': {
                    key: value for key, value in execution_record.items()
                    if key in {'state', 'pid', 'serviceId', 'error'}
                } or None,
                'owner': 'tofu-manager',
                'managerPid': os.getpid(),
                'resourceBudgetPolicyVersion': RESOURCE_BUDGET_POLICY_VERSION,
                'workerResourceBudget': worker_resource_budget or None,
                'worker': worker,
                'restartCount': int(self._state.get('restartCount') or 0),
                'workerFailureCount': int(
                    self._state.get('workerFailureCount') or 0),
                'plannedExitCount': int(
                    self._state.get('plannedExitCount') or 0),
                'exitAccountingSince': float(
                    self._state.get('exitAccountingSince') or 0),
                'lastWorkerExitAt': float(
                    self._state.get('lastWorkerExitAt') or 0),
                'lastWorkerExitKind': (
                    self._state.get('lastWorkerExitKind') or ''),
                'lastWorkerExitReason': (
                    self._state.get('lastWorkerExitReason') or ''),
                'consecutiveFailures': int(self._state.get('consecutiveFailures') or 0),
                'recentFailureCount': recent_failure_count,
                'failureWindowSeconds': self.failure_window,
                'maxFailures': self.max_failures,
                'lastFailureAt': float(self._state.get('lastFailureAt') or 0),
                'lastFailureReason': self._state.get('lastFailureReason') or '',
                'lastExitCause': self._state.get('lastExitCause') or '',
                'lastWorkerProcessExit': dict(
                    self._state.get('lastWorkerProcessExit') or {}),
                'lastCgroupOomDelta': int(
                    self._state.get('lastCgroupOomDelta') or 0),
                'cgroupOomKillCount': self._state.get(
                    'lastCgroupOomKillCount'),
                'workerBytecodeCache': dict(
                    self._state.get('workerBytecodeCache') or {}),
                'workerRuntime': dict(
                    self._state.get('workerRuntime') or {}),
                'runtimeSuspect': dict(
                    self._state.get('runtimeSuspect') or {}),
                'lastRecoveredAt': float(self._state.get('lastRecoveredAt') or 0),
                'lastRecoverySeconds': self._state.get('lastRecoverySeconds'),
                'nextRetryAt': float(self._state.get('nextRetryAt') or 0),
                'lastError': last_error,
                'environmentQuarantine': dict(
                    self._state.get('environmentQuarantine') or {}),
                'launchSource': self._state.get('launchSource') or '',
                'serverArgs': list(self._state.get('serverArgs') or []),
                'serverEnv': dict(self._state.get('serverEnv') or {}),
                'foreignListenerPids': foreign,
                'workerLog': str(self.worker_log),
                'managerLog': str(self.logs_dir / 'server-manager.log'),
                'stateFile': str(self.state_path),
            }

    def _validated_server_environment(
        self,
        server_env: dict[str, str] | None,
    ) -> tuple[dict[str, str] | None, dict[str, str], str]:
        """Return project values, effective forwarded values, and an error."""
        current_project_environment = project_server_env(self.project)
        project_environment_error = production_server_environment_error(
            self.project, current_project_environment)
        if project_environment_error:
            return current_project_environment, {}, project_environment_error
        if server_env is None:
            candidate = {
                str(key): str(value)
                for key, value in (self._state.get('serverEnv') or {}).items()
            }
            project_environment = None
        else:
            unsafe = sorted(str(key) for key in set(server_env) - SERVER_ENV_KEYS)
            if unsafe:
                return None, {}, (
                    f'unsupported serverEnv key(s): {", ".join(unsafe)}')
            project_environment = current_project_environment
            candidate = {
                **project_environment,
                **{
                    str(key): str(value)
                    for key, value in server_env.items()
                    if value is not None and str(value) != ''
                },
            }
        effective_environment: dict[str, object] = dict(os.environ)
        effective_environment.update(candidate)
        environment_error = production_server_environment_error(
            self.project, effective_environment)
        return project_environment, candidate, environment_error

    def _worker_environment(self, server_env: dict[str, str], port: int) -> dict[str, str]:
        """Compose the same launch environment for preparation and spawning."""
        environment = managed_worker_environment(self.project, {
            'TOFU_SERVER_WORKER': '1',
            'TOFU_MANAGED_BY': 'supervisor',
            'TOFU_EXTERNAL_CONSOLE_LOG': str(self.worker_log),
            'TOFU_EXTERNAL_CONSOLE_STREAM': 'server_console',
            # The manager may supervise a project other than the checkout that
            # supplied this module. Probe that project's persistent data volume,
            # never an incidental source/supervisor filesystem.
            'TOFU_PROJECT_PATH': self.project,
            'PORT': str(port),
        }, server_env)
        return (self._native_storage.shared_environment(environment)
                if self._execution.enabled(environment) else environment)

    def _prepare_resident_services(self, environment: dict[str, str]):
        """Prepare every generation before replacing any serving process."""
        independent = self._execution.enabled(environment)
        env = (self._native_storage.shared_environment(environment)
               if independent else dict(environment))
        storage_plan = self._native_storage.prepare(env)
        execution_plan = None
        if independent:
            execution_env = dict(env)
            if storage_plan:
                execution_env['TOFU_STORAGE_CONNECTION_FILE'] = self._native_storage.result_path()
            if not execution_env.get('TOFU_STORAGE_CONNECTION_FILE'):
                raise RuntimeError('Independent execution requires a shared storage endpoint')
            execution_plan = self._execution.prepare(execution_env)
        return env, storage_plan, execution_plan

    def _ensure_resident_services(self, environment, storage_plan, execution_plan, *, reset_failed=False):
        """Reuse unchanged execution; replace dependencies only after joined drain."""
        env = dict(environment)
        storage_changes = self._native_storage.needs_replacement(storage_plan, reset_failed=reset_failed)
        execution_changes = self._execution.needs_replacement(execution_plan, reset_failed=reset_failed)
        if storage_changes or execution_changes:
            self._execution.stop()
        if storage_changes:
            self._native_storage.stop()
        if storage_plan:
            env['TOFU_STORAGE_CONNECTION_FILE'] = self._native_storage.ensure(storage_plan, env)
        if execution_plan:
            self._execution.ensure(execution_plan, env)
            env['TOFU_PROCESS_ROLE'] = 'api'
        return env

    def _stop_resident_services(self):
        self._execution.stop()
        self._native_storage.stop()

    def _restart_configuration(self) -> str:
        """Detect another lifecycle operation while preparation releases the lock."""
        worker = self._state.get('worker') or {}
        return json.dumps({
            'desired': self._state.get('desired'),
            'pid': worker.get('pid'),
            'processStartTime': worker.get('processStartTime'),
            'serverArgs': self._state.get('serverArgs'),
            'serverEnv': self._state.get('serverEnv'),
            'port': self.port,
        }, sort_keys=True)

    def _spawn(self, source: str, *,
               prepared_environment: dict[str, str] | None = None,
               prepared_storage_plan: dict | None = None,
               prepared_execution_plan: dict | None = None,
               reset_failed_storage: bool = False) -> dict[str, Any]:
        # A persisted planned-recovery intent is consumed by this exact spawn
        # attempt.  If process creation itself fails, _spawn latches a real
        # crashloop instead of replaying the old clean marker forever.
        self._state['pendingRecoverySource'] = ''
        self._state['pendingWorkerExitIntent'] = {}
        env = (dict(prepared_environment) if prepared_environment is not None
               else self._worker_environment(self._state.get('serverEnv') or {}, self.port))
        environment_error = production_server_environment_error(
            self.project, env)
        if environment_error:
            self._state['desired'] = 'stopped'
            self._state['environmentQuarantine'] = {
                'detectedAt': _now(),
                'reason': environment_error,
                'discardedKeys': [],
            }
            self._set_observed('stopped', environment_error)
            self._save()
            logger.error('%s; worker spawn was blocked', environment_error)
            return {'ok': False, 'message': environment_error}
        self._release_worker_leases()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._maintain_process_logs(force=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._disable_legacy_guard()
        # A restart carries its one verified launch environment across stop.
        # Cold starts/recovery still prepare here; never rebuild changing source
        # after an approved restart has removed the last serving worker.
        frontend_ready, frontend_error = (True, '')
        if prepared_environment is None:
            frontend_ready, frontend_error = run_frontend_preflight(
                self.project, self.python, env, f'manager {source}')
        if not frontend_ready:
            message = (
                'worker spawn refused during artifact preparation: '
                f'{frontend_error}')
            self._set_observed('crashloop', message)
            self._save()
            logger.error('%s', message)
            return {'ok': False, 'message': message}
        try:
            if prepared_environment is None:
                env, storage_plan, execution_plan = self._prepare_resident_services(env)
            else:
                storage_plan, execution_plan = prepared_storage_plan, prepared_execution_plan
            env = self._ensure_resident_services(env, storage_plan, execution_plan,
                                                reset_failed=reset_failed_storage)
        except Exception as exc:
            message = f'resident service preparation failed: {exc}'
            self._set_observed('crashloop', message)
            self._save()
            return {'ok': False, 'message': message}
        runtime = None
        worker_python = self.python
        runtime_status: dict[str, object] = {
            'replica': False,
            'reason': 'runtime-helper-unavailable',
            'python': self.python,
        }
        suspect = str((self._state.get('runtimeSuspect') or {}).get('generationPath') or '')
        try:
            from serverctl_pkg.runtime_replica import select_worker_runtime
            runtime = select_worker_runtime(self.python, env, avoid=suspect)
            worker_python = runtime.python
            runtime_status = {
                **runtime.status,
                'replica': runtime.replica,
                'reason': runtime.reason,
                'python': runtime.python,
                'generationPaths': list(runtime.generations),
            }
            self._pending_runtime_build = (
                (runtime.build_root, list(runtime.build_pythons))
                if runtime.build_pythons else None)
            if runtime.replica and suspect:
                # A newer generation superseded the suspect one.
                self._state['runtimeSuspect'] = {}
        except Exception as exc:
            logger.warning('worker runtime replica selection skipped: %s', exc)
        self._state['workerRuntime'] = runtime_status
        bytecode_activation = None
        bytecode_status: dict[str, object] = {
            'selected': False,
            'managed': False,
            'reason': 'cache-helper-unavailable',
        }
        try:
            from serverctl_pkg.python_bytecode_cache import (
                prepare_server_python_cache,
            )
            # Keyed by the source interpreter: a replica runs the identical
            # binary, and project-module bytecode stays valid across replica
            # generations and source/replica switches.
            bytecode_activation = prepare_server_python_cache(
                self.project, self.python, env)
            bytecode_status = bytecode_activation.as_status()
            if bytecode_activation.managed:
                env['PYTHONPYCACHEPREFIX'] = bytecode_activation.pycache_prefix
        except Exception as exc:
            logger.warning('server bytecode cache setup skipped: %s', exc)
        self._state['workerBytecodeCache'] = bytecode_status
        args = [str(item) for item in self._state.get('serverArgs') or []]
        try:
            log_fh = self.worker_log.open('ab')
        except OSError as exc:
            if bytecode_activation is not None:
                bytecode_activation.close_parent_lock()
            if runtime is not None:
                runtime.close_parent_lock()
            self._set_observed('crashloop', f'cannot open worker log: {exc}')
            self._save()
            return {'ok': False, 'message': str(exc)}
        try:
            proc = subprocess.Popen(
                [worker_python, 'server.py', *args], cwd=self.project, env=env,
                stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
        except Exception as exc:
            if bytecode_activation is not None:
                bytecode_activation.close_parent_lock()
            if runtime is not None:
                runtime.close_parent_lock()
            self._set_observed('crashloop', f'spawn failed: {exc}')
            self._save()
            return {'ok': False, 'message': f'spawn failed: {exc}'}
        finally:
            log_fh.close()
        if bytecode_activation is not None:
            self._worker_bytecode_cache_lock_fd = bytecode_activation.lock_fd
        if runtime is not None:
            self._worker_runtime_lock_fds = list(runtime.lease_fds)
        unrecorded, self._worker_process = self._worker_process, proc
        if unrecorded is not None:
            # Settlement reaps this image's child together with its record;
            # a child still referenced here left the record unsettled, e.g.
            # when adopting another live worker replaced it.
            returncode = unrecorded.poll()
            logger.warning(
                'worker pid=%s (%s) was replaced by generation pid=%s before '
                'its exit was recorded', unrecorded.pid,
                'still running' if returncode is None
                else f'returncode {returncode}', proc.pid)
        self._state['worker'] = {
            'pid': proc.pid,
            'host': _hostname(),
            'processStartTime': proc_start_ticks(proc.pid),
            'processCwd': self.project,
            'spawnedAt': _now(),
        }
        self._state['workerReadyLoggedAt'] = 0.0
        self._state['launchSource'] = source
        self._state['lastError'] = ''
        self._state['storageBlocker'] = {}
        self._set_observed('starting')
        self._save()
        logger.info(
            'worker spawned pid=%s source=%s%s; boot progress follows in %s',
            proc.pid, source, self._trace_total_suffix(), self.worker_log)
        return {
            'ok': True,
            'alreadyRunning': False,
            'launcherPid': proc.pid,
            'message': 'started; poll /status for readiness',
        }

    def start(self, *, server_args: list[str] | None = None,
              server_env: dict[str, str] | None = None,
              source: str = 'cli', explicit: bool = True,
              _prepared_environment: dict[str, str] | None = None,
              _prepared_storage_plan: dict | None = None,
              _prepared_execution_plan: dict | None = None) -> dict[str, Any]:
        with self._lock:
            if server_args is not None and not isinstance(server_args, list):
                return {'ok': False, 'message': 'serverArgs must be a JSON array'}
            if server_env is not None and not isinstance(server_env, dict):
                return {'ok': False, 'message': 'serverEnv must be a JSON object'}
            project_environment, forwarded_environment, environment_error = (
                self._validated_server_environment(server_env))
            if environment_error:
                return {'ok': False, 'message': environment_error}
            status = read_lock_status(self.project)
            if status.get('running'):
                identity_error = self._identity_error(status)
                if not identity_error:
                    self._reconcile_live_worker_port(status)
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
                self._project_env = dict(project_environment or {})
                self._state['serverEnv'] = forwarded_environment
                self._state['environmentQuarantine'] = {}
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
            self._settle_departed_worker()
            if explicit:
                self._clear_failure_budget()
            if self._launcher_is_alive():
                self._set_observed('starting')
                self._save()
                return {'ok': True, 'alreadyRunning': True,
                        'launcherPid': (self._state.get('worker') or {}).get('pid'),
                        'message': 'startup already in progress', **self.status()}
            storage_lease = self._wait_for_stopped_sidecar_release()
            if self._storage_lease_blocks_worker(storage_lease):
                maintenance = self._defer_for_storage_lease(storage_lease)
                return {
                    'ok': maintenance,
                    'alreadyRunning': False,
                    'launcherPid': None,
                    'waitingForMaintenance': maintenance,
                    'blockedByStorageLease': True,
                    'message': self._storage_blocker_message(storage_lease),
                    **self.status(),
                }
            if _prepared_environment is not None:
                return self._spawn(source, prepared_environment=_prepared_environment,
                                   prepared_storage_plan=_prepared_storage_plan,
                                   prepared_execution_plan=_prepared_execution_plan,
                                   reset_failed_storage=explicit)
            return self._spawn(source, reset_failed_storage=explicit)

    def _worker_shutdown_hard_seconds(self) -> float:
        """Mirror the worker's configured graceful-shutdown hard budget.

        The worker computes ``shutdown_hard_deadline_seconds`` from
        ``TOFU_SHUTDOWN_HARD_SECS`` in its effective environment (spawn env
        overlaying the project .env); the manager must wait out that budget
        before escalating, or it kills the shutdown sequence mid-flight.
        """
        raw = (
            str((self._state.get('serverEnv') or {}).get(
                'TOFU_SHUTDOWN_HARD_SECS') or '')
            or os.environ.get('TOFU_SHUTDOWN_HARD_SECS', '')
            or str((self._project_env or {}).get(
                'TOFU_SHUTDOWN_HARD_SECS') or ''))
        try:
            value = float(raw) if raw else DEFAULT_WORKER_SHUTDOWN_HARD_SECS
        except (TypeError, ValueError):
            value = DEFAULT_WORKER_SHUTDOWN_HARD_SECS
        return max(5.0, min(300.0, value))

    def _terminate_timeout_seconds(self) -> float:
        return (self._worker_shutdown_hard_seconds()
                + WORKER_TERMINATE_TIMEOUT_MARGIN)

    def _terminate(self, status: dict[str, Any], *, timeout: float | None = None) -> tuple[bool, bool, str]:
        if timeout is None:
            timeout = self._terminate_timeout_seconds()
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
        term_started = time.monotonic()
        logger.info(
            'worker pid=%s SIGTERM sent; waiting up to %.1fs for graceful '
            'shutdown', pid, timeout)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not pid_is_alive(pid):
                logger.info(
                    'worker pid=%s exited cleanly %.1fs after SIGTERM',
                    pid, time.monotonic() - term_started)
                return True, False, 'stopped cleanly'
            time.sleep(0.2)
        logger.warning(
            'worker pid=%s still alive %.1fs after SIGTERM; escalating to '
            'SIGKILL (worker shutdown sequence was pre-empted)',
            pid, time.monotonic() - term_started)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True, False, 'stopped cleanly'
        except OSError as exc:
            return False, False, f'SIGKILL failed: {exc}'
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not pid_is_alive(pid):
                logger.info(
                    'worker pid=%s exited after SIGKILL (%.1fs after SIGTERM)',
                    pid, time.monotonic() - term_started)
                return True, True, 'stopped with SIGKILL'
            time.sleep(0.1)
        return False, True, f'pid {pid} survived SIGKILL'

    def stop(self, *, source: str = 'cli', keep_desired_running: bool = False) -> dict[str, Any]:
        with self._lock:
            status = read_lock_status(self.project)
            foreign_owner = (
                '' if status.get('running') else self._foreign_owner(status))
            if foreign_owner:
                # desired=stopped written here would reach the owner host's
                # manager only through the shared state file, on its next
                # reload, and stop a worker this host never saw.
                self._set_observed('conflict', foreign_owner)
                return {'ok': False, 'wasRunning': False, 'exitCode': 1,
                        'message': foreign_owner, **self.status()}
            if not status.get('running'):
                status = self._remembered_worker_status() or status
            was_running = bool(status.get('running'))
            planned_reason = ''
            if was_running:
                self._remember_worker(status)
                planned_reason = (
                    'restart' if keep_desired_running else 'manual')
            else:
                self._settle_departed_worker()
            self._state['desired'] = 'running' if keep_desired_running else 'stopped'
            self._state['launchSource'] = source
            self._state['pendingWorkerExitIntent'] = ({
                'pid': status.get('pid'),
                'host': _hostname(),
                'reason': planned_reason,
                'requestedAt': _now(),
                'recoverySource': (
                    'manager-restart' if keep_desired_running else ''),
            } if was_running else {})
            if not keep_desired_running:
                self._state['storageBlocker'] = {}
            self._set_observed('stopping' if was_running else 'stopped')
            self._disable_legacy_guard()
            self._save()  # desired=stopped must win before the signal is sent
            ok, killed, message = self._terminate(status)
            if ok and not keep_desired_running:
                try:
                    self._stop_resident_services()
                except Exception as exc:
                    ok, message = False, f'resident service shutdown incomplete: {exc}'
            if ok:
                if was_running:
                    self._record_planned_exit(
                        planned_reason,
                        pid=status.get('pid'),
                        marker_key=(
                            f'manager:{status.get("pid")}:{_now():.6f}:'
                            f'{planned_reason}'),
                        recovery_source=(
                            'manager-restart' if keep_desired_running else ''),
                    )
                self._state['storageBlocker'] = {}
                self._set_observed('stopped')
                self._state['lastError'] = ''
            else:
                self._state['pendingWorkerExitIntent'] = {}
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
                source: str = 'cli',
                requested_at: float | None = None) -> dict[str, Any]:
        # User-visible restart epoch propagated from the initiating request;
        # stage logs reference it so a slow restart attributes its time.
        self._trace_requested_at = requested_at
        started = time.monotonic()
        # Validate and snapshot launch policy while the old worker serves.
        # Builds run outside the mutex so status/stop remain available.
        with self._lock:
            if server_args is not None and not isinstance(server_args, list):
                return {'ok': False, 'message': 'serverArgs must be a JSON array'}
            if server_env is not None and not isinstance(server_env, dict):
                return {'ok': False, 'message': 'serverEnv must be a JSON object'}
            if server_args is None and server_env is None:
                live = read_lock_status(self.project)
                if live.get('running') and not self._identity_error(live):
                    self._reconcile_live_worker_port(live)
            launch_args = [str(arg) for arg in (
                server_args if server_args is not None else self._state.get('serverArgs') or [])]
            if len(launch_args) > 32 or any(len(arg) > 4096 for arg in launch_args):
                return {'ok': False, 'message': 'serverArgs exceeds safety limit'}
            project_environment, forwarded_environment, environment_error = (
                self._validated_server_environment(server_env))
            if environment_error:
                return {'ok': False, 'message': environment_error}
            effective_env = {**os.environ, **forwarded_environment}
            launch_port = (_server_port(launch_args, effective_env)
                           if server_args is not None or server_env is not None else self.port)
            preparation_environment = self._worker_environment(forwarded_environment, launch_port)
            configuration = self._restart_configuration()
        ready, error = run_frontend_preflight(
            self.project, self.python, preparation_environment,
            f'manager {source} before shutdown')
        if not ready:
            return {'ok': False, 'message': error}
        try:
            preparation_environment, storage_plan, execution_plan = self._prepare_resident_services(
                preparation_environment)
        except Exception as exc:
            return {'ok': False, 'message': f'resident service preparation failed before shutdown: {exc}'}
        with self._lock:
            if configuration != self._restart_configuration():
                return {'ok': False, 'message': (
                    'Worker lifecycle changed during restart preparation; retry against current state.')}
            if server_args is not None:
                self._state['serverArgs'] = [str(arg) for arg in server_args]
            if server_env is not None:
                self._project_env = dict(project_environment or {})
                self._state['serverEnv'] = forwarded_environment
                self._state['environmentQuarantine'] = {}
            if server_args is not None or server_env is not None:
                effective_env = dict(os.environ)
                effective_env.update(self._state.get('serverEnv') or {})
                self._state['port'] = _server_port(
                    self._state.get('serverArgs') or [], effective_env)
            stopped = self.stop(source=source, keep_desired_running=True)
            if not stopped.get('ok'):
                logger.error(
                    'worker restart failed at stop stage after %.1fs%s: %s',
                    time.monotonic() - started,
                    self._trace_total_suffix(),
                    stopped.get('message') or 'stop refused')
                return stopped
            self._wait_for_stopped_sidecar_release()
            self._clear_failure_budget()
            result = self.start(server_args=self._state.get('serverArgs') or [],
                                server_env=self._state.get('serverEnv') or {},
                                source=source, explicit=True,
                                _prepared_environment=preparation_environment,
                                _prepared_storage_plan=storage_plan,
                                _prepared_execution_plan=execution_plan)
            if result.get('ok'):
                logger.info(
                    'worker restart replacement spawned in %.1fs%s',
                    time.monotonic() - started,
                    self._trace_total_suffix())
            else:
                logger.error(
                    'worker restart failed at start stage after %.1fs%s: %s',
                    time.monotonic() - started,
                    self._trace_total_suffix(),
                    result.get('message') or result)
            return result

    def _trace_total_suffix(self) -> str:
        requested_at = getattr(self, '_trace_requested_at', None)
        if not requested_at:
            return ''
        return f' totalSinceRequest={time.time() - requested_at:.1f}s'

    def reconcile(self) -> None:
        with self._lock:
            self._maintain_process_logs()
            reap_children_of_previous_image()
            status = read_lock_status(self.project)
            if not status.get('running') and self._state.get('desired') == 'stopped':
                status = self._remembered_worker_status() or status
            desired = self._state.get('desired')
            identity_error = self._identity_error(status) if status.get('running') else None
            if identity_error:
                self._set_observed('conflict', identity_error)
                self._save()
                return
            if status.get('running'):
                self._reconcile_live_worker_port(status)
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
                    # As in stop(), the PID intent precedes the signal, so
                    # the settlement below records a manual stop.
                    self._state['pendingWorkerExitIntent'] = {
                        'pid': status.get('pid'),
                        'host': _hostname(),
                        'reason': 'manual',
                        'requestedAt': _now(),
                        'recoverySource': '',
                    }
                    self._set_observed('stopping')
                    self._save()
                    ok, _, error = self._terminate(status)
                    if not ok:
                        self._set_observed('conflict', error)
                        self._save()
                        return
                self._settle_departed_worker()
                try:
                    self._stop_resident_services()
                except Exception as exc:
                    self._set_observed('conflict', f'resident service shutdown incomplete: {exc}')
                    self._save()
                    return
                self._state['pendingWorkerExitIntent'] = {}
                self._state['storageBlocker'] = {}
                self._set_observed('stopped')
                self._save()
                return
            if status.get('running'):
                try:
                    self._recover_native_storage_service()
                except Exception as exc:
                    self._set_observed('degraded', f'native storage recovery failed: {exc}')
                    self._save()
                    return
                self._remember_worker(status)
                execution_ready = self._recover_execution_service()
                probe = self._http_probe(int(status['pid']))
                if probe.get('liveness') and probe.get('ready'):
                    if not execution_ready:
                        self._save()
                        return
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
                    if not self._state.get('workerReadyLoggedAt'):
                        self._state['workerReadyLoggedAt'] = now
                        spawned_at = float(
                            (self._state.get('worker') or {}).get('spawnedAt')
                            or 0)
                        boot_elapsed = (
                            max(0.0, now - spawned_at) if spawned_at else None)
                        logger.info(
                            'worker ready pid=%s source=%s%s%s',
                            status.get('pid'),
                            self._state.get('launchSource') or 'unknown',
                            (f' bootTime={boot_elapsed:.1f}s'
                             if boot_elapsed is not None else ''),
                            self._trace_total_suffix())
                        self._settle_runtime_suspect()
                        self._request_pending_runtime_build()
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
                    self._request_bytecode_sweep()
                    return
                if probe.get('liveness'):
                    # Dependency readiness is not process liveness. Keep the
                    # worker available to finish startup/recovery and never
                    # feed a Sidecar outage into the wedge-restart policy.
                    self._state['wedgeSince'] = 0.0
                    self._set_observed(
                        'degraded',
                        str(probe.get('readinessError')
                            or 'application readiness probe failed'),
                    )
                    self._save()
                    return
                age = self._heartbeat_stale_age(int(status['pid']))
                if age is None:
                    self._set_observed(
                        'degraded',
                        str(probe.get('livenessError')
                            or 'identity liveness failed')
                        + '; heartbeat not stale',
                    )
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
                # A stale loop cannot prove its threads or pending writes are
                # dead. FUSE/network stalls can return after several minutes;
                # retain this generation and let its owned retries finish.
                # Explicit stop/restart and actual process-exit recovery retain
                # their separate authority and resource-safety boundaries.
                error = (
                    f'worker pid {status["pid"]} preserved during event-loop '
                    'stall; health probes and heartbeat remain unavailable; '
                    'waiting for in-place recovery')
                if self._state.get('lastError') != error:
                    logger.error('%s (heartbeat stale %.0fs)', error, age)
                self._set_observed('degraded', error)
                self._save()
                return
            if self._launcher_is_alive():
                if self._worker_within_boot_grace():
                    self._set_observed('starting')
                    self._save()
                    return
                # Still alive is never an exit: counting it as failed would
                # start a second generation beside this one.
                error = (
                    f'worker pid {self._state["worker"]["pid"]} is alive but '
                    f'holds no instance lock {DEFAULT_BOOT_GRACE:.0f}s after '
                    'spawn; retained, no second worker is started')
                if self._state.get('lastError') != error:
                    logger.error('%s', error)
                self._set_observed('degraded', error)
                self._save()
                return
            foreign_owner = self._foreign_owner(status)
            if foreign_owner:
                # No failure accounting, no spawn, and no write to the state
                # file shared with the owner host's manager.
                if self._state.get('lastError') != foreign_owner:
                    logger.warning('%s', foreign_owner)
                self._set_observed('conflict', foreign_owner)
                return
            prior_storage_blocker = dict(
                self._state.get('storageBlocker') or {})
            if prior_storage_blocker:
                storage_lease = self._active_storage_lease()
                if self._storage_lease_blocks_worker(storage_lease):
                    self._defer_for_storage_lease(
                        storage_lease,
                        resume_source=str(
                            prior_storage_blocker.get('resumeSource') or ''),
                    )
                    return
                self._state['storageBlocker'] = {}
                self._state['lastError'] = ''
                source = (
                    str(prior_storage_blocker.get('resumeSource') or '')
                    or str(self._state.get('pendingRecoverySource') or '')
                    or ('storage-maintenance-complete'
                        if prior_storage_blocker.get('kind')
                        == 'offline_maintenance'
                        else 'storage-lease-released'))
                retry_at = float(self._state.get('nextRetryAt') or 0)
                if retry_at and _now() < retry_at:
                    self._set_observed(
                        'starting',
                        f'storage lease released; retrying at {retry_at:.3f}',
                    )
                    self._save()
                    return
                if retry_at:
                    self._state['nextRetryAt'] = 0.0
                self._spawn(source)
                return

            pending_recovery = str(
                self._state.get('pendingRecoverySource') or '')
            if pending_recovery:
                storage_lease = self._wait_for_stopped_sidecar_release()
                if self._storage_lease_blocks_worker(storage_lease):
                    self._defer_for_storage_lease(
                        storage_lease, resume_source=pending_recovery)
                    return
                self._spawn(pending_recovery)
                return

            departed = self._departed_worker_exit()
            if departed['kind'] == 'planned':
                reason = departed['reason']
                recovery_source = departed['recoverySource']
                if reason == 'manual':
                    self._state['desired'] = 'stopped'
                self._record_planned_exit(
                    reason,
                    pid=departed['pid'],
                    marker_key=departed['markerKey'],
                    recovery_source=recovery_source,
                    exited_at=departed['exitedAt'],
                )
                if reason == 'manual':
                    self._state['storageBlocker'] = {}
                    self._set_observed('stopped')
                    self._state['lastError'] = ''
                    self._save()
                    return
                storage_lease = self._wait_for_stopped_sidecar_release()
                if self._storage_lease_blocks_worker(storage_lease):
                    self._defer_for_storage_lease(
                        storage_lease, resume_source=recovery_source)
                else:
                    self._spawn(recovery_source)
                return
            if departed.get('failureReason'):
                self._state['lastError'] = departed['failureReason']

            # Count the worker generation before waiting for its orphaned child
            # Sidecar lease.  The old order treated a real crash as mere storage
            # maintenance, then erased the failure budget when the lease fell.
            self._record_failure(
                exit_cause=departed.get('exitCause'),
                failure_reason=departed.get('failureReason'),
                exited_at=departed.get('exitedAt'),
            )
            if self._state.get('observed') == 'crashloop':
                return
            if self._launcher_is_alive():
                return
            storage_lease = self._wait_for_stopped_sidecar_release()
            if self._storage_lease_blocks_worker(storage_lease):
                self._defer_for_storage_lease(
                    storage_lease, resume_source='automatic-recovery')

    def _record_failure(
        self,
        *,
        exit_cause: str | None = None,
        failure_reason: str | None = None,
        exited_at: float | None = None,
    ) -> None:
        # A crash-loop is a latched operator boundary. Reconciliation keeps
        # observing the absent worker every monitor interval, but that is not
        # a new worker exit and must not inflate lifetime diagnostics (or turn
        # the manager state file into a periodic write source). An explicit
        # start/restart clears this latch via _clear_failure_budget().
        if self._state.get('observed') == 'crashloop':
            return
        retry_at = float(self._state.get('nextRetryAt') or 0)
        if retry_at:
            if _now() < retry_at:
                self._save()
                return
            self._state['nextRetryAt'] = 0.0
            if read_lock_status(self.project).get('running'):
                # The generation that failed is still alive (a slow boot whose
                # liveness the manager has not yet observed): spawning now
                # would race it for the instance lock. The caller's own
                # _launcher_is_alive guard runs after this method returns, so
                # it cannot cover this spawn.
                self._save()
                return
            self._spawn('automatic-recovery')
            return
        now, recent_failures, failures, retired_worker = (
            self._account_worker_failure(
                exit_cause=exit_cause,
                failure_reason=failure_reason,
                exited_at=exited_at,
            ))
        if recent_failures >= self.max_failures:
            self._set_observed('crashloop',
                               f'worker failed {recent_failures} times in '
                               f'{self.failure_window:.0f}s; recovery paused')
            self._state['nextRetryAt'] = 0.0
            self._save()
            self._reap_retired_worker(retired_worker)
            logger.error(
                'worker recovery paused after %d failures in %.0fs; explicit '
                'start/restart is required to reset the crash-loop budget',
                recent_failures, self.failure_window)
            return
        delay = self._failure_retry_delay(failures, recent_failures)
        self._state['nextRetryAt'] = now + delay
        self._set_observed('starting', f'worker exited; retrying in {delay:.0f}s')
        self._save()
        self._reap_retired_worker(retired_worker)

    @staticmethod
    def _failure_retry_delay(consecutive: int, recent: int) -> float:
        # Existing manager retry contract, shared by API and execution hosts.
        return min(60.0, float(2 ** max(consecutive, recent)))

    def _account_failure(self) -> tuple[float, int, int]:
        now = _now()
        history = self._recent_failure_times(now)
        history.append(now)
        history = history[-DEFAULT_FAILURE_HISTORY_KEEP:]
        failures = int(self._state.get('consecutiveFailures') or 0) + 1
        self._state['failureHistory'] = history
        self._state['activeFailureAt'] = float(self._state.get('activeFailureAt') or 0) or now
        self._state['lastFailureAt'] = now
        self._state['consecutiveFailures'] = failures
        return now, len(history), failures

    def _account_worker_failure(
        self,
        *,
        exit_cause: str | None,
        failure_reason: str | None,
        exited_at: float | None,
    ) -> tuple[float, int, int, dict[str, Any]]:
        """Record one failed generation's exit; retry policy stays with callers."""
        now, recent_failures, failures = self._account_failure()
        prior_oom = self._state.get('lastCgroupOomKillCount')
        current_oom = cgroup_oom_kill_count()
        oom_delta = 0
        if isinstance(prior_oom, int) and isinstance(current_oom, int):
            oom_delta = max(0, current_oom - prior_oom)
        if current_oom is not None:
            self._state['lastCgroupOomKillCount'] = current_oom
        prior_error = self._state.get('lastError') or ''
        retired_worker = dict(self._state.get('worker') or {})
        process_exit = self._worker_exit_observation(retired_worker)
        self._state['lastWorkerProcessExit'] = process_exit
        if exit_cause and failure_reason:
            classified_exit_cause = exit_cause
            classified_failure_reason = failure_reason
        elif oom_delta:
            classified_exit_cause = 'cgroup_oom_event'
            classified_failure_reason = (
                f'worker disappeared while shared cgroup oom_kill advanced '
                f'by {oom_delta}')
        elif prior_error.startswith('recovered wedged worker'):
            classified_exit_cause = 'health_wedge'
            classified_failure_reason = prior_error
        elif process_exit.get('signal'):
            classified_exit_cause = 'fatal_signal'
            classified_failure_reason = (
                f'worker pid={process_exit["pid"]} terminated with '
                f'{process_exit["signalName"]} ({process_exit["signal"]}); '
                'signal sender unknown; inspect the native core')
        else:
            classified_exit_cause = 'unexpected_exit'
            classified_failure_reason = (
                prior_error or 'worker exited unexpectedly')
        self._state['lastFailureReason'] = classified_failure_reason
        self._state['lastExitCause'] = classified_exit_cause
        self._state['lastWorkerExitAt'] = now if exited_at is None else exited_at
        self._state['lastWorkerExitKind'] = 'failure'
        self._state['lastWorkerExitReason'] = classified_exit_cause
        self._state['lastCgroupOomDelta'] = oom_delta
        self._state['restartCount'] = int(self._state.get('restartCount') or 0) + 1
        self._state['workerFailureCount'] = int(
            self._state.get('workerFailureCount') or 0) + 1
        self._state['pendingWorkerExitIntent'] = {}
        self._state['worker'] = {}
        self._note_unready_runtime(classified_failure_reason)
        self._release_worker_leases()
        logger.warning(
            'worker failure %d/%d in %.0fs window (consecutive=%d, cause=%s): %s',
            recent_failures, self.max_failures, self.failure_window,
            failures, classified_exit_cause, classified_failure_reason)
        self._log_worker_exit(retired_worker, process_exit, logging.WARNING)
        return now, recent_failures, failures, retired_worker

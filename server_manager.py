#!/usr/bin/env python3
"""Single-owner lifecycle state machine for the Tofu server worker.

This module intentionally uses only the Python standard library.  The manager
must remain observable and able to stop/recover the worker even when the
application dependency graph or database cannot be imported.
"""

from __future__ import annotations

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
import urllib.request
from pathlib import Path
from typing import Any

from runtime_guards import (
    RESOURCE_BUDGET_AUTOMATIC_ENV,
    RESOURCE_BUDGET_ENV_KEYS,
    RESOURCE_BUDGET_POLICY_ENV,
    RESOURCE_BUDGET_POLICY_VERSION,
    _persistent_data_path,
    deployment_resource_default,
    install_process_resource_defaults,
)


STATE_VERSION = 1
DEFAULT_SERVER_PORT = 15000
DEFAULT_MONITOR_INTERVAL = 5.0
DEFAULT_BOOT_GRACE = 180.0
DEFAULT_WEDGE_STALE = 180.0
DEFAULT_WEDGE_STREAK = 120.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_FAILURE_WINDOW = 120.0
DEFAULT_FAILURE_HISTORY_KEEP = 50
DEFAULT_WORKER_RSS_CGROUP_FRACTION = 0.70
DEFAULT_SIDECAR_LEASE_RELEASE_WAIT = 10.0
DEFAULT_STORAGE_LEASE_POLL_INTERVAL = 0.1
DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT = 660.0
DEFAULT_EXIT_INTENT_WINDOW = 120.0
CLEAN_WORKER_EXIT_REASONS = frozenset(
    {'manual', 'signal', 'restart', 'memory_recycle'})
MIB = 1024 * 1024
WORKER_BUDGET_STATUS_KEYS = (
    'TOFU_MAX_INFLIGHT_TASKS',
    'TOFU_AGENT_WORKERS',
    'TOFU_TASK_RSS_RESERVE_MB',
    'TOFU_PROCESS_RSS_RELIEF_MB',
    'TOFU_PROCESS_RSS_RECYCLE_MB',
)
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
    'TOFU_DEPLOYMENT_MODE',
    'TOFU_DATA_DIR', 'TOFU_DATA_LAYOUT',
    'XDG_DATA_HOME', 'LOCALAPPDATA',
    'TOFU_SERVER_PYTHON_CACHE', 'TOFU_SERVER_PYTHON_CACHE_DIR',
    # Agent scheduling is created before request handling starts. Preserve
    # explicit project overrides across manager-owned worker generations.
    'TOFU_AGENT_QUEUE_CAPACITY', 'TOFU_AGENT_STUCK_REPLACEMENTS',
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


def worker_rss_recycle_limit_bytes(
    raw_mb: str | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> int:
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
    default = int(deployment_resource_default(
        'TOFU_PROCESS_RSS_RECYCLE_MB', environment) * MIB)
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
    if '-m lib.storage_sidecar' in command:
        return 'storage_sidecar', 'Storage sidecar'
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
    known_kinds = {'offline_maintenance', 'storage_sidecar', 'storage_operation'}
    if kind not in known_kinds:
        kind = ''
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
            'storage_sidecar': 'Storage sidecar',
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
        with socket.create_connection((host, int(port)), timeout=timeout):
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
        with urllib.request.urlopen(
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
    try:
        completed = subprocess.run(
            [
                python_executable,
                str(command_path),
                'prepare-frontend',
                '--operation',
                str(operation or 'manager worker spawn')[:120],
            ],
            cwd=project_path,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            timeout=DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, (
            'frontend preflight exceeded '
            f'{DEFAULT_FRONTEND_PREFLIGHT_TIMEOUT:.0f}s')
    except OSError as exc:
        return False, f'frontend preflight could not start: {exc}'
    if completed.returncode:
        return False, (
            f'frontend preflight exited {completed.returncode}; '
            'see the manager log for the validation/build error')
    return True, ''


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
        self._last_log_maintenance_at = 0.0
        self._last_monitor_error = ''
        self._monitor_error_count = 0
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self._worker_bytecode_cache_lock_fd: int | None = None
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
        rss_limit_mb = (
            os.environ.get('TOFU_PROCESS_RSS_RECYCLE_MB')
            or self._project_env.get('TOFU_PROCESS_RSS_RECYCLE_MB'))
        profile_environment = dict(os.environ)
        profile_environment.update(self._project_env)
        self.worker_rss_recycle_bytes = worker_rss_recycle_limit_bytes(
            rss_limit_mb, environment=profile_environment)
        self._state = self._load_state()
        if self._state_needs_save:
            self._save()
        self._adopt_existing()
        self._restore_worker_bytecode_cache_lease()

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
            'workerRssBytes': None,
            'workerBytecodeCache': {},
            'lastMemoryRecycleAt': 0.0,
            'lastMemoryRecycleRssBytes': None,
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
    ) -> bool:
        """Consume one controlled exit without spending crash-loop budget."""
        if marker_key and marker_key == self._state.get(
                'lastConsumedExitMarker'):
            return False
        now = _now()
        self._state['plannedExitCount'] = int(
            self._state.get('plannedExitCount') or 0) + 1
        self._state['lastWorkerExitAt'] = now
        self._state['lastWorkerExitKind'] = 'planned'
        self._state['lastWorkerExitReason'] = reason
        self._state['lastConsumedExitMarker'] = marker_key
        self._state['pendingRecoverySource'] = recovery_source
        self._state['pendingWorkerExitIntent'] = {}
        self._state['nextRetryAt'] = 0.0
        self._state['worker'] = {}
        self._release_worker_bytecode_cache_lease()
        logger.info(
            'worker controlled exit recorded pid=%s reason=%s recovery=%s',
            pid, reason, recovery_source or 'none')
        self._save()
        return True

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
            if (not lease.get('held')
                    or lease.get('kind') != 'storage_sidecar'):
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
                return lease
            time.sleep(min(DEFAULT_STORAGE_LEASE_POLL_INTERVAL, remaining))

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
        self._release_worker_bytecode_cache_lease()
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

    def _release_worker_bytecode_cache_lease(self) -> None:
        descriptor = self._worker_bytecode_cache_lock_fd
        self._worker_bytecode_cache_lock_fd = None
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _restore_worker_bytecode_cache_lease(self) -> None:
        """Protect an adopted worker's exact cache without pruning it."""
        cache_status = self._state.get('workerBytecodeCache') or {}
        if not cache_status.get('managed') or not self._launcher_is_alive():
            return
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
            return
        if descriptor is None:
            logger.warning(
                'server bytecode cache lease restore could not validate namespace')
            return
        self._worker_bytecode_cache_lock_fd = descriptor

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
            from lib.log_policy import stream_backup_count, stream_max_bytes
            from lib.log_retention import (
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

    def _launcher_is_alive(self) -> bool:
        worker = self._state.get('worker') or {}
        pid = worker.get('pid')
        if not isinstance(pid, int) or not pid_is_server(pid):
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

    def _heartbeat_age(self, pid: int) -> float | None:
        root = (os.environ.get('TOFU_HEARTBEAT_DIR') or '').strip()
        path = Path(root or '/tmp/tofu/heartbeat') / 'server.heartbeat'
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
            desired = self._state.get('desired')
            launcher_alive = self._launcher_is_alive()
            storage_lease: dict[str, Any] = {}
            if (desired == 'running' and not low.get('running')
                    and not launcher_alive):
                candidate = self._active_storage_lease()
                if candidate.get('held'):
                    storage_lease = candidate
            identity_error = self._identity_error(low) if low.get('running') else None
            if conflict or identity_error:
                observed = 'conflict'
            elif low.get('running'):
                if self._state.get('desired') == 'stopped':
                    observed = 'stopping'
                else:
                    observed = 'running' if port_accepts(self.port) else 'starting'
            elif desired == 'stopped':
                observed = 'stopped'
            elif launcher_alive:
                observed = 'starting'
            elif storage_lease:
                observed = (
                    'maintenance'
                    if storage_lease.get('kind') == 'offline_maintenance'
                    else 'conflict')
            elif (int(self._state.get('consecutiveFailures') or 0) >= self.max_failures
                  or recent_failure_count >= self.max_failures):
                observed = 'crashloop'
            probe: dict[str, Any] = {}
            liveness = None
            ready = None
            if probe_health and low.get('running') and not conflict:
                probe = self._http_probe(int(low['pid']))
                liveness = probe.get('liveness') is True
                ready = probe.get('ready') is True
                if not (liveness and ready) and observed == 'running':
                    observed = 'degraded'
            last_error = self._state.get('lastError') or ''
            if liveness is True and ready is True:
                # The returned snapshot should never contradict its own live
                # probe while the background reconcile is between ticks.
                last_error = ''
            elif probe:
                last_error = str(
                    probe.get('readinessError') if liveness
                    else probe.get('livenessError') or last_error)
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
                'readinessError': probe.get('readinessError') or '',
                'readinessState': probe.get('readinessState'),
                'storageState': probe.get('storageState'),
                'storageLease': storage_lease or None,
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
                'lastCgroupOomDelta': int(
                    self._state.get('lastCgroupOomDelta') or 0),
                'cgroupOomKillCount': self._state.get(
                    'lastCgroupOomKillCount'),
                'workerRssBytes': self._state.get('workerRssBytes'),
                'workerRssRecycleBytes': self.worker_rss_recycle_bytes,
                'workerRssGuardEnabled': bool(self.worker_rss_recycle_bytes),
                'workerBytecodeCache': dict(
                    self._state.get('workerBytecodeCache') or {}),
                'lastMemoryRecycleAt': float(
                    self._state.get('lastMemoryRecycleAt') or 0),
                'lastMemoryRecycleRssBytes': self._state.get(
                    'lastMemoryRecycleRssBytes'),
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

    def _spawn(self, source: str) -> dict[str, Any]:
        # A persisted planned-recovery intent is consumed by this exact spawn
        # attempt.  If process creation itself fails, _spawn latches a real
        # crashloop instead of replaying the old clean marker forever.
        self._state['pendingRecoverySource'] = ''
        self._state['pendingWorkerExitIntent'] = {}
        env = os.environ.copy()
        env['TOFU_SERVER_WORKER'] = '1'
        env['TOFU_MANAGED_BY'] = 'supervisor'
        env['TOFU_EXTERNAL_CONSOLE_LOG'] = str(self.worker_log)
        env['TOFU_EXTERNAL_CONSOLE_STREAM'] = 'server_console'
        # The manager may supervise a project other than the checkout that
        # supplied this module. Probe that project's persistent data volume,
        # never an incidental source/supervisor filesystem.
        env['TOFU_PROJECT_PATH'] = self.project
        env['PORT'] = str(self.port)
        env.update({str(key): str(value)
                    for key, value in (self._state.get('serverEnv') or {}).items()})
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
        self._release_worker_bytecode_cache_lease()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._maintain_process_logs(force=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._disable_legacy_guard()
        # Apply native-process settings only after the project .env overlay is
        # present. glibc reads MALLOC_ARENA_MAX at exec time; translating the
        # Tofu knob before this merge would silently ignore a user's override.
        install_process_resource_defaults(env)
        self.worker_rss_recycle_bytes = worker_rss_recycle_limit_bytes(
            env.get('TOFU_PROCESS_RSS_RECYCLE_MB'), environment=env)
        frontend_ready, frontend_error = run_frontend_preflight(
            self.project,
            self.python,
            env,
            f'manager {source}',
        )
        if not frontend_ready:
            message = (
                'worker spawn refused before lifecycle change: '
                f'{frontend_error}')
            self._set_observed('crashloop', message)
            self._save()
            logger.error('%s', message)
            return {'ok': False, 'message': message}
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
            if bytecode_activation is not None:
                bytecode_activation.close_parent_lock()
            self._set_observed('crashloop', f'spawn failed: {exc}')
            self._save()
            return {'ok': False, 'message': f'spawn failed: {exc}'}
        finally:
            log_fh.close()
        if bytecode_activation is not None:
            self._worker_bytecode_cache_lock_fd = bytecode_activation.lock_fd
        self._state['worker'] = {
            'pid': proc.pid,
            'host': _hostname(),
            'processStartTime': proc_start_ticks(proc.pid),
            'processCwd': self.project,
            'spawnedAt': _now(),
        }
        self._state['launchSource'] = source
        self._state['lastError'] = ''
        self._state['storageBlocker'] = {}
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
            if explicit:
                self._clear_failure_budget()
            if self._launcher_is_alive():
                self._set_observed('starting')
                self._save()
                return {'ok': True, 'alreadyRunning': True,
                        'launcherPid': (self._state.get('worker') or {}).get('pid'),
                        'message': 'startup already in progress', **self.status()}
            storage_lease = self._active_storage_lease()
            if storage_lease.get('held'):
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
        self._release_worker_bytecode_cache_lease()
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
            planned_reason = ''
            if was_running:
                self._remember_worker(status)
                planned_reason = (
                    'restart' if keep_desired_running else 'manual')
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
                else:
                    self._state['worker'] = {}
                    self._release_worker_bytecode_cache_lease()
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
                source: str = 'cli') -> dict[str, Any]:
        with self._lock:
            if server_args is not None and not isinstance(server_args, list):
                return {'ok': False, 'message': 'serverArgs must be a JSON array'}
            if server_env is not None and not isinstance(server_env, dict):
                return {'ok': False, 'message': 'serverEnv must be a JSON object'}
            project_environment, forwarded_environment, environment_error = (
                self._validated_server_environment(server_env))
            if environment_error:
                return {'ok': False, 'message': environment_error}
            if server_args is None and server_env is None:
                live = read_lock_status(self.project)
                if live.get('running') and not self._identity_error(live):
                    self._reconcile_live_worker_port(live)
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
                return stopped
            self._wait_for_stopped_sidecar_release()
            self._clear_failure_budget()
            return self.start(server_args=self._state.get('serverArgs') or [],
                              server_env=self._state.get('serverEnv') or {},
                              source=source, explicit=True)

    def reconcile(self) -> None:
        with self._lock:
            self._maintain_process_logs()
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
                    self._set_observed('stopping')
                    self._save()
                    ok, _, error = self._terminate(status)
                    if not ok:
                        self._set_observed('conflict', error)
                        self._save()
                        return
                self._state['worker'] = {}
                self._state['pendingWorkerExitIntent'] = {}
                self._release_worker_bytecode_cache_lease()
                self._state['storageBlocker'] = {}
                self._set_observed('stopped')
                self._save()
                return
            if status.get('running'):
                self._remember_worker(status)
                if self._enforce_worker_rss_limit(status):
                    return
                probe = self._http_probe(int(status['pid']))
                if probe.get('liveness') and probe.get('ready'):
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
                age = self._heartbeat_age(int(status['pid']))
                if age is None or age < DEFAULT_WEDGE_STALE:
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
                ok, _, error = self._terminate(status, timeout=3.0)
                if not ok:
                    self._set_observed('conflict', error)
                    self._save()
                    return
                self._state['worker'] = {}
                self._release_worker_bytecode_cache_lease()
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
            prior_storage_blocker = dict(
                self._state.get('storageBlocker') or {})
            if prior_storage_blocker:
                storage_lease = self._active_storage_lease()
                if storage_lease.get('held'):
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
                storage_lease = self._active_storage_lease()
                if storage_lease.get('held'):
                    self._defer_for_storage_lease(
                        storage_lease, resume_source=pending_recovery)
                    return
                self._spawn(pending_recovery)
                return

            clean_exit = self._clean_exit_marker_for_remembered_worker()
            if clean_exit and not clean_exit.get('alreadyConsumed'):
                reason = str(clean_exit['reason'])
                pid = int(clean_exit['pid'])
                marker_key = str(clean_exit['markerKey'])
                pending_intent = self._pending_exit_intent_for_worker(
                    pid, float(clean_exit['cleanTs']))
                if (reason == 'signal'
                        and pending_intent.get('reason') == 'restart'):
                    reason = 'restart'
                if reason == 'manual':
                    self._state['desired'] = 'stopped'
                    self._record_planned_exit(
                        reason,
                        pid=pid,
                        marker_key=marker_key,
                    )
                    self._state['storageBlocker'] = {}
                    self._set_observed('stopped')
                    self._state['lastError'] = ''
                    self._save()
                    return
                if reason == 'restart':
                    recovery_source = (
                        str(pending_intent.get('recoverySource') or '')
                        or 'clean-restart-recovery')
                    self._record_planned_exit(
                        reason,
                        pid=pid,
                        marker_key=marker_key,
                        recovery_source=recovery_source,
                    )
                    storage_lease = self._active_storage_lease()
                    if storage_lease.get('held'):
                        self._defer_for_storage_lease(
                            storage_lease, resume_source=recovery_source)
                    else:
                        self._spawn(recovery_source)
                    return
                # Generic SIGTERM proves a graceful drain, not who intended
                # it. Only a manager-authored PID intent or explicit restart
                # marker bypasses accounting. Memory recycle also spends
                # budget so a leak/recycle loop cannot run forever.
                exit_cause = (
                    'unintended_signal' if reason == 'signal' else
                    'memory_recycle')
                self._state['lastError'] = (
                    f'worker exited after unplanned graceful signal pid {pid}'
                    if reason == 'signal' else
                    f'worker requested graceful memory recycle pid {pid}')
                self._record_failure(
                    exit_cause=exit_cause,
                    failure_reason=self._state['lastError'],
                )
                if self._state.get('observed') == 'crashloop':
                    return
                if self._launcher_is_alive():
                    return
                storage_lease = self._active_storage_lease()
                if storage_lease.get('held'):
                    self._defer_for_storage_lease(
                        storage_lease, resume_source='automatic-recovery')
                return

            # Count the worker generation before waiting for its orphaned child
            # Sidecar lease.  The old order treated a real crash as mere storage
            # maintenance, then erased the failure budget when the lease fell.
            self._record_failure()
            if self._state.get('observed') == 'crashloop':
                return
            if self._launcher_is_alive():
                return
            storage_lease = self._active_storage_lease()
            if storage_lease.get('held'):
                self._defer_for_storage_lease(
                    storage_lease, resume_source='automatic-recovery')

    def _record_failure(
        self,
        *,
        exit_cause: str | None = None,
        failure_reason: str | None = None,
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
        if exit_cause and failure_reason:
            classified_exit_cause = exit_cause
            classified_failure_reason = failure_reason
        elif prior_error.startswith('manager RSS ceiling exceeded'):
            classified_exit_cause = 'manager_rss_recycle'
            classified_failure_reason = prior_error
        elif oom_delta:
            classified_exit_cause = 'cgroup_oom_event'
            classified_failure_reason = (
                f'worker disappeared while shared cgroup oom_kill advanced '
                f'by {oom_delta}')
        elif prior_error.startswith('recovered wedged worker'):
            classified_exit_cause = 'health_wedge'
            classified_failure_reason = prior_error
        else:
            classified_exit_cause = 'unexpected_exit'
            classified_failure_reason = (
                prior_error or 'worker exited unexpectedly')
        self._state['failureHistory'] = history
        self._state['activeFailureAt'] = (
            float(self._state.get('activeFailureAt') or 0) or now)
        self._state['lastFailureAt'] = now
        self._state['lastFailureReason'] = classified_failure_reason
        self._state['lastExitCause'] = classified_exit_cause
        self._state['lastWorkerExitAt'] = now
        self._state['lastWorkerExitKind'] = 'failure'
        self._state['lastWorkerExitReason'] = classified_exit_cause
        self._state['lastCgroupOomDelta'] = oom_delta
        self._state['consecutiveFailures'] = failures
        self._state['restartCount'] = int(self._state.get('restartCount') or 0) + 1
        self._state['workerFailureCount'] = int(
            self._state.get('workerFailureCount') or 0) + 1
        self._state['pendingWorkerExitIntent'] = {}
        self._state['worker'] = {}
        self._release_worker_bytecode_cache_lease()
        logger.warning(
            'worker failure %d/%d in %.0fs window (consecutive=%d, cause=%s): %s',
            recent_failures, self.max_failures, self.failure_window,
            failures, classified_exit_cause, classified_failure_reason)
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

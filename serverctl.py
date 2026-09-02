#!/usr/bin/env python3
"""Human-facing lifecycle CLI for the project-local Tofu manager."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from runtime_guards import resource_budget_manifest
from server_manager import (
    listener_pids,
    probe_application_readiness,
    read_lock_status,
)
from tofu_dotenv import parse_env_boolean, read_dotenv_values


PROJECT = os.path.realpath(os.environ.get('TOFU_PROJECT_PATH') or
                           os.path.dirname(os.path.abspath(__file__)))
MANAGER_HOST = os.environ.get('TOFU_SUPERVISOR_HOST', '127.0.0.1')
try:
    MANAGER_PORT = int(os.environ.get('TOFU_SUPERVISOR_PORT', '15001'))
except ValueError:
    MANAGER_PORT = 15001
BASE_URL = f'http://{MANAGER_HOST}:{MANAGER_PORT}'
# supervisor.sh intentionally waits up to 20 seconds for a detached watchdog
# on a saturated host. The caller must outlive that whole budget plus shell
# cleanup; a shorter timeout leaves a healthy manager behind while reporting
# launch failure.
_MANAGER_LAUNCH_TIMEOUT_FLOOR = 25.0
_FRONTEND_REPAIR_TIMEOUT_SECONDS = 600.0
STARTUP_STUCK_SECONDS = 300.0

_CGROUP_V2_USAGE = '/sys/fs/cgroup/memory.current'
_CGROUP_V2_LIMIT = '/sys/fs/cgroup/memory.max'
_CGROUP_V2_SWAP_LIMIT = '/sys/fs/cgroup/memory.swap.max'
_CGROUP_V2_EVENTS = '/sys/fs/cgroup/memory.events'
_CGROUP_V1_USAGE = '/sys/fs/cgroup/memory/memory.usage_in_bytes'
_CGROUP_V1_LIMIT = '/sys/fs/cgroup/memory/memory.limit_in_bytes'
_CGROUP_V1_MEMSW_LIMIT = '/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes'
_CGROUP_V1_OOM = '/sys/fs/cgroup/memory/memory.oom_control'
_CGROUP_V1_FAILCNT = '/sys/fs/cgroup/memory/memory.failcnt'


class ManagerUnavailable(RuntimeError):
    pass


def _control_command(*arguments: object) -> str:
    """Return one cwd-independent, shell-copyable serverctl invocation."""
    script = str(Path(PROJECT) / 'serverctl.py')
    return shlex.join([sys.executable, script, *(str(item) for item in arguments)])


def _bounded_log_lines(value: str) -> int:
    try:
        lines = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('must be an integer from 1 to 1000') from exc
    if not 1 <= lines <= 1000:
        raise argparse.ArgumentTypeError('must be an integer from 1 to 1000')
    return lines


def _wait_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('must be between 0 and 3600 seconds') from exc
    if not 0 <= seconds <= 3600:
        raise argparse.ArgumentTypeError('must be between 0 and 3600 seconds')
    return seconds


def _tcp_port(value: object, default: int = 15000) -> int:
    """Return a valid port without letting broken config crash diagnostics."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _valid_tcp_port(value: object) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _version_text() -> str:
    try:
        version = (Path(PROJECT) / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        version = 'unknown'
    return f'Tofu {version or "unknown"}'


def _login_base_url(explicit: str = '') -> str:
    """Resolve one browser origin, normalizing listener-only host values."""
    configured = (explicit or os.environ.get('TOFU_PUBLIC_URL') or '').strip()
    if configured:
        candidate = configured
    else:
        raw_tls = _project_setting(PROJECT, 'TOFU_TLS', '').strip()
        tls = parse_env_boolean(raw_tls)
        if raw_tls and tls is None:
            raise ValueError(
                f'unsupported TOFU_TLS={raw_tls!r}; run serverctl.py doctor')
        scheme = 'https' if tls else 'http'
        published_port = _valid_tcp_port(os.environ.get('TOFU_PUBLISHED_PORT'))
        port = published_port or _configured_port_snapshot(PROJECT)['port']
        host = (os.environ.get('TOFU_PUBLIC_HOST') or
                _project_setting(PROJECT, 'BIND_HOST', 'localhost')).strip()
        if not host or host in ('0.0.0.0', '::', '[::]'):
            host = 'localhost'
        elif ':' in host and not host.startswith('['):
            host = f'[{host}]'
        candidate = f'{scheme}://{host}:{port}'

    parsed = urllib.parse.urlsplit(candidate)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f'invalid login base URL port: {candidate!r}') from exc
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('login base URL must be an http(s) origin')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            'login base URL must not contain credentials, a query, or a fragment')
    if parsed.path not in ('', '/'):
        raise ValueError('login base URL must not contain a path')
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ValueError('login base URL port must be between 1 and 65535')
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, '', '', '')).rstrip('/')


def _resolved_auth_mode() -> str:
    from lib.auth_mode import get_mode
    return get_mode()


def _read_first_run_token() -> tuple[str, Path]:
    """Read and validate the deliberately recoverable bootstrap credential."""
    from lib import api_keys

    path = Path(api_keys._FIRST_RUN_TOKEN_FILE)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f'bootstrap token path is not a regular file: {path}')
    if metadata.st_mode & 0o077:
        raise PermissionError(
            f'bootstrap token permissions are too broad: {path}; run chmod 600')
    if not 1 <= metadata.st_size <= 8192:
        raise ValueError(f'bootstrap token file has an invalid size: {path}')
    token = path.read_text(encoding='utf-8').strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError(f'bootstrap token file is malformed: {path}')
    if api_keys.validate_token(token) is None:
        raise ValueError(f'bootstrap token is stale or revoked: {path}')
    return token, path


def cmd_login_url(args: argparse.Namespace) -> int:
    """Print a copyable browser URL only after an explicit operator request."""
    try:
        base_url = _login_base_url(args.base_url)
        mode = _resolved_auth_mode()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Could not resolve login URL: {exc}', file=sys.stderr)
        return 1

    if mode == 'open':
        print('Auth mode is open; no login token is required.')
        print(f'Open: {base_url}')
        return 0

    try:
        token, path = _read_first_run_token()
    except FileNotFoundError:
        print('No recoverable first-run token exists.', file=sys.stderr)
        print('Use an existing API key, or create a new key from an already '
              'authenticated Settings session.', file=sys.stderr)
        return 1
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        print(f'Could not read first-run token: {exc}', file=sys.stderr)
        return 1

    login_url = base_url + '/?' + urllib.parse.urlencode({'token': token})
    print(f'Open once: {login_url}')
    print(f'Token source: {path}')
    print('This URL contains an admin credential. Do not share it or paste it '
          'into logs/support bundles.', file=sys.stderr)
    return 0


def _request(path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, method='POST' if body is not None else 'GET')
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8') or '{}')
        except (ValueError, TypeError):
            payload = {'ok': False, 'message': str(exc)}
        payload.setdefault('httpStatus', exc.code)
        return payload
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ManagerUnavailable(str(exc)) from exc


def _manager_health() -> dict | None:
    try:
        payload = _request('/health', timeout=1.0)
        if payload.get('ok') and PROJECT in payload.get('projects', []):
            return payload
    except ManagerUnavailable:
        pass
    return None


def _lifecycle_owns_frontend() -> bool:
    """Resolve the declared role without importing application assembly."""
    try:
        project_values = read_dotenv_values(Path(PROJECT) / '.env')
    except OSError:
        project_values = {}
    role = os.environ.get('TOFU_PROCESS_ROLE') \
        or project_values.get('TOFU_PROCESS_ROLE') or 'all'
    try:
        from lib.process_roles import CAPABILITY_FRONTEND, process_role_has
        return process_role_has(role, CAPABILITY_FRONTEND)
    except (ImportError, ValueError):
        # Invalid deployment configuration remains the server's authoritative
        # startup error and must not trigger a mutating build first.
        return False


def _validate_frontend_artifact() -> None:
    local_project = Path(__file__).resolve().parent
    selected_project = Path(PROJECT).resolve()
    if selected_project == local_project:
        from lib.vite_assets import validate_vite_artifact
        validate_vite_artifact()
        return
    # ``TOFU_PROJECT_PATH`` may ask this CLI to own another checkout. Importing
    # our own ``lib.vite_assets`` would validate the wrong static graph, so use
    # that checkout's small verifier in an isolated interpreter instead.
    verifier = selected_project / 'scripts' / 'verify_frontend_dist.py'
    if not verifier.is_file():
        raise RuntimeError(f'frontend validator is missing: {verifier}')
    completed = subprocess.run(
        [sys.executable, str(verifier)],
        cwd=selected_project,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30.0,
    )
    if completed.returncode:
        detail = (completed.stdout or '').strip()[-2000:]
        raise RuntimeError(detail or 'frontend artifact validation failed')


def _source_frontend_build_command() -> list[str] | None:
    """Return a source-build command only when local dev dependencies exist."""
    project = Path(PROJECT)
    build_script = project / 'scripts' / 'build_frontend.mjs'
    vite_package = project / 'node_modules' / 'vite' / 'package.json'
    if not build_script.is_file() or not vite_package.is_file():
        return None
    sibling_node = Path(sys.executable).with_name('node')
    if sibling_node.is_file() and os.access(sibling_node, os.X_OK):
        node = str(sibling_node)
    else:
        node = shutil.which('node') or ''
    if not node:
        return None
    return [node, str(build_script)]


def _repair_source_frontend_artifact(operation: str) -> str:
    """Rebuild one stale source-checkout graph before a lifecycle action.

    Release installs intentionally omit Node and continue to rely exclusively
    on their verified prebuilt graph.  Returning an empty string in that case
    preserves the production lifespan's actionable fail-closed validation.
    """
    if not _lifecycle_owns_frontend():
        return ''
    initial_error_message = ''
    try:
        _validate_frontend_artifact()
        return ''
    except Exception as initial_error:
        initial_error_message = str(initial_error)
        command = _source_frontend_build_command()
        if command is None:
            return ''
    lock_path = Path(PROJECT) / 'data' / '.frontend-build.lock'
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return f'frontend artifact rebuild lock could not be created: {exc}'
    with os.fdopen(lock_fd, 'a+', encoding='utf-8') as build_lock:
        fcntl.flock(build_lock.fileno(), fcntl.LOCK_EX)
        # @reboot and the minute-level recovery fallback may overlap. The
        # first process publishes atomically; every waiter reuses that graph.
        try:
            _validate_frontend_artifact()
            return ''
        except Exception:
            pass
        print(
            f'Frontend artifact is stale; rebuilding once before {operation}…',
            file=sys.stderr,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT,
                timeout=_FRONTEND_REPAIR_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return (
                'frontend artifact rebuild timed out after '
                f'{_FRONTEND_REPAIR_TIMEOUT_SECONDS:.0f}s; '
                'run `npm run build:frontend` manually')
        except OSError as exc:
            return f'frontend artifact rebuild could not start: {exc}'
        if completed.returncode:
            return (
                f'frontend artifact rebuild failed with exit '
                f'{completed.returncode}; '
                'run `npm run build:frontend` manually')
        try:
            _validate_frontend_artifact()
        except Exception as final_error:
            return (
                'frontend artifact remained invalid after rebuild: '
                f'{final_error}; initial error: {initial_error_message}')
    return ''


def ensure_manager(timeout: float = 8.0) -> dict:
    health = _manager_health()
    if health:
        return health
    repair_error = _repair_source_frontend_artifact('manager startup')
    if repair_error:
        raise ManagerUnavailable(repair_error)
    script = os.path.join(PROJECT, 'supervisor.sh')
    env = os.environ.copy()
    env['TOFU_SUPERVISOR_PROJECTS'] = PROJECT
    env['TOFU_SUPERVISOR_PYTHON'] = sys.executable
    try:
        result = subprocess.run(
            ['bash', script, 'daemon'], cwd=PROJECT, env=env,
            capture_output=True, text=True,
            timeout=max(_MANAGER_LAUNCH_TIMEOUT_FLOOR, timeout + 2.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagerUnavailable(f'cannot launch manager: {exc}') from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = _manager_health()
        if health:
            return health
        time.sleep(0.2)
    detail = ((result.stdout or '') + (result.stderr or '')).strip()
    log = os.path.join(PROJECT, 'logs', 'server-manager.log')
    raise ManagerUnavailable(
        f'manager did not become ready on {BASE_URL}; {detail or "no launcher output"}; '
        f'log: {log}')


def _remote_status(*, probe: bool = False) -> dict | None:
    query = urllib.parse.urlencode({'projectPath': PROJECT, 'probe': '1' if probe else '0'})
    try:
        status = _request('/status?' + query, timeout=2.0)
    except ManagerUnavailable:
        return None
    if probe and status.get('running') and 'ready' not in status:
        # During an in-place CLI upgrade an already-running manager may still
        # expose the pre-readiness schema. Probe the locked worker directly so
        # compatibility never restores the old liveness-only false positive.
        port = _valid_tcp_port(status.get('port'))
        if port is not None:
            direct = probe_application_readiness(
                port,
                status.get('pid'),
                preferred_scheme=str(status.get('scheme') or '').lower(),
                timeout=1.0,
            )
            status = {
                **status,
                'health': direct.get('health'),
                'liveness': direct.get('liveness'),
                'ready': direct.get('ready'),
                'probeScheme': direct.get('scheme'),
                'livenessError': direct.get('livenessError') or '',
                'readinessError': direct.get('readinessError') or '',
                'readinessState': direct.get('readinessState'),
                'storageState': direct.get('storageState'),
                'legacyManagerProbe': True,
            }
            if not direct.get('ready'):
                status['observed'] = 'degraded'
                status['lastError'] = (
                    direct.get('readinessError')
                    if direct.get('liveness') else
                    direct.get('livenessError')) or status.get('lastError') or ''
    return status


def _status_liveness(status: dict | None) -> bool:
    if not status:
        return False
    liveness = status.get('liveness')
    if liveness is None:
        liveness = status.get('health')
    return liveness is True


def _status_ready(status: dict | None) -> bool:
    """Return true only for manager-verified HTTP readiness.

    A listening TCP socket is useful startup progress, but it is not the
    public readiness contract: imports, storage, or an HTTP probe may still be
    failing behind that socket. Callers must obtain the status with
    ``probe=True`` before using this predicate.
    """
    return bool(
        status
        and status.get('running')
        and status.get('observed') == 'running'
        and _status_liveness(status)
        and status.get('ready') is True)


def _startup_age_seconds(status: dict | None, *, now: float | None = None) -> float:
    try:
        started_at = float((status or {}).get('processStartedAt') or 0)
    except (TypeError, ValueError):
        return 0.0
    if started_at <= 0:
        return 0.0
    return max(0.0, (time.time() if now is None else float(now)) - started_at)


def _startup_stuck(status: dict | None, *, now: float | None = None) -> bool:
    return bool(
        status
        and status.get('observed') in ('starting', 'degraded')
        and status.get('ready') is not True
        and _startup_age_seconds(status, now=now) >= STARTUP_STUCK_SECONDS)


def _declared_worker_port(lock_status: dict | None) -> int | None:
    """Read an explicit ``--port`` from the identity-checked worker command."""
    command = str((lock_status or {}).get('cmdline') or '').strip()
    if not command:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError:
        arguments = command.split()
    values, error = _forwarded_option_values(arguments, '--port')
    return None if error or not values else _valid_tcp_port(values[0])


def _probe_local_worker(port: int, expected_pid: int | None, *,
                        preferred_scheme: str = '') -> dict:
    """Probe loopback identity and readiness through the manager contract."""
    return probe_application_readiness(
        port,
        expected_pid,
        preferred_scheme=preferred_scheme,
        timeout=1.0,
    )


def _worker_port_drift(status: dict | None, lock_status: dict | None) -> dict | None:
    """Return evidence when manager intent and the live worker endpoint differ."""
    if not lock_status or not lock_status.get('running'):
        return None
    declared_port = _declared_worker_port(lock_status)
    manager_port = _valid_tcp_port((status or {}).get('port'))
    if declared_port is None or manager_port is None or declared_port == manager_port:
        return None
    preferred_scheme = str((status or {}).get('scheme') or '').lower()
    probe = _probe_local_worker(
        declared_port, lock_status.get('pid'), preferred_scheme=preferred_scheme)
    return {
        'managerPort': manager_port,
        'workerDeclaredPort': declared_port,
        'workerPid': lock_status.get('pid'),
        'listenerPids': listener_pids(declared_port),
        **probe,
    }


def _service_url(status: dict) -> str:
    """Return a copyable local URL without lying about explicit TLS mode."""
    host = str(status.get('bindHost') or os.environ.get('BIND_HOST')
               or _project_setting(PROJECT, 'BIND_HOST', 'localhost'))
    if host in ('0.0.0.0', '::', '[::]'):
        host = 'localhost'
    elif ':' in host and not host.startswith('['):
        host = f'[{host}]'
    scheme = str(status.get('scheme') or '').lower()
    if scheme not in ('http', 'https'):
        try:
            mode = (Path(PROJECT) / 'data' / '.last_serve_mode').read_text(
                encoding='utf-8').strip().lower()
        except OSError:
            mode = ''
        scheme = 'https' if mode == 'https' else 'http'
    return f'{scheme}://{host}:{_tcp_port(status.get("port"))}'


def _print_start_failure_help() -> None:
    print(f'Diagnose: {_control_command("doctor")}', file=sys.stderr)
    print(f'Logs    : {_control_command("logs", "-n", 200)}', file=sys.stderr)


def _post(action: str, **extra) -> dict:
    ensure_manager()
    return _request('/' + action, {
        'projectPath': PROJECT,
        'source': extra.pop('source', 'serverctl'),
        **extra,
    }, timeout=35.0 if action in ('stop', 'restart') else 5.0)


def _forwarded_server_env() -> dict[str, str]:
    keys = ('PORT', 'BIND_HOST', 'TOFU_TLS', 'TLS_CERTFILE', 'TLS_KEYFILE',
            'TOFU_PROCESS_RSS_RECYCLE_MB')
    try:
        project_values = read_dotenv_values(Path(PROJECT) / '.env')
    except OSError as exc:
        raise ManagerUnavailable(f'cannot read project .env: {exc}') from exc
    result: dict[str, str] = {}
    for key in keys:
        # This mirrors server startup's fill-if-absent rule. Empty values are
        # omitted because the lifecycle manager intentionally treats them as
        # "not configured" rather than persisting an unusable launch setting.
        value = os.environ.get(key) if key in os.environ else project_values.get(key)
        if value:
            result[key] = value
    return result


def _forwarded_option_values(args: list[str], name: str) -> tuple[list[str], str]:
    """Extract one lifecycle-critical server option without importing the app."""
    values: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == name:
            if index + 1 >= len(args) or args[index + 1].startswith('--'):
                return [], f'{name} requires a value'
            values.append(args[index + 1])
            index += 2
            continue
        if item.startswith(name + '='):
            value = item.partition('=')[2]
            if not value:
                return [], f'{name} requires a value'
            values.append(value)
        index += 1
    if len(values) > 1:
        return [], f'{name} may be supplied only once'
    return values, ''


def _forwarded_server_options_error(
        args: list[str], server_env: dict[str, str]) -> str:
    """Reject options that would make manager and worker observe different state."""
    value_options = ('--host', '--port', '--certfile', '--keyfile', '--workers')
    flag_options = {'--no-tls'}
    seen_flags: set[str] = set()
    index = 0
    while index < len(args):
        item = args[index]
        if item in flag_options:
            if item in seen_flags:
                return f'{item} may be supplied only once'
            seen_flags.add(item)
            index += 1
            continue
        if item in value_options:
            if index + 1 >= len(args) or args[index + 1].startswith('--'):
                return f'{item} requires a value'
            index += 2
            continue
        option_name, equals, option_value = item.partition('=')
        if equals and option_name in value_options:
            if not option_value:
                return f'{option_name} requires a value'
            index += 1
            continue
        if item.startswith('-'):
            return 'unsupported server option; run `python server.py --help`'
        return 'positional server arguments are not supported'

    ports, error = _forwarded_option_values(args, '--port')
    if error:
        return error
    raw_port = ports[0] if ports else server_env.get('PORT')
    if raw_port is not None:
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            source = '--port' if ports else 'PORT'
            return f'{source} must be an integer from 1 to 65535 (got {raw_port!r})'

    workers, error = _forwarded_option_values(args, '--workers')
    if error:
        return error
    if workers:
        try:
            worker_count = int(workers[0])
        except (TypeError, ValueError):
            worker_count = 0
        if worker_count != 1:
            return '--workers must be 1; scale with separate replicas'

    option_values: dict[str, list[str]] = {}
    for option in ('--host', '--certfile', '--keyfile'):
        values, error = _forwarded_option_values(args, option)
        if error:
            return error
        option_values[option] = values
    cert_configured = bool(
        option_values['--certfile'] or server_env.get('TLS_CERTFILE'))
    key_configured = bool(
        option_values['--keyfile'] or server_env.get('TLS_KEYFILE'))
    if cert_configured != key_configured:
        return '--certfile and --keyfile must be configured together'
    if cert_configured and key_configured:
        configured_files = {
            '--certfile/TLS_CERTFILE': (
                option_values['--certfile'][0]
                if option_values['--certfile'] else server_env['TLS_CERTFILE']),
            '--keyfile/TLS_KEYFILE': (
                option_values['--keyfile'][0]
                if option_values['--keyfile'] else server_env['TLS_KEYFILE']),
        }
        for label, configured_path in configured_files.items():
            path = Path(configured_path)
            if not path.is_absolute():
                path = Path(PROJECT) / path
            if not path.is_file():
                return f'{label} file does not exist: {path}'
    raw_tls = str(server_env.get('TOFU_TLS') or '').strip()
    if raw_tls and parse_env_boolean(raw_tls) is None \
            and '--no-tls' not in seen_flags \
            and not (cert_configured and key_configured):
        return (
            f'unsupported TOFU_TLS={raw_tls!r}; expected 0/1, false/true, '
            'no/yes, off/on, or disabled/enabled')
    return ''


def _requested_server_port(
        args: list[str], server_env: dict[str, str]) -> int | None:
    """Resolve the requested port after validation, using server precedence."""
    ports, error = _forwarded_option_values(args, '--port')
    if error:
        return None
    raw_port = ports[0] if ports else server_env.get('PORT')
    try:
        port = int(raw_port) if raw_port is not None else None
    except (TypeError, ValueError):
        return None
    return port if port is not None and 1 <= port <= 65535 else None


_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_BOOT_PROGRESS_RE = re.compile(r'\[boot \+\s*\d+(?:\.\d+)?s\]\s*(.+)')
_STARTUP_PHASE_RE = re.compile(
    r'\[startup phase (?P<index>\d+)/(?P<total>\d+)\]\s+'
    r'(?P<state>start|done|failed)\s+\|\s*(?P<label>[^|]+?)'
    r'(?:\s*\|\s*(?P<duration>\d+(?:\.\d+)?)s)?\s*$')


class _StartupProgress:
    """Small foreground progress view backed by the worker's boot log.

    The manager deliberately detaches the worker, so its existing ``[boot]``
    messages otherwise disappear into server-console.log while a human-facing
    ``python server.py`` waits in silence. TTYs get one determinate line;
    pipes get sparse, durable stage lines suitable for CI logs.
    """

    def __init__(self, log_path: str | os.PathLike[str], *, stream=None) -> None:
        self.log_path = Path(log_path)
        self.stream = sys.stderr if stream is None else stream
        setting = (os.environ.get('TOFU_STARTUP_PROGRESS') or '').strip().lower()
        self.enabled = setting not in {'0', 'false', 'no', 'off'}
        self.interactive = bool(
            self.enabled
            and getattr(self.stream, 'isatty', lambda: False)()
        )
        try:
            self.offset = self.log_path.stat().st_size
        except OSError:
            self.offset = 0
        self.started = time.monotonic()
        self.last_stage = 'Contacting lifecycle manager…'
        self.last_emitted_stage = ''
        self.last_line_at = 0.0
        self.failure_hint = ''
        self._partial = ''
        self.phase_total = 0
        self.phases: dict[int, dict[str, object]] = {}

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def start(self) -> None:
        if not self.enabled:
            return
        self._emit(force=True)

    def _consume_worker_log(self) -> None:
        try:
            size = self.log_path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self._partial = ''
            with self.log_path.open('rb') as stream:
                stream.seek(self.offset)
                # A boot log storm must not make the foreground client ingest
                # unbounded output.  The latest 256 KiB contains every useful
                # stage and failure line while the full log remains on disk.
                available = max(0, size - self.offset)
                if available > 256 * 1024:
                    stream.seek(size - 256 * 1024)
                    self._partial = ''
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError:
            return
        if not chunk:
            return
        text = self._partial + chunk.decode('utf-8', 'replace')
        lines = text.split('\n')
        self._partial = lines.pop()
        for raw in lines:
            line = _ANSI_ESCAPE_RE.sub('', raw).strip()
            phase = _STARTUP_PHASE_RE.search(line)
            if phase:
                self._record_phase(phase.groupdict())
            boot = _BOOT_PROGRESS_RE.search(line)
            if boot:
                stage = ' '.join(boot.group(1).split())
                # Structured phase markers are the source of truth. Keep the
                # old free-form boot messages as a fallback for workers from
                # an older process image or a failed pre-lifespan launch.
                if stage and not phase:
                    self.last_stage = stage[:180]
            if ('storage sidecar startup refused' in line.lower()
                    or line.startswith('RuntimeError: storage sidecar')
                    or line.startswith('StorageError:')):
                self.failure_hint = line[-500:]

    def _record_phase(self, fields: dict[str, str | None]) -> None:
        try:
            index = int(fields['index'] or 0)
            total = int(fields['total'] or 0)
        except (TypeError, ValueError):
            return
        if index <= 0 or total <= 0 or index > total:
            return
        self.phase_total = max(self.phase_total, total)
        label = ' '.join((fields.get('label') or '').split())
        if not label:
            return
        phase = self.phases.setdefault(index, {
            'label': label,
            'state': 'pending',
            'duration': None,
            'started_at': None,
        })
        phase['label'] = label
        state = fields.get('state')
        if state == 'start':
            phase['state'] = 'running'
            phase['started_at'] = time.monotonic()
            phase['duration'] = None
            self.last_stage = label
        else:
            phase['state'] = 'done' if state == 'done' else 'failed'
            raw_duration = fields.get('duration')
            try:
                duration = float(raw_duration) if raw_duration is not None else None
            except (TypeError, ValueError):
                duration = None
            if duration is None and phase.get('started_at') is not None:
                duration = max(0.0, time.monotonic() - float(phase['started_at']))
            phase['duration'] = duration
            if state == 'failed':
                self.failure_hint = f'{label} failed'
            self.last_stage = label

    def _phase_counts(self) -> tuple[int, int, dict[str, object] | None]:
        total = self.phase_total
        if total <= 0:
            return 0, 0, None
        completed = sum(
            1 for index in range(1, total + 1)
            if self.phases.get(index, {}).get('state') == 'done')
        current = next(
            (self.phases[index] for index in range(1, total + 1)
             if self.phases.get(index, {}).get('state') == 'running'),
            None)
        return completed, total, current

    def _phase_elapsed(self, phase: dict[str, object] | None) -> float | None:
        if not phase:
            return None
        duration = phase.get('duration')
        if isinstance(duration, (int, float)):
            return float(duration)
        started_at = phase.get('started_at')
        if isinstance(started_at, (int, float)):
            return max(0.0, time.monotonic() - float(started_at))
        return None

    def _render_phase_summary(self) -> list[str]:
        if self.phase_total <= 0:
            return []
        rows = []
        for index in range(1, self.phase_total + 1):
            phase = self.phases.get(index)
            if not phase:
                rows.append(f'  ○ {index:>2}/{self.phase_total:<2} pending')
                continue
            state = phase.get('state')
            icon = {'done': '✓', 'failed': '✗', 'running': '▶'}.get(state, '○')
            elapsed = self._phase_elapsed(phase)
            duration = f'{elapsed:6.1f}s' if elapsed is not None else '      —'
            rows.append(
                f'  {icon} {index:>2}/{self.phase_total:<2} '
                f'{str(phase.get("label") or ""):28.28} {duration}')
        return rows

    def tick(self, status: dict | None = None) -> None:
        if not self.enabled:
            return
        self._consume_worker_log()
        if (status and status.get('running')
                and self.last_stage == 'Contacting lifecycle manager…'):
            pid = status.get('pid')
            if isinstance(pid, int):
                self.last_stage = f'Worker PID {pid} launched; waiting for health check…'
        self._emit()

    def _emit(self, *, force: bool = False) -> None:
        now = time.monotonic()
        elapsed = self.elapsed
        if self.interactive:
            completed, total, current = self._phase_counts()
            if total:
                width = 16
                filled = round(width * completed / total)
                bar = '█' * filled + '░' * (width - filled)
                current_label = str(current.get('label')) if current else self.last_stage
                current_elapsed = self._phase_elapsed(current)
                phase_time = (f'{current_elapsed:.1f}s'
                              if current_elapsed is not None else '—')
                detail = (f'{completed}/{total} {completed * 100 // total:3d}% | '
                          f'{current_label} [{phase_time}]')
            else:
                # Legacy worker fallback: no fake movement. A static bar is
                # more truthful than an indeterminate dot when phase metadata
                # is unavailable.
                bar = '░' * 16
                detail = f'waiting for startup phase | {self.last_stage}'
            self.stream.write(
                f'\r\033[2K⏳ Tofu startup [{bar}] {elapsed:5.1f}s | {detail}')
            self.stream.flush()
            return
        completed, total, current = self._phase_counts()
        stage_key: object
        if total and current:
            current_elapsed = self._phase_elapsed(current)
            current_time = (f'{current_elapsed:.1f}s'
                            if current_elapsed is not None else '—')
            display_stage = (
                f'{completed}/{total} | ▶ {current.get("label")} '
                f'({current_time})')
            stage_key = ('phase', completed, total, current.get('label'))
        else:
            display_stage = self.last_stage
            stage_key = ('text', display_stage)
        stage_changed = stage_key != self.last_emitted_stage
        if not force and not stage_changed and now - self.last_line_at < 10.0:
            return
        self.stream.write(f'[startup +{elapsed:5.1f}s] {display_stage}\n')
        self.stream.flush()
        self.last_emitted_stage = stage_key
        self.last_line_at = now

    def finish(self, *, ready: bool) -> None:
        if not self.enabled:
            return
        self._consume_worker_log()
        if self.interactive:
            self.stream.write('\r\033[2K')
        outcome = 'ready' if ready else 'failed'
        self.stream.write(f'Tofu startup {outcome} after {self.elapsed:.1f}s.\n')
        summary = self._render_phase_summary()
        if summary:
            self.stream.write('Startup stages:\n' + '\n'.join(summary) + '\n')
        if not ready and self.failure_hint:
            self.stream.write(f'Startup error: {self.failure_hint}\n')
        self.stream.flush()

    def finish_waiting(self) -> None:
        """Close the foreground view for an accepted deferred start."""
        if not self.enabled:
            return
        self._consume_worker_log()
        if self.interactive:
            self.stream.write('\r\033[2K')
        self.stream.write(
            f'Tofu startup queued after {self.elapsed:.1f}s; storage '
            'maintenance is still running.\n')
        self.stream.flush()


def _wait_ready(timeout: float, *, progress: _StartupProgress | None = None) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        # The manager's live HTTP probe is the authority for readiness.  A
        # bare open port can precede a usable application by several startup
        # phases and previously caused false-positive "started" messages.
        last = _remote_status(probe=True) or {}
        if progress is not None:
            progress.tick(last)
        if _status_ready(last):
            return last
        if last.get('observed') in ('conflict', 'crashloop', 'maintenance') \
                or _startup_stuck(last):
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last
        time.sleep(min(0.5, remaining))


def _print_status(status: dict, *, manager_online: bool = True) -> None:
    print(f"Manager : {'running' if manager_online else 'not running'}"
          + (f" (PID {status.get('managerPid')})" if status.get('managerPid') else ''))
    print(f"Desired : {status.get('desired', 'unknown')}")
    print(f"Server  : {status.get('observed', 'unknown')}")
    liveness = status.get('liveness')
    if liveness is None:
        liveness = status.get('health')
    if liveness is not None:
        print(f"Liveness: {'passing' if liveness else 'failing'}")
    if status.get('ready') is not None:
        print(f"Ready   : {'yes' if status.get('ready') else 'no'}")
    if status.get('pid'):
        print(f"PID     : {status['pid']}")
    if status.get('processStartedAt'):
        print('Started : ' + time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(float(status['processStartedAt']))))
    print(f"Port    : {status.get('port', os.environ.get('PORT', '15000'))}")
    if status.get('launchSource'):
        print(f"Source  : {status['launchSource']}")
    if status.get('restartCount') is not None:
        recent = status.get('recentFailureCount')
        window = status.get('failureWindowSeconds')
        detail = (f'; recent={recent}/{status.get("maxFailures")} in '
                  f'{float(window):.0f}s'
                  if recent is not None and window is not None else '')
        print(f"Restarts: total={status.get('restartCount', 0)}{detail}")
    if status.get('lastRecoverySeconds') is not None:
        print(f"Last RTO: {float(status['lastRecoverySeconds']):.3f}s")
    if status.get('workerRssGuardEnabled'):
        rss = status.get('workerRssBytes')
        limit = status.get('workerRssRecycleBytes')
        print('RSS guard: '
              f"{float(rss or 0) / (1024 ** 3):.2f} GiB / "
              f"{float(limit or 0) / (1024 ** 3):.2f} GiB hard ceiling")
    if status.get('lastExitCause'):
        print(f"Last exit: {status['lastExitCause']}"
              + (f" ({status.get('lastFailureReason')})"
                 if status.get('lastFailureReason') else ''))
    storage_lease = status.get('storageLease') or {}
    if storage_lease.get('held'):
        holder = str(storage_lease.get('label') or 'Storage operation')
        if isinstance(storage_lease.get('pid'), int):
            holder += f" (PID {storage_lease['pid']})"
        age = storage_lease.get('ageSeconds')
        if isinstance(age, (int, float)):
            holder += f'; active {float(age) / 60:.1f}m'
        print(f'Storage : {holder}')
    if status.get('lastError'):
        print(f"Problem : {status['lastError']}")
    if status.get('workerLog'):
        print(f"Log     : {status['workerLog']}")


def cmd_start(args: argparse.Namespace) -> int:
    return managed_start(args.server_args, wait=args.wait, source=args.source)


def managed_start(server_args: list[str] | None = None, *, wait: float = 180.0,
                  source: str = 'python-server.py') -> int:
    """Start through the manager; used by both the CLI and ``server.py``."""
    forwarded = list(server_args or [])
    if forwarded[:1] == ['--']:
        forwarded = forwarded[1:]
    server_env = _forwarded_server_env()
    option_error = _forwarded_server_options_error(forwarded, server_env)
    if option_error:
        print(f'Cannot start Tofu: {option_error}', file=sys.stderr)
        print(f'Usage: {sys.executable} server.py --help', file=sys.stderr)
        return 2
    repair_error = _repair_source_frontend_artifact('Tofu startup')
    if repair_error:
        print(f'Cannot start Tofu: {repair_error}', file=sys.stderr)
        return 1
    progress = _StartupProgress(Path(PROJECT) / 'logs' / 'server-console.log')
    progress.start()
    try:
        result = _post('start', source=source, serverArgs=forwarded,
                       serverEnv=server_env)
    except BaseException:
        progress.finish(ready=False)
        raise
    if not result.get('ok'):
        progress.finish(ready=False)
        print(f"Tofu failed to start: {result.get('message') or result.get('error')}",
              file=sys.stderr)
        _print_status(result)
        _print_start_failure_help()
        return 1
    if (result.get('waitingForMaintenance')
            or result.get('observed') == 'maintenance'):
        progress.finish_waiting()
        print('Tofu start is queued behind offline storage maintenance.')
        print('The manager will start it automatically when the storage lease is released.')
        _print_status(result)
        return 0
    status = _wait_ready(wait, progress=progress)
    if status.get('observed') == 'maintenance':
        progress.finish_waiting()
        print('Tofu start is queued behind offline storage maintenance.')
        print('The manager will start it automatically when the storage lease is released.')
        _print_status(status)
        return 0
    if _status_ready(status):
        requested_port = _requested_server_port(forwarded, server_env)
        actual_port = _valid_tcp_port(status.get('port'))
        if result.get('alreadyRunning') and requested_port is not None \
                and actual_port != requested_port:
            progress.finish(ready=True)
            print(
                f'Tofu is already ready at {_service_url(status)}, but '
                f'requested port {requested_port} was not applied because '
                'start is idempotent.',
                file=sys.stderr,
            )
            restart_parts: list[object] = ['restart']
            if forwarded:
                restart_parts.extend(['--', *forwarded])
            print('Apply the configuration with a human-approved restart:',
                  file=sys.stderr)
            print(f'  {_control_command(*restart_parts)}', file=sys.stderr)
            return 1
        progress.finish(ready=True)
        label = 'already running' if result.get('alreadyRunning') else 'started'
        print(f"Tofu {label} (PID {status.get('pid')}, port {status.get('port')}).")
        print(f'Open  : {_service_url(status)}')
        print(f"Status: {_control_command('status')}")
        print(f"Logs  : {_control_command('logs', '-f')}")
        return 0
    progress.finish(ready=False)
    port_drift = _worker_port_drift(status, read_lock_status(PROJECT))
    if port_drift and port_drift.get('ready'):
        print(
            f"Tofu is ready at {port_drift['url']}, but the manager is "
            f"probing port {port_drift['managerPort']} instead of the worker's "
            f"declared port {port_drift['workerDeclaredPort']}.",
            file=sys.stderr)
    elif status.get('observed') in ('conflict', 'crashloop'):
        print('Tofu startup stopped before readiness.', file=sys.stderr)
    elif _startup_stuck(status):
        print(
            f'Tofu startup is stuck after '
            f'{_startup_age_seconds(status) / 60:.1f} minutes.',
            file=sys.stderr)
    else:
        print('Tofu was launched but did not become ready before the timeout.', file=sys.stderr)
    _print_status(status or result)
    _print_start_failure_help()
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    current = _remote_status()
    live = bool(current and current.get('running')) \
        or bool(read_lock_status(PROJECT).get('running'))
    if live and not _approve_stop_if_needed(args):
        return 3
    result = _post('stop', source=args.source)
    print(result.get('message') or ('stopped' if result.get('ok') else 'stop failed'))
    return 0 if result.get('ok') else 1


def _approve_lifecycle_if_needed(
        args: argparse.Namespace, *, action: str, prompt: str) -> bool:
    """Require a human decision before interrupting one live worker."""
    # Interactive terminal invocation is itself the human action. Automated
    # callers must present/consume the existing one-time approval mechanism.
    gate_passed = (
        os.environ.get('TOFU_LIFECYCLE_GATE_PASSED') == action
        or (action == 'restart'
            and os.environ.get('TOFU_RESTART_GATE_PASSED') == '1'))
    if sys.stdin.isatty() or gate_passed:
        if args.yes or gate_passed:
            return True
        try:
            return input(prompt).strip().lower() in ('y', 'yes')
        except EOFError:
            return False
    try:
        from lib.lifecycle_approval import consume_any, create_request, list_records
        ok, why, _ = consume_any(action)
    except Exception as exc:
        print(f'{action.capitalize()} approval check failed: {exc}', file=sys.stderr)
        return False
    if not ok:
        print(f'{action.capitalize()} requires human approval ({why}).',
              file=sys.stderr)
        try:
            pending = list_records(status='pending', action=action, limit=1)
            request = pending[0] if pending else create_request(
                action,
                origin={
                    'source': 'serverctl',
                    'pid': os.getpid(),
                    'project_path': PROJECT,
                },
            )
            request_id = str(request.get('id') or '')
        except Exception as exc:
            print(f'Could not register pending approval: {exc}', file=sys.stderr)
            request_id = ''
        suffix = f' (request {request_id[:8]})' if request_id else ''
        print(f'Approve the pending {action} action in the Tofu UI{suffix}, '
              'then re-run this command; or run it from an interactive terminal.',
              file=sys.stderr)
    return ok


def _approve_restart_if_needed(args: argparse.Namespace) -> bool:
    return _approve_lifecycle_if_needed(
        args, action='restart',
        prompt='Restart the running Tofu server? [y/N] ')


def _approve_stop_if_needed(args: argparse.Namespace) -> bool:
    return _approve_lifecycle_if_needed(
        args, action='shutdown',
        prompt='Stop the running Tofu server? [y/N] ')


def cmd_restart(args: argparse.Namespace) -> int:
    forwarded = list(args.server_args or [])
    if forwarded[:1] == ['--']:
        forwarded = forwarded[1:]
    server_env = _forwarded_server_env()
    option_error = _forwarded_server_options_error(forwarded, server_env)
    if option_error:
        print(f'Cannot restart Tofu: {option_error}', file=sys.stderr)
        print(f'Usage: {sys.executable} server.py --help', file=sys.stderr)
        return 2
    current = _remote_status()
    live = bool(current and current.get('running')) \
        or bool(read_lock_status(PROJECT).get('running'))
    if live and not _approve_restart_if_needed(args):
        return 3
    repair_error = _repair_source_frontend_artifact('Tofu restart')
    if repair_error:
        print(f'Cannot restart Tofu: {repair_error}', file=sys.stderr)
        return 1
    result = _post('restart', source=args.source, serverArgs=forwarded or None,
                   serverEnv=server_env)
    if not result.get('ok'):
        print(f"Restart failed: {result.get('message')}", file=sys.stderr)
        _print_start_failure_help()
        return 1
    if (result.get('waitingForMaintenance')
            or result.get('observed') == 'maintenance'):
        print('Tofu restart is queued behind offline storage maintenance.')
        print('The manager will start it automatically when the storage lease is released.')
        _print_status(result)
        return 0
    status = _wait_ready(args.wait)
    if status.get('observed') == 'maintenance':
        print('Tofu restart is queued behind offline storage maintenance.')
        print('The manager will start it automatically when the storage lease is released.')
        _print_status(status)
        return 0
    if _status_ready(status):
        print(f"Tofu restarted (PID {status.get('pid')}, port {status.get('port')}).")
        print(f'Open: {_service_url(status)}')
        return 0
    port_drift = _worker_port_drift(status, read_lock_status(PROJECT))
    if port_drift and port_drift.get('ready'):
        print(
            f"Restarted worker is ready at {port_drift['url']}, but manager "
            f"readiness still probes port {port_drift['managerPort']}.",
            file=sys.stderr)
    else:
        print('Restart launched, but the server is not ready.', file=sys.stderr)
    _print_status(status or result)
    _print_start_failure_help()
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    status = _remote_status(probe=True)
    online = status is not None
    low = read_lock_status(PROJECT)
    if status is None:
        port_config = _configured_port_snapshot(PROJECT)
        configured_port = port_config['port']
        pids = listener_pids(configured_port)
        status = {
            **low,
            'desired': 'unknown',
            'observed': ('running-unmanaged' if low.get('running') else
                         ('conflict' if pids else 'stopped')),
            'port': configured_port,
            'portConfigSource': port_config['source'],
            'portConfigValid': port_config['valid'],
            'portConfigError': port_config.get('error'),
            'foreignListenerPids': [pid for pid in pids if pid != low.get('pid')],
            'lastError': 'manager is not running',
        }
    port_drift = _worker_port_drift(status, low)
    startup_age = (
        _startup_age_seconds(status)
        if status.get('observed') in ('starting', 'degraded') else 0.0)
    startup_stuck = online and _startup_stuck(status)
    ready = online and _status_ready(status)
    application_reachable = bool(
        online and _status_liveness(status)
        or (port_drift and port_drift.get('liveness')))
    application_url = (
        _service_url(status) if ready else
        ((port_drift or {}).get('url')
         if (port_drift or {}).get('ready') else None))
    if args.json:
        print(json.dumps({**status, 'commandOk': True, 'managerOnline': online,
                          'ready': ready, 'startupStuck': startup_stuck,
                          'startupAgeSeconds': round(startup_age, 1),
                          'portDrift': port_drift,
                          'applicationReachable': application_reachable,
                          'applicationUrl': application_url},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_status(status, manager_online=online)
        if port_drift:
            health = (
                'ready' if port_drift.get('ready') else
                ('live but not ready' if port_drift.get('liveness')
                 else 'not live'))
            print(f"Drift   : manager probes {port_drift['managerPort']}; "
                  f"worker PID {port_drift['workerPid']} declares "
                  f"{port_drift['workerDeclaredPort']} ({health})")
            if port_drift.get('url'):
                print(f"Open    : {port_drift['url']}")
            print(f'Next    : {_control_command("doctor")}')
        elif ready:
            print(f'Open    : {application_url}')
        elif startup_stuck:
            print(f'Stuck   : startup has not become ready for '
                  f'{startup_age / 60:.1f} minutes')
            print(f'Next    : {_control_command("doctor")}')
    unhealthy = status.get('observed') in ('conflict', 'crashloop', 'degraded')
    return 0 if online and not unhealthy and not startup_stuck \
        and port_drift is None else 1


def cmd_logs(args: argparse.Namespace) -> int:
    status = _remote_status() or {}
    path = status.get('managerLog' if args.manager else 'workerLog')
    if not path:
        path = os.path.join(PROJECT, 'logs',
                            'server-manager.log' if args.manager else 'server-console.log')
    role = 'manager' if args.manager else 'worker'
    if not Path(path).is_file():
        print(f'No {role} log exists yet at {path}.', file=sys.stderr)
        print(f'Status  : {_control_command("status")}', file=sys.stderr)
        print(f'Diagnose: {_control_command("doctor")}', file=sys.stderr)
        return 1
    if args.follow:
        try:
            result = subprocess.call(['tail', '-n', str(args.lines), '-f', path])
        except KeyboardInterrupt:
            return 130
        except OSError as exc:
            print(f'Cannot follow {path}: {exc}', file=sys.stderr)
            return 1
    else:
        try:
            result = subprocess.call(['tail', '-n', str(args.lines), path])
        except OSError as exc:
            print(f'Cannot read {path}: {exc}', file=sys.stderr)
            return 1
    if result:
        print(f'Diagnose: {_control_command("doctor")}', file=sys.stderr)
    return result


def _proc_lines(pattern: str) -> list[str]:
    try:
        out = subprocess.run(['ps', '-eo', 'pid,ppid,sid,lstart,stat,args'],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if pattern in line and
            'serverctl.py doctor' not in line]


def _read_first_text(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        try:
            return Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            continue
    return None


def _parse_counter(text: str | None, name: str) -> int | None:
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == name:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def _finite_bytes(text: str | None) -> int | None:
    try:
        value = int(text or '')
    except (TypeError, ValueError):
        return None
    # cgroup v1 represents "unlimited" with a huge page-aligned integer.
    return value if 0 <= value < (1 << 60) else None


def cgroup_memory_snapshot(worker_pid: int | None = None) -> dict:
    """Best-effort cgroup/OOM evidence for the read-only doctor command."""
    usage = _finite_bytes(_read_first_text((_CGROUP_V2_USAGE, _CGROUP_V1_USAGE)))
    limit = _finite_bytes(_read_first_text((_CGROUP_V2_LIMIT, _CGROUP_V1_LIMIT)))
    v2_swap = _finite_bytes(_read_first_text((_CGROUP_V2_SWAP_LIMIT,)))
    memsw_limit = _finite_bytes(_read_first_text((_CGROUP_V1_MEMSW_LIMIT,)))
    if v2_swap is not None:
        swap_limit = v2_swap
    elif memsw_limit is not None and limit is not None:
        swap_limit = max(0, memsw_limit - limit)
    else:
        swap_limit = None
    events = _read_first_text((_CGROUP_V2_EVENTS, _CGROUP_V1_OOM))
    oom_kills = _parse_counter(events, 'oom_kill')
    failcnt = _finite_bytes(_read_first_text((_CGROUP_V1_FAILCNT,)))
    worker_rss = None
    if isinstance(worker_pid, int) and worker_pid > 0:
        try:
            fields = Path(f'/proc/{worker_pid}/statm').read_text().split()
            worker_rss = int(fields[1]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            pass
    return {
        'usageBytes': usage,
        'limitBytes': limit,
        'usagePct': (round(usage * 100.0 / limit, 1)
                     if usage is not None and limit else None),
        'swapLimitBytes': swap_limit,
        'oomKills': oom_kills,
        'failCount': failcnt,
        'workerRssBytes': worker_rss,
    }


def _project_setting(project_path: str, name: str, default: str = '') -> str:
    names = [name]
    if name.startswith('TOFU_'):
        names.append('CHATUI_' + name[len('TOFU_'):])
    for candidate in names:
        if candidate in os.environ:
            return os.environ[candidate].strip()
    try:
        values = read_dotenv_values(Path(project_path) / '.env')
    except OSError:
        return default
    for candidate in names:
        if values.get(candidate):
            return values[candidate]
    return default


def _configured_port_snapshot(project_path: str) -> dict:
    """Resolve PORT for offline diagnostics and retain invalid-config evidence."""
    if 'PORT' in os.environ:
        raw = os.environ.get('PORT', '')
        source = 'environment'
    else:
        try:
            values = read_dotenv_values(Path(project_path) / '.env')
        except OSError as exc:
            return {
                'source': 'project .env',
                'raw': '',
                'valid': False,
                'port': 15000,
                'error': str(exc)[:300],
            }
        raw = values.get('PORT', '15000')
        source = 'project .env' if 'PORT' in values else 'default'
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = 0
    valid = 1 <= parsed <= 65535
    return {
        'source': source,
        'raw': str(raw)[:80],
        'valid': valid,
        'port': parsed if valid else 15000,
    }


def _verified_sidecar_snapshots(snapshot_dir: Path) -> tuple[
        list[tuple[float, int, Path]], bool]:
    """Return bounded manifest-published Sidecar backups, newest chosen later."""
    candidates: list[tuple[float, int, Path]] = []
    scanned = 0
    truncated = False
    try:
        entries = os.scandir(snapshot_dir)
    except OSError:
        return candidates, truncated
    with entries:
        for entry in entries:
            if not (entry.name.startswith('storage-sqlite-')
                    and entry.name.endswith('.sqlite3')):
                continue
            scanned += 1
            if scanned > 256:
                truncated = True
                break
            path = snapshot_dir / entry.name
            manifest = path.with_name(path.name + '.manifest.json')
            try:
                stat = path.stat()
                payload = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            try:
                manifest_bytes = int(payload.get('bytes') or -1) \
                    if isinstance(payload, dict) else -1
            except (TypeError, ValueError):
                continue
            if (path.is_symlink() or not path.is_file()
                    or not isinstance(payload, dict)
                    or payload.get('backend') != 'sqlite'
                    or payload.get('artifact') != path.name
                    or payload.get('integrity') != 'ok'
                    or manifest_bytes != stat.st_size
                    or len(str(payload.get('sha256') or '')) != 64):
                continue
            candidates.append((stat.st_mtime, stat.st_size, path))
    return candidates, truncated


def _legacy_snapshot_inventory(data_dir: Path) -> dict[str, int | bool]:
    """Measure retired backup-owner artifacts without deleting or opening them."""
    legacy_dir = data_dir / 'db_snapshots'
    published_count = 0
    temporary_count = 0
    published_bytes = 0
    temporary_bytes = 0
    matched = 0
    truncated = False
    try:
        entries = os.scandir(legacy_dir)
    except OSError:
        return {
            'publishedCount': 0,
            'publishedBytes': 0,
            'temporaryCount': 0,
            'temporaryBytes': 0,
            'totalBytes': 0,
            'scanTruncated': False,
        }
    with entries:
        for entry in entries:
            is_published = (
                entry.name.startswith('tofu-')
                and entry.name.endswith('.sqlite3'))
            is_temporary = (
                entry.name.startswith('.tofu-') and '.sqlite3.tmp-' in entry.name
                and not entry.name.endswith(('-journal', '-wal', '-shm')))
            if not (is_published or is_temporary):
                continue
            matched += 1
            if matched > 256:
                truncated = True
                break
            try:
                size = max(0, int(entry.stat(follow_symlinks=False).st_size))
            except OSError:
                continue
            if is_published:
                published_count += 1
                published_bytes += size
            else:
                temporary_count += 1
                temporary_bytes += size
    return {
        'publishedCount': published_count,
        'publishedBytes': published_bytes,
        'temporaryCount': temporary_count,
        'temporaryBytes': temporary_bytes,
        'totalBytes': published_bytes + temporary_bytes,
        'scanTruncated': truncated,
    }


def sqlite_snapshot_status(project_path: str, now: float | None = None) -> dict:
    """Return cheap canonical backup freshness and retired-owner inventory."""
    project = Path(project_path).resolve()
    deployment_mode = _project_setting(
        str(project), 'TOFU_DEPLOYMENT_MODE', 'personal').strip().lower()
    db_path = project / 'data' / 'tofu.db'
    raw_dir = _project_setting(str(project), 'TOFU_SQLITE_SNAPSHOT_DIR', '')
    snapshot_dir = Path(os.path.expandvars(os.path.expanduser(raw_dir))) \
        if raw_dir else db_path.parent / 'backups'
    if not snapshot_dir.is_absolute():
        snapshot_dir = project / snapshot_dir
    try:
        max_age_hours = float(_project_setting(
            str(project), 'TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS', '26'))
    except (TypeError, ValueError):
        max_age_hours = 26.0
    max_age_hours = max(1.0, min(24.0 * 30, max_age_hours))
    stamp = time.time() if now is None else float(now)
    candidates, scan_truncated = _verified_sidecar_snapshots(snapshot_dir)
    latest = max(candidates, default=None, key=lambda item: (item[0], str(item[2])))
    age_hours = (max(0.0, stamp - latest[0]) / 3600.0) if latest else None
    # Invalid modes fail during runtime configuration. Keep diagnostics
    # conservative by requiring a snapshot for every non-distributed value.
    required = deployment_mode != 'distributed' and db_path.is_file()
    return {
        'required': required,
        'destinationConfigured': bool(raw_dir),
        'databasePath': str(db_path),
        'snapshotDir': str(snapshot_dir),
        'snapshotCount': len(candidates),
        'snapshotScanTruncated': scan_truncated,
        'latestPath': str(latest[2]) if latest else None,
        'latestSizeBytes': latest[1] if latest else None,
        'latestMtime': latest[0] if latest else None,
        'latestAgeHours': round(age_hours, 2) if age_hours is not None else None,
        'maxAgeHours': max_age_hours,
        'fresh': (age_hours <= max_age_hours) if age_hours is not None else False,
        'legacy': _legacy_snapshot_inventory(db_path.parent),
    }


def _gib_text(value: int | None) -> str:
    return 'unknown' if value is None else f'{value / (1 << 30):.1f} GiB'


def _classify_recovery_cron(text: str) -> tuple[list[str], list[str]]:
    lines = [line for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    legacy = [line for line in lines if 'tofu_guard' in line]
    manager = [line for line in lines
               if '# tofu_manager' in line and 'serverctl.py ensure' in line]
    return legacy, manager


def _doctor_findings(
        status: dict | None, *, guard_loops: list[str], legacy_cron: list[str],
        manager_cron: list[str], memory: dict, snapshot: dict,
        port_config: dict | None = None,
        port_drift: dict | None = None,
        startup_config: dict | None = None,
        now: float | None = None) -> list[dict[str, str]]:
    """Classify diagnostics once, with severity and an executable next step.

    Runtime failures are errors. Hardening gaps that do not stop today's
    server (boot recovery, off-host backups, RSS policy) are warnings. Keeping
    that distinction machine-readable prevents a support bot from treating a
    healthy personal install as down merely because it lacks a production
    topology recommendation.
    """
    findings: list[dict[str, str]] = []

    def add(code: str, severity: str, message: str, command: str = '') -> None:
        item = {'code': code, 'severity': severity, 'message': message}
        if command:
            item['command'] = command
        findings.append(item)

    observed = (status or {}).get('observed')
    desired = (status or {}).get('desired')
    drift_explains_readiness_failure = bool(
        port_drift and port_drift.get('ready'))
    if port_config and port_config.get('error'):
        currently_ready = _status_ready(status)
        add(
            'dotenv_unreadable', 'warning' if currently_ready else 'error',
            f"cannot read project .env: {port_config['error']}",
            'rewrite .env as valid UTF-8 under 262144 bytes and verify its permissions')
    elif port_config and not port_config.get('valid'):
        currently_ready = _status_ready(status)
        add(
            'port_config_invalid', 'warning' if currently_ready else 'error',
            f"invalid PORT={port_config.get('raw')!r} from "
            f"{port_config.get('source')}; diagnostics fell back to 15000",
            'set PORT to an integer from 1 to 65535 in .env')
    startup_error = str((startup_config or {}).get('error') or '')
    port_already_explains_error = bool(
        port_config and not port_config.get('valid'))
    if startup_error and not port_already_explains_error:
        currently_ready = _status_ready(status)
        add(
            'startup_config_invalid',
            'warning' if currently_ready else 'error',
            f'next startup is blocked by invalid listener configuration: '
            f'{startup_error}',
            'correct the reported setting in .env, then ' +
            _control_command('restart'))
    if status and port_config and port_config.get('valid') and not port_drift:
        configured_port = _valid_tcp_port(port_config.get('port'))
        manager_port = _valid_tcp_port(status.get('port'))
        stored_args = status.get('serverArgs')
        stored_override = (
            _requested_server_port(
                [str(item) for item in stored_args], {})
            if isinstance(stored_args, list) else None)
        if configured_port is not None and manager_port is not None \
                and configured_port != manager_port \
                and stored_override is None:
            currently_ready = _status_ready(status)
            add(
                'configured_port_not_applied',
                'warning' if currently_ready else 'error',
                f"configured port {configured_port} from "
                f"{port_config.get('source')} is not applied; manager and "
                f"worker still use {manager_port}",
                _control_command('restart'))
    if port_drift:
        if drift_explains_readiness_failure:
            message = (
                f"manager probes port {port_drift['managerPort']}, but locked "
                f"worker PID {port_drift['workerPid']} is ready at "
                f"{port_drift['url']}")
            command = _control_command(
                'restart', '--', '--port', port_drift['workerDeclaredPort'])
        else:
            message = (
                f"manager expects port {port_drift['managerPort']}, while locked "
                f"worker PID {port_drift['workerPid']} declares "
                f"{port_drift['workerDeclaredPort']}; alternate readiness was not verified")
            command = _control_command('logs', '-n', 200)
        add('worker_port_drift', 'error', message, command)
    if not status:
        add(
            'manager_offline', 'error',
            'project lifecycle manager is not running',
            _control_command('start'))
    elif drift_explains_readiness_failure:
        # The endpoint mismatch above is the root cause. Do not bury it under
        # a second generic "starting too long" or "health failed" finding.
        pass
    elif observed == 'maintenance':
        add(
            'storage_maintenance_active', 'warning',
            (status.get('lastError')
             or 'offline storage maintenance is active; startup is queued').strip(),
            _control_command('status'))
    elif observed in ('conflict', 'crashloop', 'degraded'):
        add(
            f'worker_{observed}', 'error',
            (status.get('lastError') or str(observed)).strip(),
            _control_command('logs', '-n', 200))
    elif observed == 'stopped' and desired == 'running':
        add(
            'worker_unexpectedly_stopped', 'error',
            'manager wants Tofu running, but no worker is running',
            _control_command('start'))
    elif observed == 'running' and not _status_liveness(status):
        add(
            'worker_health_failed', 'error',
            (status.get('lastError')
             or 'worker identity liveness probe is failing').strip(),
            _control_command('logs', '-n', 200))
    elif observed == 'running' and status.get('ready') is not True:
        add(
            'worker_readiness_failed', 'error',
            (status.get('lastError')
             or 'worker application readiness probe is failing').strip(),
            _control_command('logs', '-n', 200))
    elif observed in ('starting', 'stopping'):
        detail = (status.get('lastError') or '').strip()
        message = f'worker is {observed}; readiness is not established yet'
        if detail:
            message += f' ({detail})'
        transition_age = _startup_age_seconds(status, now=now)
        if observed == 'starting' and transition_age >= STARTUP_STUCK_SECONDS:
            add(
                'worker_startup_stuck', 'error',
                f'worker has not become ready after {transition_age / 60:.1f} minutes'
                + (f' ({detail})' if detail else ''),
                _control_command('logs', '-n', 200))
        else:
            add(
                f'worker_{observed}', 'warning', message,
                _control_command('status'))

    recent_failures = int((status or {}).get('recentFailureCount') or 0)
    max_failures = int((status or {}).get('maxFailures') or 5)
    if status and recent_failures >= max(3, max_failures - 2) \
            and observed != 'crashloop':
        add(
            'worker_unstable', 'warning',
            f'worker is unstable: {recent_failures}/{max_failures} recent failures',
            _control_command('logs', '-n', 200))
    if guard_loops or legacy_cron:
        add(
            'competing_lifecycle_owner', 'error',
            'legacy tofu_guard still competes with the lifecycle manager',
            _control_command('install-recovery'))
    if status and not manager_cron:
        add(
            'boot_recovery_missing', 'warning',
            'manager will not recover after host/session restart',
            _control_command('install-recovery'))
    if status and status.get('workerRssGuardEnabled') is False:
        add(
            'rss_guard_disabled', 'warning',
            'manager-side worker RSS recycle guard is disabled',
            'set TOFU_PROCESS_RSS_RECYCLE_MB to a safe non-zero ceiling')
    if memory.get('usagePct') is not None and memory['usagePct'] >= 90.0:
        add(
            'memory_pressure_critical', 'error',
            'cgroup memory pressure is critical (>=90%)',
            'isolate Tofu in a dedicated cgroup/container and add headroom')
    if snapshot.get('required') and not snapshot.get('fresh'):
        if snapshot.get('latestPath'):
            message = (
                f"SQLite snapshot is stale ({snapshot['latestAgeHours']:.1f}h > "
                f"{snapshot['maxAgeHours']:.1f}h)")
        else:
            message = 'no verified SQLite snapshot was found'
        add(
            'sqlite_snapshot_missing_or_stale', 'warning', message,
            'run/repair the Database Backup schedule and verify its destination')
    if snapshot.get('required') and not snapshot.get('destinationConfigured'):
        add(
            'sqlite_backup_same_failure_domain', 'warning',
            'SQLite snapshots still share the authority data directory/failure domain',
            'set TOFU_SQLITE_SNAPSHOT_DIR to a separately mounted backup target')
    legacy = snapshot.get('legacy') or {}
    if int(legacy.get('totalBytes') or 0) > 0:
        add(
            'legacy_sqlite_snapshot_artifacts', 'warning',
            'retired db_snapshots owner still holds '
            f"{_gib_text(int(legacy.get('totalBytes') or 0))} "
            f"({int(legacy.get('publishedCount') or 0)} published, "
            f"{int(legacy.get('temporaryCount') or 0)} interrupted)",
            'review data/db_snapshots after an independent canonical backup; '
            'retirement is operator-controlled')
    return findings


def build_doctor_report() -> dict:
    """Collect the canonical read-only lifecycle diagnostic report."""
    status = _remote_status(probe=True)
    low = read_lock_status(PROJECT)
    port_config = _configured_port_snapshot(PROJECT)
    try:
        startup_env = _forwarded_server_env()
        startup_error = _forwarded_server_options_error([], startup_env)
    except ManagerUnavailable as exc:
        # The dedicated dotenv/PORT finding below gives the more precise fix.
        startup_env = {}
        startup_error = str(exc)
    startup_config = {
        'valid': not startup_error,
        'error': startup_error or None,
        'tlsRequested': parse_env_boolean(startup_env.get('TOFU_TLS')) is True
            or bool(startup_env.get('TLS_CERTFILE')),
        'customCertificateConfigured': bool(
            startup_env.get('TLS_CERTFILE') and startup_env.get('TLS_KEYFILE')),
    }
    port = _tcp_port((status or {}).get('port'), port_config['port'])
    pids = listener_pids(port)
    port_drift = _worker_port_drift(status or {'port': port}, low)
    guard_flag = Path(PROJECT) / 'data' / '.tofu_guard_disabled'
    guard_loops = [line for line in _proc_lines('tofu_guard.sh --loop')
                   if PROJECT in line]
    manager_pid = (status or {}).get('managerPid')
    supervisors = [
        line for line in _proc_lines('supervisor.py')
        if PROJECT in line or (
            isinstance(manager_pid, int)
            and line.split(maxsplit=1)[:1] == [str(manager_pid)])
    ]
    memory = cgroup_memory_snapshot(low.get('pid'))
    resource_environment = dict(os.environ)
    # Reuse the same validated project .env overlay forwarded to the manager,
    # otherwise doctor would describe a zero-config budget while silently
    # omitting the operator overrides that the next worker will receive.
    resource_environment.update(startup_env)
    resource_environment['TOFU_PROJECT_PATH'] = PROJECT
    resource_environment['TOFU_DEPLOYMENT_MODE'] = _project_setting(
        PROJECT, 'TOFU_DEPLOYMENT_MODE', 'personal')
    resource_budget = resource_budget_manifest(resource_environment)
    snapshot = sqlite_snapshot_status(PROJECT)
    cron_lines: list[str] = []
    manager_cron_lines: list[str] = []
    try:
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                              timeout=3).stdout
        cron_lines, manager_cron_lines = _classify_recovery_cron(cron)
    except (OSError, subprocess.SubprocessError):
        pass
    report = {
        'projectPath': PROJECT,
        'managerOnline': status is not None,
        'managerStatus': status,
        'lock': low,
        'port': port,
        'portConfiguration': port_config,
        'startupConfiguration': startup_config,
        'listenerPids': pids,
        'portDrift': port_drift,
        'legacyGuardDisabled': guard_flag.exists(),
        'legacyGuardLoops': guard_loops,
        'legacyGuardCron': cron_lines,
        'managerRecoveryCron': manager_cron_lines,
        'managerBootRecoveryInstalled': bool(manager_cron_lines),
        'supervisorProcesses': supervisors,
        'memory': memory,
        'resourceBudget': resource_budget,
        'snapshot': snapshot,
    }
    findings = _doctor_findings(
        status, guard_loops=guard_loops, legacy_cron=cron_lines,
        manager_cron=manager_cron_lines, memory=memory, snapshot=snapshot,
        port_config=port_config, port_drift=port_drift,
        startup_config=startup_config)
    errors = [item['message'] for item in findings if item['severity'] == 'error']
    warnings = [item['message'] for item in findings if item['severity'] == 'warning']
    fixes = [item['command'] for item in findings if item.get('command')]
    ready = _status_ready(status)
    report['ready'] = ready
    report['applicationReachable'] = bool(
        status and _status_liveness(status)
        or (port_drift and port_drift.get('liveness')))
    report['applicationUrl'] = (
        _service_url(status) if ready and status else
        ((port_drift or {}).get('url')
         if (port_drift or {}).get('ready') else None))
    report['lifecycleHealthy'] = not errors
    report['healthy'] = report['lifecycleHealthy']
    report['findings'] = findings
    report['errors'] = errors
    report['warnings'] = warnings
    # Compatibility for existing operators: problems remains the complete flat
    # list while new consumers should use findings/errors/warnings.
    report['problems'] = [item['message'] for item in findings]
    report['fixes'] = list(dict.fromkeys(fixes))
    return report


def cmd_doctor(args: argparse.Namespace) -> int:
    report = build_doctor_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report['healthy'] else 1
    status = report['managerStatus']
    low = report['lock']
    port = report['port']
    pids = report['listenerPids']
    port_drift = report['portDrift']
    guard_loops = report['legacyGuardLoops']
    cron_lines = report['legacyGuardCron']
    manager_cron_lines = report['managerRecoveryCron']
    memory = report['memory']
    resource_budget = report['resourceBudget']
    snapshot = report['snapshot']
    findings = report['findings']
    errors = report['errors']
    warnings = report['warnings']
    print(f'Project : {PROJECT}')
    print(f"Manager : {'OK' if status is not None else 'MISSING'}")
    print(f"Lock    : {'live pid ' + str(low.get('pid')) if low.get('running') else 'no live worker'}")
    print(f"Port    : {port} -> {pids or 'free/no PID visibility'}")
    if port_drift:
        health = (
            'ready' if port_drift.get('ready') else
            ('live but not ready' if port_drift.get('liveness')
             else 'not live'))
        print(f"Worker  : PID {port_drift['workerPid']} declares "
              f"{port_drift['workerDeclaredPort']} ({health})"
              + (f" -> {port_drift['url']}" if port_drift.get('url') else ''))
    print(f"Guard   : {'disabled' if report['legacyGuardDisabled'] else 'enabled'}; "
          f'{len(guard_loops)} loop(s), {len(cron_lines)} cron line(s)')
    print(f"Recovery: {'installed' if manager_cron_lines else 'NOT installed'}"
          f'; {len(manager_cron_lines)} manager cron line(s)')
    print(f"Memory  : {_gib_text(memory['usageBytes'])} / "
          f"{_gib_text(memory['limitBytes'])}"
          + (f" ({memory['usagePct']:.1f}%)" if memory['usagePct'] is not None else '')
          + f"; swap limit {_gib_text(memory['swapLimitBytes'])}")
    print(f"OOM     : kernel kills={memory['oomKills'] if memory['oomKills'] is not None else 'unknown'}; "
          f"worker RSS={_gib_text(memory['workerRssBytes'])}")
    probe = resource_budget['probe']
    defaults = resource_budget['defaults']
    available_mb = probe['effective_memory_available_mb']
    capacity_mb = probe['effective_memory_capacity_mb']
    disk_free_mb = probe['disk_free_mb']
    print(
        f"Probe   : cpu={probe['effective_cpu_count']}; memory "
        f"{'unknown' if available_mb is None else available_mb} / "
        f"{'unknown' if capacity_mb is None else capacity_mb} MiB available; "
        f"disk free {'unknown' if disk_free_mb is None else disk_free_mb} MiB "
        f"at {probe['data_path']}")
    print(
        f"Budget  : tasks={defaults['TOFU_MAX_INFLIGHT_TASKS']}; "
        f"API rounds={defaults['TOFU_TASK_MAX_API_ROUNDS']}; "
        f"sync/agent={defaults['TOFU_SYNC_WORKERS']}/"
        f"{defaults['TOFU_AGENT_WORKERS']}; storage RPC="
        f"{defaults['TOFU_STORAGE_RPC_CAPACITY']}; RSS soft/hard="
        f"{defaults['TOFU_PROCESS_RSS_RELIEF_MB']}/"
        f"{defaults['TOFU_PROCESS_RSS_RECYCLE_MB']} MiB; logs="
        f"{defaults['TOFU_LOG_TOTAL_BUDGET_MB']} MiB; storage reserve="
        f"{int(defaults['TOFU_STORAGE_MIN_FREE_BYTES']) // (1024 * 1024)} MiB")
    overrides = resource_budget['overrides']
    if overrides:
        print('Override: ' + '; '.join(
            f'{name}={value}' for name, value in sorted(overrides.items())))
    if status and status.get('workerRssGuardEnabled'):
        print(f"RSS cap : {_gib_text(status.get('workerRssRecycleBytes'))} "
              '(manager-enforced)')
    if snapshot['latestPath']:
        print(f"Snapshot: {snapshot['latestAgeHours']:.1f}h old; "
              f"{_gib_text(snapshot['latestSizeBytes'])}; "
              f"{snapshot['latestPath']}")
    elif snapshot['required']:
        print(f"Snapshot: MISSING under {snapshot['snapshotDir']}")
    for finding in findings:
        label = 'Error' if finding['severity'] == 'error' else 'Warning'
        print(f'{label:<8}: [{finding["code"]}] {finding["message"]}')
        if finding.get('command'):
            print(f'Next    : {finding["command"]}')
    if report['healthy']:
        if warnings:
            print(f'Result  : no blocking lifecycle errors; {len(warnings)} recommendation(s)')
        else:
            print('Result  : lifecycle ownership is consistent')
    else:
        print(f'Result  : {len(errors)} blocking lifecycle error(s)')
    return 0 if report['healthy'] else 1


def cmd_support_bundle(args: argparse.Namespace) -> int:
    """Emit one bounded, sanitized offline-capable diagnostic artifact."""
    from serverctl_pkg.support_bundle import (
        build_support_bundle,
        write_support_bundle,
    )

    try:
        bundle = build_support_bundle(
            PROJECT, build_doctor_report(), lines=args.lines,
            include_logs=not args.no_logs)
        if not args.output or args.output == '-':
            print(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            target = write_support_bundle(args.output, bundle)
            print(f'Support bundle written to {target}')
        if not args.no_logs:
            print(
                'Review before sharing: log tails can contain user-provided '
                'text or unrecognized secrets.',
                file=sys.stderr)
        return 0
    except FileExistsError:
        print(
            f'Refusing to overwrite existing support bundle: {args.output}',
            file=sys.stderr)
        print('Choose a new --output path or remove the old file intentionally.',
              file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(f'Could not build support bundle: {exc}', file=sys.stderr)
        return 1


def cmd_inspect_conversation(args: argparse.Namespace) -> int:
    """Run the repository's read-only conversation inspector unchanged."""
    script = Path(PROJECT) / 'debug' / 'inspect_conversation.py'
    if not script.is_file():
        print(f'Conversation inspector is missing: {script}', file=sys.stderr)
        print('Restore the complete source checkout before inspecting storage.',
              file=sys.stderr)
        return 1

    command = [sys.executable, str(script), args.conversation_id]
    if args.db is not None:
        command.extend(['--db', str(args.db)])
    if args.user_id is not None:
        command.extend(['--user-id', str(args.user_id)])
    if args.full:
        command.append('--full')
    if args.raw:
        command.append('--raw')
    if args.no_logs:
        command.append('--no-logs')
    else:
        command.extend(['--logs', str(args.lines)])
    try:
        return int(subprocess.call(command, cwd=PROJECT))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f'Could not run conversation inspector: {exc}', file=sys.stderr)
        return 1


def _replace_guard_cron() -> tuple[bool, str]:
    try:
        old = subprocess.run(['crontab', '-l'], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    kept = [line for line in old.splitlines()
            if '# tofu_guard' not in line and '# tofu_manager' not in line]
    qproject = shlex.quote(PROJECT)
    qpython = shlex.quote(sys.executable)
    command = (f'@reboot cd {qproject} && {qpython} serverctl.py ensure '
               f'>/dev/null 2>&1 # tofu_manager')
    minute = (f'* * * * * cd {qproject} && {qpython} serverctl.py ensure '
              f'>/dev/null 2>&1 # tofu_manager')
    content = '\n'.join([*kept, command, minute]) + '\n'
    try:
        applied = subprocess.run(['crontab', '-'], input=content, text=True,
                                 capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if applied.returncode:
        return False, applied.stderr.strip()
    return True, ''


def cmd_install(_args: argparse.Namespace) -> int:
    health = ensure_manager()
    status = _remote_status(probe=True) or {}
    if status.get('observed') == 'conflict':
        print('Manager cannot take ownership safely: ' +
              (status.get('lastError') or 'external owner detected'), file=sys.stderr)
        print(f'Diagnose: {_control_command("doctor")}', file=sys.stderr)
        return 1
    # Commit the replacement recovery schedule before disabling the legacy
    # owner. If crontab cannot be updated, the old boot recovery remains
    # untouched instead of leaving a half-installed manager after logout.
    ok, error = _replace_guard_cron()
    if not ok:
        print(f'Manager is running, but cron migration failed: {error}', file=sys.stderr)
        print('Legacy recovery was left unchanged.', file=sys.stderr)
        return 1

    # A healthy manager makes the retired guard's public ``--stop`` command
    # deliberately return 2 and direct operators to ``serverctl.py stop``.
    # Calling it here therefore turns a successful cron migration into a false
    # installation failure.  The cron entries are already gone; terminate only
    # any still-running legacy loops without changing the worker's desired
    # state or invoking that retired command surface.
    for line in _proc_lines('tofu_guard.sh --loop'):
        if PROJECT not in line:
            continue
        raw = line.split(None, 1)[0]
        if raw.isdigit():
            try:
                os.kill(int(raw), signal.SIGTERM)
            except OSError:
                pass
    print(f"Tofu manager installed (PID {health.get('managerPid')}).")
    print('Legacy tofu_guard cron entries were replaced; the current server was not restarted.')
    return 0


def cmd_ensure(_args: argparse.Namespace) -> int:
    try:
        ensure_manager()
        return 0
    except ManagerUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='serverctl.py',
                                     description='Manage the project-local Tofu server')
    parser.add_argument('--version', action='version', version=_version_text())
    sub = parser.add_subparsers(dest='command', required=True)

    start = sub.add_parser(
        'start', help='start Tofu idempotently',
        description='Start or join the one manager-owned Tofu worker and wait for readiness.')
    start.add_argument(
        '--wait', type=_wait_seconds, default=180.0, metavar='SECONDS',
        help='maximum readiness wait (default: 180)')
    start.add_argument('--source', default='serverctl', help=argparse.SUPPRESS)
    start.add_argument(
        'server_args', nargs=argparse.REMAINDER, metavar='-- SERVER_OPTIONS',
        help='options after -- are forwarded to server.py')
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser('stop', help='stop Tofu and keep it stopped')
    stop.add_argument(
        '-y', '--yes', action='store_true',
        help=('skip the terminal prompt; non-interactive callers still need '
              'a human-approved shutdown token'))
    stop.add_argument('--source', default='serverctl', help=argparse.SUPPRESS)
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser('restart', help='restart Tofu through the manager')
    restart.add_argument(
        '-y', '--yes', action='store_true',
        help=('skip the terminal prompt; non-interactive callers still need '
              'a human-approved restart token'))
    restart.add_argument(
        '--wait', type=_wait_seconds, default=180.0, metavar='SECONDS',
        help='maximum readiness wait (default: 180)')
    restart.add_argument('--source', default='serverctl', help=argparse.SUPPRESS)
    restart.add_argument(
        'server_args', nargs=argparse.REMAINDER, metavar='-- SERVER_OPTIONS',
        help='replacement options after -- are forwarded to server.py')
    restart.set_defaults(func=cmd_restart)

    status = sub.add_parser('status', help='show owner, desired and observed state')
    status.add_argument(
        '--json', action='store_true',
        help=('emit machine-readable managed readiness and actual application '
              'reachability'))
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser('logs', help='show worker or manager logs')
    logs.add_argument('-f', '--follow', action='store_true', help='follow new lines')
    logs.add_argument(
        '--manager', action='store_true',
        help='show lifecycle-manager log instead of worker console')
    logs.add_argument(
        '-n', '--lines', type=_bounded_log_lines, default=100, metavar='N',
        help='recent lines to show, 1-1000 (default: 100)')
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser('doctor', help='read-only lifecycle diagnostics')
    doctor.add_argument(
        '--json', action='store_true',
        help='emit findings with stable code, severity, message, and command fields')
    doctor.set_defaults(func=cmd_doctor)

    support = sub.add_parser(
        'support-bundle',
        help='emit bounded diagnostics with common credentials redacted')
    support.add_argument(
        '--output', metavar='FILE',
        help='write mode-0600 JSON to a new file (default: stdout)')
    support.add_argument(
        '--lines', type=_bounded_log_lines, default=200, metavar='N',
        help='recent lines per log, bounded to 1-1000 (default: 200)')
    support.add_argument(
        '--no-logs', action='store_true',
        help='omit all log tails for a metadata-only privacy mode')
    support.set_defaults(func=cmd_support_bundle)

    login = sub.add_parser(
        'login-url',
        help='print the local UI URL or recover the first-run private login URL',
        description=(
            'Print a copyable browser URL. In private mode this explicitly '
            'reveals the still-valid first-run admin token; do not share the '
            'output or include it in support bundles.'))
    login.add_argument(
        '--base-url', default='', metavar='URL',
        help='public http(s) origin when it differs from the local listener')
    login.set_defaults(func=cmd_login_url)

    inspect = sub.add_parser(
        'inspect-conversation',
        help='read one conversation and its matching log evidence',
        description=(
            'Inspect a conversation ID in one read-only pass. The transcript '
            'and matching logs can contain private user text; review output '
            'before sharing it.'))
    inspect.add_argument('conversation_id', metavar='CONVERSATION_ID')
    inspect.add_argument(
        '--db', type=Path, metavar='PATH',
        help='SQLite authority (default: data/tofu.db in this checkout)')
    inspect.add_argument(
        '--user-id', type=int, default=None, metavar='N',
        help='owning user (default: auto-detect from the conversation row)')
    inspect.add_argument(
        '--full', action='store_true', help='show every message without truncation')
    inspect.add_argument(
        '--raw', action='store_true', help='emit the full messages array as JSON')
    inspect.add_argument(
        '--lines', type=_bounded_log_lines, default=50, metavar='N',
        help='matching lines per log, bounded to 1-1000 (default: 50)')
    inspect.add_argument(
        '--no-logs', action='store_true', help='omit matching log lines')
    inspect.set_defaults(func=cmd_inspect_conversation)

    sub.add_parser(
        'install-recovery',
        help='install manager boot recovery and migrate the legacy guard',
        description=(
            'Install boot/session recovery for an existing Tofu checkout. '
            'This does not install the application or its dependencies.')) \
        .set_defaults(func=cmd_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Cron's internal recovery verb stays accepted but is intentionally absent
    # from human/model-facing help; it is not an operation users should choose.
    if raw == ['ensure']:
        try:
            return cmd_ensure(argparse.Namespace())
        except ManagerUnavailable as exc:
            print(f'Tofu manager unavailable: {exc}', file=sys.stderr)
            return 1
    if raw and raw[0] == 'install':
        print(
            "'serverctl.py install' was renamed to 'install-recovery'; "
            'continuing for compatibility.',
            file=sys.stderr)
        raw[0] = 'install-recovery'
    parser = build_parser()
    if not raw:
        parser.print_help()
        return 0
    args = parser.parse_args(raw)
    try:
        return int(args.func(args))
    except ManagerUnavailable as exc:
        print(f'Tofu manager unavailable: {exc}', file=sys.stderr)
        print(f'Diagnose: {_control_command("doctor")}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

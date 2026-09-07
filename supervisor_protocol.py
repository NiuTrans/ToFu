"""Host-local lifecycle protocol shared by Tofu workers and control clients.

Responsibility
--------------
Identify the exact source generation loaded by the long-lived Supervisor,
reload that process without stopping its independently-owned worker, and ask
the refreshed Supervisor to replace a worker after the initiating HTTP
response has already flushed.

This module intentionally uses only the Python standard library.  Lifecycle
repair must remain available when the application dependency graph is broken.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any
import urllib.error
import urllib.request


SUPERVISOR_VERSION = '0.3.0'
SUPERVISOR_PROTOCOL_VERSION = 1

# These files form the dependency closure whose already-imported definitions
# affect every future worker generation.  A content digest, rather than mtimes,
# also works across release overlays that preserve timestamps.
SUPERVISOR_SOURCE_FILES = (
    'supervisor.py',
    'supervisor_protocol.py',
    'server_manager.py',
    'runtime_guards.py',
    'tofu_dotenv.py',
)


class SupervisorRefreshError(RuntimeError):
    """The active lifecycle owner could not be proven current and healthy."""


def supervisor_source_fingerprint(project_path: str) -> str:
    """Return a deterministic digest of the Supervisor dependency closure."""
    project = Path(project_path).resolve()
    digest = hashlib.sha256()
    for relative_name in SUPERVISOR_SOURCE_FILES:
        path = project / relative_name
        digest.update(relative_name.encode('utf-8'))
        digest.update(b'\0')
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f'<unreadable:{type(exc).__name__}>'.encode('ascii'))
        digest.update(b'\0')
    return digest.hexdigest()


def supervisor_base_url(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve the project-local manager endpoint from the declared knobs."""
    env = os.environ if environment is None else environment
    host = str(env.get('TOFU_SUPERVISOR_HOST') or '127.0.0.1')
    try:
        port = int(env.get('TOFU_SUPERVISOR_PORT') or 15001)
    except (TypeError, ValueError):
        port = 15001
    if not 1 <= port <= 65535:
        port = 15001
    return f'http://{host}:{port}'


def request_supervisor_json(
    base_url: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Call one manager endpoint and retain structured HTTP failures."""
    encoded = json.dumps(body).encode('utf-8') if body is not None else None
    request = urllib.request.Request(
        base_url.rstrip('/') + path,
        data=encoded,
        method='POST' if body is not None else 'GET',
    )
    if encoded is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8') or '{}')
            return payload if isinstance(payload, dict) else {
                'ok': False,
                'message': 'Supervisor returned a non-object response.',
            }
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8') or '{}')
        except (TypeError, ValueError):
            payload = {'ok': False, 'message': str(exc)}
        if not isinstance(payload, dict):
            payload = {'ok': False, 'message': str(exc)}
        payload.setdefault('httpStatus', int(exc.code))
        return payload
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SupervisorRefreshError(str(exc)) from exc


def supervisor_generation_matches(
    health: Mapping[str, Any] | None,
    project_path: str,
) -> bool:
    """Return whether *health* proves the current checkout is loaded."""
    if not health or health.get('ok') is not True:
        return False
    project = os.path.realpath(project_path)
    projects = health.get('projects')
    if not isinstance(projects, list) or project not in projects:
        return False
    source_project = health.get('sourceProjectPath')
    if source_project and os.path.realpath(str(source_project)) != project:
        return False
    return health.get('sourceFingerprint') == supervisor_source_fingerprint(project)


def _verified_legacy_manager_pid(
    health: Mapping[str, Any],
    project_path: str,
) -> int | None:
    """Resolve a Linux PID only after both protocol and process identity agree."""
    project = os.path.realpath(project_path)
    projects = health.get('projects')
    try:
        pid = int(health.get('managerPid') or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 1 or pid == os.getpid() or not isinstance(projects, list) \
            or project not in projects:
        return None
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        argv = [part.decode('utf-8', errors='replace')
                for part in raw.split(b'\0') if part]
        cwd = os.path.realpath(os.readlink(f'/proc/{pid}/cwd'))
    except OSError:
        return None
    if cwd != project:
        return None
    if not any(os.path.basename(argument) == 'supervisor.py'
               for argument in argv):
        return None
    return pid


def _launch_supervisor(
    project_path: str,
    environment: Mapping[str, str],
) -> None:
    project = os.path.realpath(project_path)
    child_environment = dict(environment)
    child_environment['TOFU_SUPERVISOR_PROJECTS'] = project
    child_environment['TOFU_SUPERVISOR_PYTHON'] = sys.executable
    script = os.path.join(project, 'supervisor.sh')
    try:
        completed = subprocess.run(
            ['bash', script, 'daemon'],
            cwd=project,
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=25.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SupervisorRefreshError(
            f'could not launch the refreshed Supervisor: {exc}') from exc
    if completed.returncode:
        detail = ((completed.stdout or '') + (completed.stderr or '')).strip()
        raise SupervisorRefreshError(
            'refreshed Supervisor launcher failed: '
            + (detail[-1000:] or f'exit {completed.returncode}'))


def refresh_supervisor(
    project_path: str,
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Ensure the live Supervisor has loaded the current source generation.

    Current Supervisors re-exec themselves and leave the worker untouched.
    A one-time compatibility path retires a pre-protocol Supervisor only after
    validating its PID, cwd, command line, and project allow-list; its
    watchdog/systemd owner normally brings the new image back.  If it does
    not, the canonical detached launcher is invoked once.
    """
    env = dict(os.environ if environment is None else environment)
    project = os.path.realpath(project_path)
    base_url = supervisor_base_url(env)
    expected = supervisor_source_fingerprint(project)
    initial_health: dict[str, Any] | None = None
    try:
        initial_health = request_supervisor_json(
            base_url, '/health', timeout=1.0)
    except SupervisorRefreshError:
        initial_health = None
    if supervisor_generation_matches(initial_health, project):
        return {
            'ok': True,
            'reloaded': False,
            'sourceFingerprint': expected,
            'managerPid': initial_health.get('managerPid'),
        }

    reload_accepted = False
    retired_legacy = False
    if initial_health and initial_health.get('ok') is True:
        try:
            response = request_supervisor_json(
                base_url,
                '/reload',
                {
                    'projectPath': project,
                    'expectedFingerprint': expected,
                    'source': 'source-generation-refresh',
                },
                timeout=2.0,
            )
        except SupervisorRefreshError:
            response = {'ok': False}
        reload_accepted = response.get('ok') is True
        if not reload_accepted:
            # Versions before the reload protocol answer 404.  Retire only a
            # process whose host identity we can prove; never signal a bare PID
            # supplied by an untrusted/mismatched endpoint.
            legacy_pid = _verified_legacy_manager_pid(initial_health, project)
            if legacy_pid is None:
                raise SupervisorRefreshError(
                    'Supervisor source is stale, but its process identity '
                    'could not be verified for a safe compatibility reload.')
            try:
                os.kill(legacy_pid, signal.SIGTERM)
            except OSError as exc:
                raise SupervisorRefreshError(
                    f'could not retire legacy Supervisor PID {legacy_pid}: {exc}'
                ) from exc
            retired_legacy = True

    deadline = time.monotonic() + max(1.0, float(timeout))
    launch_attempted = False
    launch_after = time.monotonic() + (3.0 if initial_health else 0.0)
    last_error = ''
    while time.monotonic() < deadline:
        try:
            health = request_supervisor_json(base_url, '/health', timeout=1.0)
        except SupervisorRefreshError as exc:
            health = None
            last_error = str(exc)
        if supervisor_generation_matches(health, project):
            return {
                'ok': True,
                'reloaded': True,
                'reloadAccepted': reload_accepted,
                'retiredLegacy': retired_legacy,
                'sourceFingerprint': expected,
                'managerPid': health.get('managerPid'),
            }
        if not launch_attempted and time.monotonic() >= launch_after:
            _launch_supervisor(project, env)
            launch_attempted = True
        time.sleep(0.2)
    raise SupervisorRefreshError(
        'Supervisor did not load the current source generation within '
        f'{max(1.0, float(timeout)):.1f}s'
        + (f' ({last_error})' if last_error else ''))


def request_deferred_worker_restart(
    project_path: str,
    *,
    source: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Ask the current Supervisor to restart its worker after replying."""
    env = os.environ if environment is None else environment
    project = os.path.realpath(project_path)
    refresh = refresh_supervisor(project, environment=env)
    response = request_supervisor_json(
        supervisor_base_url(env),
        '/restart-deferred',
        {'projectPath': project, 'source': source},
        timeout=3.0,
    )
    if response.get('ok') is not True:
        raise SupervisorRefreshError(
            str(response.get('message') or response.get('error')
                or 'Supervisor refused the deferred worker restart.'))
    return {**response, 'supervisorRefresh': refresh}


__all__ = [
    'SUPERVISOR_PROTOCOL_VERSION',
    'SUPERVISOR_SOURCE_FILES',
    'SUPERVISOR_VERSION',
    'SupervisorRefreshError',
    'refresh_supervisor',
    'request_deferred_worker_restart',
    'request_supervisor_json',
    'supervisor_base_url',
    'supervisor_generation_matches',
    'supervisor_source_fingerprint',
]

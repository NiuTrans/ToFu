#!/usr/bin/env python3
"""supervisor.py — always-on process supervisor for remote start/stop of Tofu.

The Android app is a WebView shell that talks HTTP to a Tofu server. It cannot
run host shell commands, and — critically — it cannot *start* a stopped Tofu
server, because a stopped server can't answer the "start me" request. This
daemon breaks that chicken-and-egg: it is a tiny, ALWAYS-ON process whose only
job is to spawn / kill ``server.py`` for an allow-listed project path, so the
app can start and stop Tofu remotely.

Design (see android/docs/SUPERVISOR_DESIGN.md, in this repo):

  * Runs on a fixed port (default 15001), exposed behind the SAME code-server
    that proxies Tofu (``…/proxy/15001/``), so it inherits the code-server
    password gate.
  * NO separate auth. Tofu is a PERSONAL app; the code-server password already
    gates the whole proxy (and code-server's own terminal can already run any
    shell command), so a second supervisor token would guard a door that is
    already locked — pure friction for the single user. The only guard kept is
    the ``projectPath`` allow-list, which is CONFIG ("which projects may I
    manage"), not authentication — it needs nothing typed at runtime.
  * Start is idempotent (a live lock → no second process). Stop reuses the
    project's own ``stop.sh`` verbatim (SIGTERM→graceful→SIGKILL, host-scoped,
    PID-reuse-guarded) rather than reimplementing kill logic.
  * ``/start`` returns immediately; ``server.py`` takes a few seconds to bind,
    so the caller polls ``/status`` for the authoritative running state.
  * ``projectPath`` is validated against a strict allow-list
    (``TOFU_SUPERVISOR_PROJECTS``) — exact realpath match, no globbing — to
    keep "run python in a directory" from becoming arbitrary RCE.

Endpoints (all under the proxied prefix):

    GET  /health                     → {ok, version}
    GET  /status?projectPath=<abs>   → {running, pid, host, …}
    POST /start   {projectPath}      → {ok, running, pid, …}
    POST /stop    {projectPath}      → {ok, wasRunning, …}

Launch (owner-ratified): a systemd USER UNIT with ``Restart=always``; fall back
to ``supervisor.sh`` + nohup where user-lingering is unavailable.
"""

import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from server_manager import LifecycleManager

# The lifecycle manager must stay available when application imports or the DB
# are broken. Keep its dependency closure strictly standard-library; the shell
# redirects this stream to logs/server-manager.log.
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
logger = logging.getLogger('supervisor')


def audit_log(event, **details):
    logger.info('[audit] %s %s', event, details)


SUPERVISOR_VERSION = '0.2.0'
DEFAULT_PORT = 15001

# ── Environment knobs ─────────────────────────────────────────────────
ENV_PROJECTS = 'TOFU_SUPERVISOR_PROJECTS'   # ':'-separated absolute project paths
ENV_PORT = 'TOFU_SUPERVISOR_PORT'
ENV_HOST = 'TOFU_SUPERVISOR_HOST'
ENV_PYTHON = 'TOFU_SUPERVISOR_PYTHON'       # interpreter used to launch server.py


# ══════════════════════════════════════════════════════════════════════
#  Pure logic (unit-testable without a live HTTP server or real processes)
# ══════════════════════════════════════════════════════════════════════

def parse_allowlist(raw):
    """Parse ``TOFU_SUPERVISOR_PROJECTS`` into a set of canonical abs paths.

    Each entry is ``os.path.realpath``-normalised so ``..`` traversal and
    symlinks cannot smuggle a path past the exact-match check. Blank entries
    are ignored.

    Args:
        raw: The raw env value (``a:b:c``) or None.

    Returns:
        A set of canonical absolute paths.
    """
    if not raw:
        return set()
    out = set()
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.add(os.path.realpath(part))
    return out


def is_allowed(project_path, allowlist):
    """True iff *project_path* is in the allow-list AND is a real Tofu checkout.

    A path is runnable only when it (a) canonicalises to an allow-listed entry
    and (b) actually contains ``server.py`` and ``stop.sh`` — so a stale
    allow-list entry can't spawn against a directory that has since lost the
    scripts.
    """
    if not project_path:
        return False
    canon = os.path.realpath(project_path)
    if canon not in allowlist:
        return False
    return (os.path.isfile(os.path.join(canon, 'server.py'))
            and os.path.isfile(os.path.join(canon, 'stop.sh')))


def _lock_path(project_path):
    """Path to the project's server lock — mirrors stop.sh (``data/.server.lock``)."""
    return os.path.join(os.path.realpath(project_path), 'data', '.server.lock')


def _pid_is_server(pid):
    """True if *pid* is alive AND its cmdline looks like server.py.

    Mirrors stop.sh's defensive check: a bare ``kill -0`` is not enough because
    the PID could have been reused by an unrelated process after a crash.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'args='],
            capture_output=True, text=True, timeout=5,
        )
        return 'server.py' in (out.stdout or '')
    except Exception as e:
        logger.warning('ps check for pid %s failed: %s', pid, e)
        # Fail-safe: we confirmed the pid is alive; treat as running so we do
        # not spawn a duplicate. A false "running" is safer than a double-start.
        return True


def read_status(project_path):
    """Read the running state of the Tofu server for *project_path*.

    Parses ``<project>/data/.server.lock`` (``<pid>@<host>``) and confirms
    liveness the same way stop.sh does.

    Returns:
        dict with keys: running(bool), pid(int|None), host(str|None),
        sameHost(bool), projectPath(str), lockPresent(bool), stale(bool).
    """
    canon = os.path.realpath(project_path)
    lock = _lock_path(canon)
    result = {
        'projectPath': canon,
        'running': False,
        'pid': None,
        'host': None,
        'sameHost': None,
        'lockPresent': False,
        'stale': False,
    }
    if not os.path.isfile(lock):
        return result
    result['lockPresent'] = True
    try:
        with open(lock, 'r') as fh:
            entry = (fh.readline() or '').strip()
    except OSError as e:
        logger.warning('Could not read lock %s: %s', lock, e)
        return result
    if not entry or '@' not in entry:
        result['stale'] = True
        return result
    pid_str, _, host = entry.partition('@')
    if not pid_str.isdigit():
        logger.warning('Malformed lock entry %r in %s', entry, lock)
        result['stale'] = True
        return result
    pid = int(pid_str)
    result['pid'] = pid
    result['host'] = host or None
    try:
        this_host = os.uname().nodename
    except Exception:
        this_host = None
    result['sameHost'] = (host == this_host) if (host and this_host) else None
    if _pid_is_server(pid):
        result['running'] = True
    else:
        # Lock present but no live server.py at that pid → stale lock.
        result['stale'] = True
    return result


def do_start(project_path, python_exe=None):
    """Start ``server.py`` for *project_path* if not already running (idempotent).

    Returns immediately after spawning — the caller polls ``read_status`` for
    the authoritative running state, since ``server.py`` binds asynchronously.

    Returns:
        dict: {ok, alreadyRunning, launcherPid|None, message}.
    """
    canon = os.path.realpath(project_path)
    status = read_status(canon)
    if status['running']:
        logger.info('start: %s already running (pid=%s)', canon, status['pid'])
        return {'ok': True, 'alreadyRunning': True,
                'launcherPid': status['pid'],
                'message': 'already running'}

    py = python_exe or os.environ.get(ENV_PYTHON) or sys.executable
    data_dir = os.path.join(canon, 'data')
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        logger.warning('start: could not ensure data dir %s: %s', data_dir, e)
    log_path = os.path.join(data_dir, 'supervisor-server.log')
    try:
        log_fh = open(log_path, 'ab')
    except OSError as e:
        logger.error('start: cannot open server log %s: %s', log_path, e, exc_info=True)
        return {'ok': False, 'alreadyRunning': False, 'launcherPid': None,
                'message': f'cannot open log: {e}'}
    try:
        # start_new_session detaches the child into its own process group so it
        # survives this request / a supervisor restart. Output → the log file.
        proc = subprocess.Popen(
            [py, 'server.py'],
            cwd=canon,
            env=os.environ.copy(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error('start: failed to spawn server.py in %s: %s', canon, e, exc_info=True)
        try:
            log_fh.close()
        except OSError:
            pass
        return {'ok': False, 'alreadyRunning': False, 'launcherPid': None,
                'message': f'spawn failed: {e}'}
    finally:
        # The child inherits the fd; the parent can close its copy.
        try:
            log_fh.close()
        except OSError:
            pass
    audit_log('supervisor_start', project=canon, launcher_pid=proc.pid, python=py)
    logger.info('start: spawned server.py in %s (launcher pid=%s)', canon, proc.pid)
    return {'ok': True, 'alreadyRunning': False, 'launcherPid': proc.pid,
            'message': 'started; poll /status for bind'}


def do_stop(project_path, timeout=30):
    """Stop the Tofu server for *project_path* by running its own ``stop.sh``.

    Reuses stop.sh verbatim so all the kill semantics (host guard, graceful
    SIGTERM → SIGKILL escalation, PID-reuse defence, exit codes) live in one
    place. stop.sh exit codes: 0 clean / nothing running, 1 refused, 2 SIGKILL.

    Returns:
        dict: {ok, wasRunning, exitCode, output, message}.
    """
    canon = os.path.realpath(project_path)
    was_running = read_status(canon)['running']
    stop_sh = os.path.join(canon, 'stop.sh')
    if not os.path.isfile(stop_sh):
        logger.error('stop: no stop.sh in %s', canon)
        return {'ok': False, 'wasRunning': was_running, 'exitCode': None,
                'output': '', 'message': 'stop.sh not found'}
    try:
        res = subprocess.run(
            ['bash', 'stop.sh'],
            cwd=canon,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        logger.error('stop: stop.sh timed out after %ss in %s', timeout, canon)
        return {'ok': False, 'wasRunning': was_running, 'exitCode': None,
                'output': (e.output or '') if isinstance(e.output, str) else '',
                'message': 'stop.sh timed out'}
    except Exception as e:
        logger.error('stop: stop.sh failed in %s: %s', canon, e, exc_info=True)
        return {'ok': False, 'wasRunning': was_running, 'exitCode': None,
                'output': '', 'message': f'stop.sh error: {e}'}
    code = res.returncode
    out = (res.stdout or '') + (res.stderr or '')
    # 0 = clean / nothing running, 2 = had to SIGKILL (still stopped). 1 = refused.
    ok = code in (0, 2)
    audit_log('supervisor_stop', project=canon, exit_code=code, was_running=was_running)
    logger.info('stop: stop.sh exit=%s in %s', code, canon)
    return {'ok': ok, 'wasRunning': was_running, 'exitCode': code,
            'output': out[-2000:], 'message': 'stopped' if ok else 'stop refused'}


# ══════════════════════════════════════════════════════════════════════
#  HTTP layer (thin — delegates to the pure logic above)
# ══════════════════════════════════════════════════════════════════════

class SupervisorHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter. Config is read from the server instance attributes."""

    server_version = f'TofuSupervisor/{SUPERVISOR_VERSION}'

    # Silence the default noisy stderr access log; route through our logger.
    def log_message(self, fmt, *args):
        logger.info('%s - %s', self.address_string(), fmt % args)

    # ── helpers ──
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception as e:
            logger.warning('bad JSON body: %s', e)
            return {}

    def _check_allowed(self, project_path):
        allowlist = getattr(self.server, 'allowlist', set())
        if not is_allowed(project_path, allowlist):
            self._send_json(403, {'ok': False,
                                  'error': 'projectPath not in allow-list',
                                  'projectPath': project_path})
            return False
        return True

    def _manager(self, project_path):
        return self.server.managers[os.path.realpath(project_path)]

    # ── routes ──
    def do_GET(self):
        route = urlparse(self.path)
        path = route.path.rstrip('/') or '/'
        if path == '/health':
            self._send_json(200, {
                'ok': True,
                'version': SUPERVISOR_VERSION,
                'managerPid': os.getpid(),
                'projects': sorted(getattr(self.server, 'allowlist', set())),
            })
            return
        if path == '/status':
            qs = parse_qs(route.query)
            project_path = (qs.get('projectPath', [''])[0] or '').strip()
            if not self._check_allowed(project_path):
                return
            probe = (qs.get('probe', ['0'])[0] == '1')
            self._send_json(200, {
                'ok': True,
                **self._manager(project_path).status(probe_health=probe),
            })
            return
        self._send_json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path not in ('/start', '/stop', '/restart'):
            self._send_json(404, {'ok': False, 'error': 'not found'})
            return
        body = self._read_json_body()
        if not isinstance(body, dict):
            self._send_json(400, {'ok': False, 'error': 'JSON body must be an object'})
            return
        project_path = (body.get('projectPath') or '').strip()
        if not self._check_allowed(project_path):
            return
        if path == '/start':
            result = self._manager(project_path).start(
                server_args=body.get('serverArgs'),
                server_env=body.get('serverEnv'),
                source=body.get('source') or 'http',
                explicit=True,
            )
            self._send_json(200 if result.get('ok') else 409, result)
        elif path == '/restart':
            result = self._manager(project_path).restart(
                server_args=body.get('serverArgs'),
                server_env=body.get('serverEnv'),
                source=body.get('source') or 'http',
            )
            self._send_json(200 if result.get('ok') else 409, result)
        else:
            result = self._manager(project_path).stop(
                source=body.get('source') or 'http')
            self._send_json(200 if result.get('ok') else 409, result)


class ManagerHTTPServer(ThreadingHTTPServer):
    """HTTP adapter that also owns the project monitor threads."""

    daemon_threads = True

    def server_close(self):
        for manager in getattr(self, 'managers', {}).values():
            manager.close()
        super().server_close()


def build_server():
    """Construct the ThreadingHTTPServer with config resolved from the env."""
    host = os.environ.get(ENV_HOST, '127.0.0.1')
    try:
        port = int(os.environ.get(ENV_PORT, DEFAULT_PORT))
    except (ValueError, TypeError):
        port = DEFAULT_PORT
    configured = os.environ.get(ENV_PROJECTS, '')
    # Project-local zero-config default. An explicit value still keeps the
    # strict exact-realpath allow-list used by remote Android control.
    allowlist = parse_allowlist(configured)
    if not configured:
        allowlist = {os.path.realpath(os.path.dirname(__file__))}

    # Bind before constructing managers. A second supervisor therefore fails
    # without touching desired state or racing the active owner.
    httpd = ManagerHTTPServer((host, port), SupervisorHandler)
    httpd.allowlist = allowlist
    httpd.managers = {
        project: LifecycleManager(project, os.environ.get(ENV_PYTHON) or sys.executable)
        for project in allowlist if is_allowed(project, allowlist)
    }
    for manager in httpd.managers.values():
        manager.start_monitor()

    if not allowlist:
        logger.warning('%s is empty — no project is startable/stoppable until '
                       'you allow-list one.', ENV_PROJECTS)
    else:
        logger.info('Allow-listed projects: %s', ', '.join(sorted(allowlist)))
    logger.info('Supervisor v%s listening on %s:%s', SUPERVISOR_VERSION, host, port)
    return httpd


def main():
    httpd = build_server()

    def _shutdown(signum, _frame):
        logger.info('Received signal %s — shutting down supervisor.', signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        logger.info('Supervisor stopped.')


if __name__ == '__main__':
    main()

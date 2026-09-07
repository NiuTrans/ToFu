"""Tests for supervisor.py — the remote start/stop daemon for Tofu projects.

Covers the pure logic (allow-list, status parsing, idempotency, auth) and a
live HTTP round-trip against a fake project dir containing stub server.py /
stop.sh scripts, so no real Tofu server is spawned.

Run: pytest -p no:napari tests/test_supervisor.py
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import shutil

import pytest

import server_manager
import supervisor
import supervisor_protocol


pytestmark = pytest.mark.unit


# ── fixtures ──────────────────────────────────────────────────────────

def _make_project(tmp_path, with_scripts=True):
    """Create a fake project dir with stub server.py / stop.sh + data/."""
    proj = tmp_path / 'proj'
    proj.mkdir()
    (proj / 'data').mkdir()
    if with_scripts:
        # A stub server.py that just sleeps; a stub stop.sh that exits 0.
        (proj / 'server.py').write_text('import time; time.sleep(300)\n')
        (proj / 'stop.sh').write_text('#!/usr/bin/env bash\necho stopped\nexit 0\n')
        os.chmod(proj / 'stop.sh', 0o755)
    return proj


def _write_lock(proj, pid, host='thishost'):
    (proj / 'data' / '.server.lock').write_text(f'{pid}@{host}\n')


# ── allow-list ────────────────────────────────────────────────────────

def test_parse_allowlist_normalizes_and_dedupes(tmp_path):
    a = str(tmp_path / 'a')
    raw = f'{a}{os.pathsep}{a}{os.pathsep}  {os.pathsep}'
    result = supervisor.parse_allowlist(raw)
    assert result == {os.path.realpath(a)}


def test_parse_allowlist_empty():
    assert supervisor.parse_allowlist('') == set()
    assert supervisor.parse_allowlist(None) == set()


def test_parse_owner_pid_is_explicit_and_validated():
    assert supervisor.parse_owner_pid(None) is None
    assert supervisor.parse_owner_pid('') is None
    assert supervisor.parse_owner_pid(' 42 ') == 42
    for invalid in ('not-a-pid', '0', '-1'):
        with pytest.raises(ValueError, match='positive PID'):
            supervisor.parse_owner_pid(invalid)


def test_supervisor_source_fingerprint_changes_with_dependency_content(tmp_path):
    for relative_name in supervisor_protocol.SUPERVISOR_SOURCE_FILES:
        (tmp_path / relative_name).write_text(relative_name, encoding='utf-8')
    before = supervisor_protocol.supervisor_source_fingerprint(str(tmp_path))
    (tmp_path / 'runtime_guards.py').write_text('new policy', encoding='utf-8')
    after = supervisor_protocol.supervisor_source_fingerprint(str(tmp_path))
    assert len(before) == 64
    assert before != after


def test_supervisor_generation_match_requires_loaded_source_root(tmp_path):
    for relative_name in supervisor_protocol.SUPERVISOR_SOURCE_FILES:
        (tmp_path / relative_name).write_text(relative_name, encoding='utf-8')
    project = os.path.realpath(str(tmp_path))
    fingerprint = supervisor_protocol.supervisor_source_fingerprint(project)
    health = {
        'ok': True,
        'projects': [project],
        'sourceProjectPath': project,
        'sourceFingerprint': fingerprint,
    }
    assert supervisor_protocol.supervisor_generation_matches(health, project)
    health['sourceFingerprint'] = '0' * 64
    assert not supervisor_protocol.supervisor_generation_matches(health, project)


def test_deferred_restart_is_deduplicated_and_runs_after_ack_window():
    restarted = threading.Event()

    class _Manager:
        def restart(self, *, source):
            assert source == 'pytest'
            restarted.set()
            return {'ok': True}

    class _Server:
        managers = {'/project': _Manager()}

    server = _Server()
    assert supervisor.ManagerHTTPServer.schedule_deferred_restart(
        server, '/project', source='pytest') is True
    assert supervisor.ManagerHTTPServer.schedule_deferred_restart(
        server, '/project', source='pytest') is False
    assert restarted.wait(timeout=2)


def test_is_allowed_requires_membership_and_scripts(tmp_path):
    proj = _make_project(tmp_path)
    allow = {os.path.realpath(str(proj))}
    assert supervisor.is_allowed(str(proj), allow) is True
    # Not in allow-list.
    assert supervisor.is_allowed(str(proj), set()) is False
    # Empty path.
    assert supervisor.is_allowed('', allow) is False


def test_is_allowed_rejects_traversal(tmp_path):
    """A '..' path that canonicalises OUTSIDE the allow-list is rejected."""
    proj = _make_project(tmp_path)
    allow = {os.path.realpath(str(proj))}
    sneaky = str(proj / '..' / 'proj' / '..' / 'other')
    assert supervisor.is_allowed(sneaky, allow) is False


def test_is_allowed_rejects_dir_without_scripts(tmp_path):
    proj = _make_project(tmp_path, with_scripts=False)
    allow = {os.path.realpath(str(proj))}
    # In allow-list but missing server.py/stop.sh → not runnable.
    assert supervisor.is_allowed(str(proj), allow) is False


# ── status ────────────────────────────────────────────────────────────

def test_read_status_no_lock(tmp_path):
    proj = _make_project(tmp_path)
    st = supervisor.read_status(str(proj))
    assert st['running'] is False
    assert st['lockPresent'] is False
    assert st['pid'] is None


def test_read_status_stale_lock_dead_pid(tmp_path):
    proj = _make_project(tmp_path)
    # A pid that is (almost certainly) not alive.
    _write_lock(proj, 999999)
    st = supervisor.read_status(str(proj))
    assert st['lockPresent'] is True
    assert st['running'] is False
    assert st['stale'] is True


def test_read_status_malformed_lock(tmp_path):
    proj = _make_project(tmp_path)
    (proj / 'data' / '.server.lock').write_text('garbage-no-at-sign\n')
    st = supervisor.read_status(str(proj))
    assert st['running'] is False
    assert st['stale'] is True


def test_read_status_running(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    _write_lock(proj, 4242)
    # Force the liveness probe to say "yes, that's a live server.py".
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: pid == 4242)
    st = supervisor.read_status(str(proj))
    assert st['running'] is True
    assert st['pid'] == 4242


# ── start idempotency ─────────────────────────────────────────────────

def test_do_start_idempotent_when_running(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    _write_lock(proj, 4242)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: True)
    spawned = []
    monkeypatch.setattr(supervisor.subprocess, 'Popen',
                        lambda *a, **k: spawned.append(a) or pytest.fail('spawned'))
    res = supervisor.do_start(str(proj))
    assert res['ok'] is True
    assert res['alreadyRunning'] is True
    assert spawned == []  # no second process


def test_do_start_spawns_when_stopped(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)

    class FakeProc:
        pid = 7777

    calls = {}

    def fake_popen(cmd, **kw):
        calls['cmd'] = cmd
        calls['cwd'] = kw.get('cwd')
        calls['new_session'] = kw.get('start_new_session')
        return FakeProc()

    monkeypatch.setattr(supervisor.subprocess, 'Popen', fake_popen)
    res = supervisor.do_start(str(proj))
    assert res['ok'] is True
    assert res['alreadyRunning'] is False
    assert res['launcherPid'] == 7777
    assert calls['cmd'][1] == 'server.py'
    assert calls['cwd'] == os.path.realpath(str(proj))
    assert calls['new_session'] is True


# ── stop ──────────────────────────────────────────────────────────────

def test_do_stop_runs_stop_sh(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    res = supervisor.do_stop(str(proj))
    # stub stop.sh exits 0 → ok.
    assert res['ok'] is True
    assert res['exitCode'] == 0


def test_do_stop_missing_script(tmp_path):
    proj = _make_project(tmp_path, with_scripts=False)
    res = supervisor.do_stop(str(proj))
    assert res['ok'] is False
    assert 'stop.sh not found' in res['message']


def test_do_stop_refused_exit1(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    (proj / 'stop.sh').write_text('#!/usr/bin/env bash\nexit 1\n')
    os.chmod(proj / 'stop.sh', 0o755)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    res = supervisor.do_stop(str(proj))
    assert res['ok'] is False   # exit 1 = refused
    assert res['exitCode'] == 1


# ── auth helpers ──────────────────────────────────────────────────────

# ── live HTTP round-trip ──────────────────────────────────────────────

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """Start the real ThreadingHTTPServer on an ephemeral port (no auth)."""
    proj = _make_project(tmp_path)
    canon = os.path.realpath(str(proj))
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')      # ephemeral
    monkeypatch.setenv(supervisor.ENV_PROJECTS, canon)
    monkeypatch.setattr(
        server_manager,
        'run_frontend_preflight',
        lambda *_args, **_kwargs: (True, ''),
    )
    # The project manager's worker port must be isolated from the developer's
    # real :15000 server; the stub never binds it, but conflict detection is
    # deliberately strict before spawn.
    with socket.socket() as worker_socket:
        worker_socket.bind(('127.0.0.1', 0))
        monkeypatch.setenv('PORT', str(worker_socket.getsockname()[1]))
    # Never actually spawn server.py in the HTTP test.
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    httpd = supervisor.build_server()
    host, port = httpd.server_address
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {'base': f'http://{host}:{port}', 'proj': canon}
    for manager in httpd.managers.values():
        manager.stop(source='test-cleanup')
    httpd.shutdown()
    httpd.server_close()


def _req(url, method='GET', token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_health_no_auth(live_server):
    status, body = _req(live_server['base'] + '/health')
    assert status == 200
    assert body['ok'] is True
    assert body['version'] == supervisor_protocol.SUPERVISOR_VERSION
    assert body['protocolVersion'] \
        == supervisor_protocol.SUPERVISOR_PROTOCOL_VERSION
    assert len(body['sourceFingerprint']) == 64
    assert body['sourceProjectPath'] == os.path.realpath(
        os.path.dirname(supervisor.__file__))


@pytest.mark.serial
def test_real_supervisor_reexec_loads_new_generation_with_same_pid(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project = tmp_path / 'reload-project'
    project.mkdir()
    (project / 'data').mkdir()
    (project / 'logs').mkdir()
    for name in supervisor_protocol.SUPERVISOR_SOURCE_FILES:
        shutil.copy(os.path.join(root, name), project / name)
    (project / 'server.py').write_text(
        'import time; time.sleep(300)\n', encoding='utf-8')
    (project / 'stop.sh').write_text(
        '#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    os.chmod(project / 'stop.sh', 0o755)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = int(probe.getsockname()[1])
    env = {
        **os.environ,
        'TOFU_SUPERVISOR_HOST': '127.0.0.1',
        'TOFU_SUPERVISOR_PORT': str(port),
        'TOFU_SUPERVISOR_PROJECTS': str(project),
        'TOFU_SUPERVISOR_PYTHON': sys.executable,
    }
    process = subprocess.Popen(
        [sys.executable, 'supervisor.py'],
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    base = f'http://127.0.0.1:{port}'
    try:
        before = None
        for _ in range(100):
            try:
                status, before = _req(base + '/health')
            except OSError:
                time.sleep(0.05)
                continue
            if status == 200:
                break
        assert before and before['managerPid'] == process.pid

        policy = project / 'runtime_guards.py'
        policy.write_text(
            policy.read_text(encoding='utf-8') + '\n# next generation\n',
            encoding='utf-8')
        expected = supervisor_protocol.supervisor_source_fingerprint(
            str(project))
        status, accepted = _req(
            base + '/reload',
            method='POST',
            body={
                'projectPath': str(project),
                'expectedFingerprint': expected,
                'source': 'pytest',
            },
        )
        assert status == 202
        assert accepted['reloading'] is True

        after = None
        for _ in range(160):
            try:
                status, candidate = _req(base + '/health')
            except OSError:
                time.sleep(0.05)
                continue
            if status == 200 and candidate.get('sourceFingerprint') == expected:
                after = candidate
                break
            time.sleep(0.05)
        assert after is not None
        assert after['managerPid'] == process.pid
        assert after['startedAt'] >= before['startedAt']
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait(timeout=5)


def test_http_status_no_auth(live_server):
    # Personal app: no token anywhere. /status just works.
    url = live_server['base'] + '/status?projectPath=' + live_server['proj']
    status, body = _req(url)
    assert status == 200
    assert body['ok'] is True
    assert body['running'] is False


def test_http_status_enforces_allowlist(live_server):
    # The allow-list is the ONLY guard kept (config, not auth).
    status, body = _req(live_server['base'] + '/status?projectPath=/etc')
    assert status == 403


def test_http_start_no_auth_works(live_server):
    # start needs no token now; but stub server.py never binds so it reports
    # not-yet-running — the call itself must be accepted (200, ok).
    status, body = _req(live_server['base'] + '/start', method='POST',
                        body={'projectPath': live_server['proj']})
    assert status == 200
    assert body['ok'] is True


def test_http_server_retires_when_checkout_ownership_disappears(
        tmp_path, monkeypatch):
    """A detached manager cannot outlive its removed source checkout."""
    proj = _make_project(tmp_path)
    canon = os.path.realpath(str(proj))
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')
    monkeypatch.setenv(supervisor.ENV_PROJECTS, canon)
    httpd = supervisor.build_server()
    sentinel = tmp_path / 'manager-owner.py'
    sentinel.write_text('# owner\n')
    httpd.ownership_sentinel = str(sentinel)
    try:
        httpd.service_actions()
        sentinel.unlink()
        with pytest.raises(
                supervisor.SupervisorOwnershipLost,
                match='ownership sentinel disappeared'):
            httpd.service_actions()
    finally:
        httpd.server_close()


def test_http_server_retires_when_ephemeral_owner_disappears(
        tmp_path, monkeypatch):
    """Opt-in test/dev supervisors cannot survive their declared owner."""
    proj = _make_project(tmp_path)
    canon = os.path.realpath(str(proj))
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')
    monkeypatch.setenv(supervisor.ENV_PROJECTS, canon)
    monkeypatch.setenv(supervisor.ENV_OWNER_PID, '424242')
    httpd = supervisor.build_server()
    monkeypatch.setattr(supervisor, 'pid_is_alive', lambda _pid: False)
    try:
        assert httpd.ownership_pid == 424242
        with pytest.raises(
                supervisor.SupervisorOwnershipLost,
                match='owner process disappeared'):
            httpd.service_actions()
        assert httpd._ownership_retired is True
    finally:
        httpd.server_close()


def test_http_start_rejects_unlisted_path(live_server):
    status, body = _req(live_server['base'] + '/start', method='POST',
                        body={'projectPath': '/etc'})
    assert status == 403


def test_http_stop_roundtrip(live_server):
    status, body = _req(live_server['base'] + '/stop', method='POST',
                        body={'projectPath': live_server['proj']})
    assert status == 200
    assert body['ok'] is True   # stub stop.sh exits 0


# ── supervisor.sh setsid + watchdog + PID-file launch path ────────────
#
# The no-systemd fallback must be a REAL durable root: fully detached from the
# launching terminal (its own session leader, so a terminal-close SIGHUP can't
# reach it) + a PID file so status/stop can manage it. These drive the actual
# supervisor.sh via subprocess against a stub supervisor.py that just sleeps.

import signal as _signal

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_SUPERVISOR_SH = os.path.join(_REPO_ROOT, 'supervisor.sh')

_needs_bash_setsid = pytest.mark.skipif(
    shutil.which('bash') is None or shutil.which('setsid') is None,
    reason='requires bash + setsid (util-linux)',
)


def _make_sh_project(tmp_path, neuter_setsid=False):
    """Copy supervisor.sh + a sleeping stub supervisor.py into a temp dir.

    When *neuter_setsid* is True, rewrite the script to drop ``setsid`` from
    the launch — the reverted/old behaviour — so we can prove setsid is what
    provides session detachment.
    """
    proj = tmp_path / 'shproj'
    proj.mkdir()
    (proj / 'data').mkdir()
    src = open(_REAL_SUPERVISOR_SH).read()
    if neuter_setsid:
        # Force the non-setsid path: make `command -v setsid` fail AND turn the
        # setsid launch into a bare background job in the caller's session.
        src = src.replace('setsid bash "$BASE_DIR/supervisor.sh"',
                          'bash "$BASE_DIR/supervisor.sh"')
    (proj / 'supervisor.sh').write_text(src)
    os.chmod(proj / 'supervisor.sh', 0o755)
    # Stub daemon target: stay live like supervisor.py, while honoring the
    # same optional owner boundary so fixture teardown is self-cleaning even
    # when an assertion bypasses the explicit shell stop.
    stub = (
        'import os, time\n'
        "owner = int(os.environ.get('TOFU_SUPERVISOR_OWNER_PID', '0'))\n"
        'deadline = time.monotonic() + 300\n'
        'while time.monotonic() < deadline:\n'
        '    if owner:\n'
        '        try:\n'
        '            os.kill(owner, 0)\n'
        '        except OSError:\n'
        '            break\n'
        '    time.sleep(0.05)\n'
    )
    (proj / 'supervisor.py').write_text(stub)
    return proj


def _read_pid(proj):
    pf = proj / 'data' / 'supervisor.pid'
    if not pf.is_file():
        return None
    raw = pf.read_text().strip()
    return int(raw) if raw.isdigit() else None


def _kill_group(pid):
    if not pid:
        return
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(0.3)


@pytest.fixture()
def supervisor_owner_lease():
    """Per-test PID lease that also disappears on pytest worker death."""
    process = subprocess.Popen(
        [sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield str(process.pid)
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


@_needs_bash_setsid
@pytest.mark.serial
def test_daemon_watchdog_retires_after_explicit_owner_exits(tmp_path):
    """The self-healing shell must not restart an owner-retired manager."""
    proj = _make_sh_project(tmp_path)
    sh = str(proj / 'supervisor.sh')
    owner = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(300)'])
    env = dict(
        os.environ,
        TOFU_SUPERVISOR_PROJECTS=str(proj),
        TOFU_SUPERVISOR_OWNER_PID=str(owner.pid),
    )
    watchdog_pid = None
    try:
        launched = subprocess.run(
            ['bash', sh, 'daemon'], env=env, capture_output=True, text=True,
            timeout=30)
        assert launched.returncode == 0, launched.stderr
        watchdog_pid = _read_pid(proj)
        assert watchdog_pid is not None

        owner.terminate()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5.0
        while (server_manager.pid_is_alive(watchdog_pid)
               and time.monotonic() < deadline):
            time.sleep(0.05)
        assert not server_manager.pid_is_alive(watchdog_pid)
        assert not (proj / 'data' / 'supervisor.pid').exists()
        watchdog_pid = None
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        subprocess.run(
            ['bash', sh, 'stop'], env=env, capture_output=True, text=True,
            timeout=20)
        _kill_group(watchdog_pid)


@_needs_bash_setsid
@pytest.mark.serial
def test_daemon_launches_detached_session_leader_with_pidfile(
        tmp_path, supervisor_owner_lease):
    proj = _make_sh_project(tmp_path)
    sh = str(proj / 'supervisor.sh')
    env = dict(
        os.environ,
        TOFU_SUPERVISOR_PROJECTS=str(proj),
        TOFU_SUPERVISOR_OWNER_PID=supervisor_owner_lease,
    )
    pid = None
    inherited_read_fd, inherited_write_fd = os.pipe()
    os.set_inheritable(inherited_read_fd, True)
    inherited_target = os.readlink(f'/proc/self/fd/{inherited_read_fd}')
    try:
        res = subprocess.run(['bash', sh, 'daemon'], env=env,
                             capture_output=True, text=True, timeout=30,
                             pass_fds=(inherited_read_fd,))
        assert res.returncode == 0, res.stderr
        pid = _read_pid(proj)
        assert pid is not None, f'no PID file written; stdout={res.stdout}'
        # The watchdog is alive…
        os.kill(pid, 0)
        # …and is its OWN session leader → fully detached from our session.
        # This is exactly what setsid buys us; a terminal-close SIGHUP to the
        # launcher's session can never reach it.
        assert os.getsid(pid) == pid, 'watchdog is not a detached session leader'
        watchdog_fd_targets = {
            os.readlink(entry.path)
            for entry in os.scandir(f'/proc/{pid}/fd')
        }
        assert inherited_target not in watchdog_fd_targets, (
            'detached watchdog retained a caller-owned control descriptor')

        # status reports it running.
        st = subprocess.run(['bash', sh, 'status'], env=env,
                            capture_output=True, text=True, timeout=15)
        assert 'RUNNING' in st.stdout

        # stop tears it down and clears the PID file.
        sp = subprocess.run(['bash', sh, 'stop'], env=env,
                            capture_output=True, text=True, timeout=30)
        assert sp.returncode == 0, sp.stderr
        time.sleep(0.5)
        assert not (proj / 'data' / 'supervisor.pid').is_file()
        # process group gone.
        with pytest.raises(OSError):
            os.kill(pid, 0)
        pid = None
    finally:
        os.close(inherited_read_fd)
        os.close(inherited_write_fd)
        _kill_group(pid)


@_needs_bash_setsid
@pytest.mark.serial
def test_setsid_is_load_bearing_neuter(tmp_path, supervisor_owner_lease):
    """NEUTER: strip setsid → the watchdog is NOT its own session leader.

    Proves the detachment assertion above is real: with setsid removed (the
    old bare-background behaviour), the watchdog inherits the launcher's
    session and a terminal SIGHUP could take it down.
    """
    proj = _make_sh_project(tmp_path, neuter_setsid=True)
    sh = str(proj / 'supervisor.sh')
    env = dict(
        os.environ,
        TOFU_SUPERVISOR_PROJECTS=str(proj),
        TOFU_SUPERVISOR_OWNER_PID=supervisor_owner_lease,
    )
    pid = None
    try:
        subprocess.run(['bash', sh, 'daemon'], env=env,
                       capture_output=True, text=True, timeout=30)
        pid = _read_pid(proj)
        assert pid is not None
        os.kill(pid, 0)
        # Without setsid the watchdog is NOT a session leader.
        assert os.getsid(pid) != pid, (
            'expected non-detached watchdog when setsid is stripped — '
            'the detachment test would give a false pass'
        )
    finally:
        _kill_group(pid)



def _run_watchdog_with_fastfail(
        tmp_path, owner_pid, neuter_backoff=False):
    """Run cmd_watchdog in the FOREGROUND against a stub supervisor.py that
    exits immediately (simulating a persistent port-in-use fast-fail), for a
    bounded window. Each launch appends a line to launches.log. Returns the
    number of launches observed in a 15s window.

    When *neuter_backoff* is True, strip the escalation so the loop restarts at
    the fixed 2s base every time — the storm the backoff is meant to prevent.
    """
    proj = tmp_path / ('wd_neuter' if neuter_backoff else 'wd')
    proj.mkdir()
    (proj / 'data').mkdir()
    src = open(_REAL_SUPERVISOR_SH).read()
    if neuter_backoff:
        # Defeat escalation: never double the backoff (constant 2s base).
        src = src.replace('_backoff=$(( _backoff * 2 ))', '_backoff="$_backoff"')
    (proj / 'supervisor.sh').write_text(src)
    os.chmod(proj / 'supervisor.sh', 0o755)
    launches = proj / 'launches.log'
    # Stub exits instantly (fast-fail) and records each start.
    (proj / 'supervisor.py').write_text(
        f"open({str(launches)!r}, 'a').write('x\\n')\n"
        "import sys; sys.exit(1)\n"
    )
    env = dict(
        os.environ,
        TOFU_SUPERVISOR_PROJECTS=str(proj),
        TOFU_SUPERVISOR_OWNER_PID=owner_pid,
    )
    # Call the internal watchdog entrypoint directly, in the foreground.
    # start_new_session → its own process group, so killpg below targets ONLY
    # the watchdog + its children, never the pytest process group.
    p = subprocess.Popen(['bash', str(proj / 'supervisor.sh'), '__watchdog__'],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
    try:
        # Leave enough scheduler slack for a loaded shared CI host. At 11s the
        # fixed-2s neuter intermittently produced only four launches because
        # process startup/logging overhead consumed >3s; 15s still keeps the
        # exponential path at <=4 launches (t≈0,2,6,14) while the fixed path
        # has ample room to cross the >=5 proof threshold.
        time.sleep(15)
    finally:
        try:
            os.killpg(os.getpgid(p.pid), _signal.SIGTERM)
        except OSError:
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    n = 0
    if launches.is_file():
        n = sum(1 for _ in open(launches))
    return n


@_needs_bash_setsid
def test_watchdog_backs_off_on_persistent_fast_fail(
        tmp_path, supervisor_owner_lease):
    """A supervisor.py that dies immediately (e.g. port 15001 in use) must NOT
    trigger a 2s restart storm — escalating backoff caps the attempt rate."""
    n = _run_watchdog_with_fastfail(
        tmp_path, supervisor_owner_lease, neuter_backoff=False)
    # Over ~15s, escalating backoff (sleep 2,4,8,…) launches at t≈0,2,6,14
    # → at most 4. Fixed-2s has time for at least 5 even with scheduler load.
    assert 1 <= n <= 4, f'expected escalating backoff to cap launches (got {n})'


@_needs_bash_setsid
def test_backoff_escalation_is_load_bearing_neuter(
        tmp_path, supervisor_owner_lease):
    """NEUTER: remove the *=2 escalation → constant 2s restarts → more launches
    in the same window. Proves the backoff assertion above is real."""
    n_fixed = _run_watchdog_with_fastfail(
        tmp_path, supervisor_owner_lease, neuter_backoff=True)
    # Fixed 2s over ~15s → launches at t≈0,2,4,…14. Must clearly exceed
    # the escalating case's cap (≤4), proving the escalation is load-bearing.
    assert n_fixed >= 5, (
        f'without escalation the loop should restart ~every 2s (got {n_fixed}) — '
        'if this is low, the backoff test would give a false pass'
    )

"""tests/test_lifecycle_hardening.py — guards for the 2026-08-03 lifecycle
incident fixes (epic pt_6f066c2ae2d64066).

Incident chain (every hop log-verified in logs/watchdog.log + app.log):

  A. stop.sh killed only the lock-recorded pid and never told the watchdog —
     deploy/tofu_guard.sh relaunched a NEW instance 9s after the SIGKILL,
     which took the instance lock; the operator's manual start then refused
     ("instance lock held by a LIVE local server", pid=240685).

  B/C. The guard's relaunch ran with a marker-less (cron-loop) environment,
     so server.py's _detect_reverse_proxy found nothing and the new instance
     came up TLS with a self-signed cert — the plain-HTTP proxy/curl hit a
     TLS socket = "socket hang up" (the exact phrase in server.py's comment),
     and the guard's own http-only healthy() misreported the READY TLS
     server as dead for 60s.

  D. The first cause: the OLD instance's event loop froze 04:40→11:14 while
     its listener stayed BOUND — the guard saw "listener present" and kept
     silent for 6.5h. The only observable wedge signal was the stale
     loop-heartbeat sidecar. A listener-liveness check alone would NOT have
     caught it; heartbeat arbitration is the first-cause fix, with a
     server-side serve-death self-kill as the second layer.

  E + probe. restart_15000.sh truncated server_15000.log on every relaunch
     (destroying prior-life forensics), and its [5/5](b) probe greps that
     stdout stream for the INFO-level "Loop blocking-guard" line — measured
     0 INFO lines vs 990 WARNING+ lines in the stdout log, so (b) can never
     pass and false-FATALs every healthy boot.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_SH = os.path.join(ROOT, 'stop.sh')
GUARD_SH = os.path.join(ROOT, 'deploy', 'tofu_guard.sh')
RESTART_SH = os.path.join(ROOT, 'restart_15000.sh')
SERVER_PY = os.path.join(ROOT, 'server.py')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _bash(script: str, env: dict | None = None, cwd: str | None = None,
          timeout: int = 60) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                          env=full_env, cwd=cwd, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────
#  A. stop.sh must interlock with the watchdog
# ─────────────────────────────────────────────────────────────────────

def test_stop_sh_disables_watchdog_e2e(tmp_path):
    """REAL stop.sh run (copied into a temp project) against a live dummy
    'server.py': it must kill the dummy AND leave data/.tofu_guard_disabled
    behind with a loud re-enable hint — the exact interlock that was missing
    when the watchdog relaunched 9s after the SIGKILL."""
    proj = tmp_path / 'proj'
    (proj / 'data').mkdir(parents=True)
    shutil.copy(STOP_SH, proj / 'stop.sh')
    os.chmod(proj / 'stop.sh', 0o755)

    dummy = subprocess.Popen(['bash', '-c', 'exec -a "python server.py" sleep 120'])
    try:
        host = socket.gethostname()
        (proj / 'data' / '.server.lock').write_text(f'{dummy.pid}@{host}\n')

        proc = subprocess.run(['bash', 'stop.sh'], cwd=proj, capture_output=True,
                              text=True, timeout=30)
        dummy.wait(timeout=10)

        assert proc.returncode == 0, \
            f'stop.sh should stop the dummy cleanly: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}'
        assert (proj / 'data' / '.tofu_guard_disabled').exists(), \
            'no .tofu_guard_disabled flag — the watchdog would relaunch a ' \
            'fresh instance within ~15s and take the instance lock, exactly ' \
            'the 2026-08-03 11:15:19 race'
        assert 'tofu_guard.sh --start' in proc.stdout, \
            'the operator was not told how to re-enable the watchdog'
    finally:
        if dummy.poll() is None:
            dummy.kill()
            dummy.wait(timeout=5)


def test_stop_sh_interlock_is_sourced_before_the_kill():
    """The flag must be touched BEFORE the SIGTERM — touching it after a
    successful kill would still lose the race if the script dies mid-way."""
    src = _read(STOP_SH)
    assert '.tofu_guard_disabled' in src, \
        'stop.sh never touches the watchdog disable flag'
    assert src.index('.tofu_guard_disabled') < src.index('kill "$pid"'), \
        'the watchdog flag is touched after the SIGTERM — the guard can ' \
        'still relaunch in the kill window'


# ─────────────────────────────────────────────────────────────────────
#  B/C. serve-mode record + replay + protocol-aware health probe
# ─────────────────────────────────────────────────────────────────────

_SOURCE_GUARD = ('export TOFU_GUARD_SOURCE_ONLY=1; '
                 f'source {shlex.quote(GUARD_SH)}; ')


def test_guard_serve_mode_reads_recorded_decision(tmp_path):
    mode_file = tmp_path / 'mode'
    for content, expect in [('https\n', 'https'), ('http\n', 'http')]:
        mode_file.write_text(content)
        proc = _bash(_SOURCE_GUARD + '_serve_mode',
                     env={'TOFU_SERVE_MODE_FILE': str(mode_file)})
        assert proc.stdout.strip() == expect, \
            f'_serve_mode read {content!r} as {proc.stdout.strip()!r}'
    # Absent / garbage → EMPTY (never invent a decision for the relaunch to
    # replay — a wrong replay is worse than none).
    proc = _bash(_SOURCE_GUARD + '_serve_mode',
                 env={'TOFU_SERVE_MODE_FILE': str(tmp_path / 'missing')})
    assert proc.stdout.strip() == '', 'missing mode file must yield an empty mode'
    mode_file.write_text('ftp-tls\n')
    proc = _bash(_SOURCE_GUARD + '_serve_mode',
                 env={'TOFU_SERVE_MODE_FILE': str(mode_file)})
    assert proc.stdout.strip() == '', 'garbage mode file must yield an empty mode'


def test_guard_tls_env_mapping():
    proc = _bash(_SOURCE_GUARD + '_guard_tls_env https; echo; '
                                 '_guard_tls_env http; echo; '
                                 '_guard_tls_env ""; echo END')
    lines = proc.stdout.splitlines()
    assert lines[0] == 'TOFU_TLS=1', 'https mode must force TLS on the relaunch'
    assert lines[1] == 'TOFU_TLS=0', 'http mode must force NO-TLS on the relaunch'
    assert lines[2] == 'END', \
        'unknown mode must NOT force anything (auto-detect stays in charge)'


def test_guard_healthy_probes_recorded_protocol_first(tmp_path):
    """Against a TLS instance an http-only probe reports a READY server as
    dead (the guard's 11:16:27 false alarm). healthy() must try the recorded
    scheme FIRST, then fall back to the other."""
    log = tmp_path / 'curl_args.log'
    stub = tmp_path / 'curl_stub.sh'
    stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    os.chmod(stub, 0o755)
    mode_file = tmp_path / 'mode'

    mode_file.write_text('https\n')
    _bash(_SOURCE_GUARD + 'healthy', env={'TOFU_SERVE_MODE_FILE': str(mode_file),
                                          'TOFU_GUARD_CURL': str(stub)})
    first = log.read_text().splitlines()[0]
    assert 'https://' in first and '-k' in first, \
        f'https mode must probe https first (got: {first})'

    log.write_text('')
    mode_file.write_text('http\n')
    _bash(_SOURCE_GUARD + 'healthy', env={'TOFU_SERVE_MODE_FILE': str(mode_file),
                                          'TOFU_GUARD_CURL': str(stub)})
    first = log.read_text().splitlines()[0]
    assert first.startswith('-s ') or ' http://' in first, \
        f'http mode must probe http first (got: {first})'
    assert 'https://' not in first, 'http mode probed https first'


def test_server_records_serve_mode_after_tls_decision(tmp_path):
    import server
    p = tmp_path / '.last_serve_mode'
    server._record_serve_mode('https', path=str(p))
    assert p.read_text().strip() == 'https'
    server._record_serve_mode('http', path=str(p))
    assert p.read_text().strip() == 'http'
    with pytest.raises(ValueError):
        server._record_serve_mode('ftp', path=str(p))

    src = _read(SERVER_PY)
    decide = src.index('_ensure_tls_certs(args.certfile')
    record = src.index("_record_serve_mode('https' if")
    assert record > decide, \
        'the mode must be recorded AFTER the TLS decision, not before it'


# ─────────────────────────────────────────────────────────────────────
#  D. heartbeat-wedge relaunch (first cause) + serve-death self-kill
# ─────────────────────────────────────────────────────────────────────

_HOLDER_PY = (
    'import fcntl, os, socket, sys, time\n'
    'lock = sys.argv[1]\n'
    'fd = os.open(lock, os.O_RDWR)\n'
    'fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
    'open(lock, "w").write("%d@%s\\n" % (os.getpid(), socket.gethostname()))\n'
    'print("held", flush=True)\n'
    'time.sleep(60)\n'
)


def _start_lock_holder(lock_path):
    """A live process flocking *lock_path*, stamped '<pid>@<host>' inside it,
    whose /proc cmdline contains 'server.py' (the guard's liveness checks)."""
    proc = subprocess.Popen(
        ['bash', '-c',
         f'exec -a "python server.py" {shlex.quote(sys.executable)} -c '
         f'{shlex.quote(_HOLDER_PY)} {shlex.quote(str(lock_path))}'],
        stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == 'held', 'lock holder failed to start'
    return proc


def _guard_harness(tmp_path, listener_pid=None):
    """Copy tofu_guard.sh into a temp project (PROJ=tmp) with probe-command
    seams pointed at stubs. Returns (proj, env)."""
    proj = tmp_path / 'proj'
    (proj / 'deploy').mkdir(parents=True, exist_ok=True)
    (proj / 'data').mkdir(exist_ok=True)
    (proj / 'logs').mkdir(exist_ok=True)
    shutil.copy(GUARD_SH, proj / 'deploy' / 'tofu_guard.sh')
    # relaunch must be instant: PY=/bin/true dies immediately ("DIED during
    # startup") instead of booting a real server.
    (proj / '.tofu_env.json').write_text('{"python": "/bin/true"}\n')

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    curl = bindir / 'curl'
    curl.write_text('#!/bin/sh\nexit 1\n')          # HTTP always unhealthy
    os.chmod(curl, 0o755)
    ss = bindir / 'ss'
    if listener_pid is None:
        ss.write_text('#!/bin/sh\nexit 0\n')        # no listener rows
    else:
        # Column layout MUST mirror this host's real `ss -ltnp` (no Netid
        # column: State Recv-Q Send-Q Local Peer Process) — the guard's awk
        # matches $4 against ':PORT$'. A 'tcp ' prefix would shift $4 to
        # Send-Q and silently route the test down the no-listener branch.
        ss.write_text('#!/bin/sh\n'
                      "printf '%s\\n' 'LISTEN 0 511 127.0.0.1:15000 0.0.0.0:* "
                      f'users:((\\"python\\",pid={listener_pid},fd=21))\'\n')
    os.chmod(ss, 0o755)

    hbdir = tmp_path / 'hb'
    hbdir.mkdir()
    env = {'TOFU_GUARD_SS': str(ss), 'TOFU_GUARD_CURL': str(curl),
           'TOFU_HEARTBEAT_DIR': str(hbdir)}
    return proj, env, hbdir


def _write_heartbeat(hbdir, pid, age_s):
    (hbdir / 'server.heartbeat').write_text(
        '{"pid": %d, "ts": %.1f}\n' % (pid, time.time() - age_s))


def _run_guard_once(proj, env):
    return _bash('bash deploy/tofu_guard.sh --once', env=env, cwd=proj, timeout=90)


def test_guard_wedged_listener_is_killed_and_relaunched(tmp_path):
    """THE 6.5h freeze, replayed: listener BOUND, HTTP dead, heartbeat stale
    600s, holder alive. Pass 1 records the wedge; a 200s-old streak then
    triggers kill + relaunch."""
    lock = tmp_path / 'proj' / 'data' / '.server.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('placeholder')
    holder = _start_lock_holder(lock)
    try:
        proj, env, hbdir = _guard_harness(tmp_path, listener_pid=holder.pid)
        _write_heartbeat(hbdir, holder.pid, age_s=600)

        first = _run_guard_once(proj, env)
        assert holder.poll() is None, \
            'first wedge sighting must NOT kill — hysteresis against transient stalls'
        wedge_state = proj / 'data' / '.tofu_guard_wedge'
        assert wedge_state.exists(), 'first sighting must record the streak'

        # Age the streak past the action threshold, then pass 2.
        wedge_state.write_text(str(int(time.time()) - 200))
        _run_guard_once(proj, env)
        deadline = time.time() + 10
        while holder.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        assert holder.poll() is not None, \
            'a confirmed wedge (stale heartbeat + dead HTTP) must be KILLED — ' \
            'silence for 6.5h is the failure this fixes'
        wlog = (proj / 'logs' / 'watchdog.log').read_text()
        assert 'relaunching' in wlog, \
            f'wedged kill must be followed by a relaunch:\n{wlog}\n{first.stdout}'
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_guard_wedged_no_listener_is_killed_and_relaunched(tmp_path):
    """The 11:14 state, replayed: NO listener, lock held by a live process,
    heartbeat stale 600s. The old guard yielded here FOREVER ('boot in
    progress'); heartbeat arbitration must now kill + relaunch."""
    lock = tmp_path / 'proj' / 'data' / '.server.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('placeholder')
    holder = _start_lock_holder(lock)
    try:
        proj, env, hbdir = _guard_harness(tmp_path, listener_pid=None)
        _write_heartbeat(hbdir, holder.pid, age_s=600)

        _run_guard_once(proj, env)
        assert holder.poll() is None, 'first wedge sighting must NOT kill'
        wedge_state = proj / 'data' / '.tofu_guard_wedge'
        assert wedge_state.exists()

        wedge_state.write_text(str(int(time.time()) - 200))
        _run_guard_once(proj, env)
        deadline = time.time() + 10
        while holder.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        assert holder.poll() is not None, \
            'no-listener wedge must be killed — the old code yielded forever'
        assert 'relaunching' in (proj / 'logs' / 'watchdog.log').read_text()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_guard_fresh_heartbeat_means_busy_never_killed(tmp_path):
    """A loaded server can answer /api/health slowly. Fresh heartbeat ⇒ busy,
    not wedged: stand down, kill nothing."""
    lock = tmp_path / 'proj' / 'data' / '.server.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('placeholder')
    holder = _start_lock_holder(lock)
    try:
        proj, env, hbdir = _guard_harness(tmp_path, listener_pid=holder.pid)
        _write_heartbeat(hbdir, holder.pid, age_s=0)

        _run_guard_once(proj, env)
        assert holder.poll() is None, 'fresh heartbeat must never be killed'
        assert not (proj / 'data' / '.tofu_guard_wedge').exists(), \
            'busy server must not even open a wedge streak'
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_guard_boot_in_progress_still_yields_without_heartbeat(tmp_path):
    """No listener + live lock holder + NO heartbeat file = a boot in
    progress (the heartbeat task starts only inside _serve). Yield exactly
    like today — never kill a boot."""
    lock = tmp_path / 'proj' / 'data' / '.server.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('placeholder')
    holder = _start_lock_holder(lock)
    try:
        proj, env, _hbdir = _guard_harness(tmp_path, listener_pid=None)
        _run_guard_once(proj, env)
        assert holder.poll() is None, 'a booting server must never be killed'
        assert not (proj / 'data' / '.tofu_guard_wedge').exists()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_listener_death_decide_matrix():
    """The server-side second layer: serve-task death with a LIVE loop must
    convert into a clean (loud) process death the watchdog can handle."""
    import server
    f = server._listener_death_decide
    assert f(False, False, 0, 5) == (False, 0, False), \
        'startup window (never bound) must not fire'
    assert f(False, True, 0, 5) == (True, 0, False), 'first sight of bound arms the watch'
    assert f(True, False, 0, 5) == (True, 1, False), 'first miss only counts'
    assert f(True, False, 4, 5) == (True, 5, True), 'K consecutive misses ⇒ die'
    assert f(True, True, 3, 5) == (True, 0, False), 'recovery resets the streak'


def test_heartbeat_task_wires_listener_watch():
    src = _read(SERVER_PY)
    assert '_listener_death_decide(' in src, 'no pure decision for listener loss'
    assert '_port_bound(' in src, 'no listener probe helper'
    hb = src.index('async def _loop_heartbeat_task')
    trigger = src.index('async def _shutdown_trigger')
    region = src[hb:trigger]
    assert '_listener_death_decide(' in region, \
        'the heartbeat task does not consult the listener watch'
    assert 'os._exit' in region, \
        'a confirmed serve death must exit the process (watchdog handles deaths)'


# ─────────────────────────────────────────────────────────────────────
#  E + [5/5](b): restart script log append + boot-scoped probes
# ─────────────────────────────────────────────────────────────────────

def test_restart_log_is_appended_not_truncated():
    src = _read(RESTART_SH)
    assert re.search(r'>> "\$\{LOG\}" 2>&1', src), \
        'relaunch must APPEND to server_15000.log — truncating destroys the ' \
        'prior life\u2019s forensics (2351494\u2019s final lines were lost this way)'
    assert not re.search(r'[^>]>\s*"\$\{LOG\}"\s*2>&1', src), \
        'a truncating > "${LOG}" redirect survived'
    assert 'launched by restart_15000.sh' in src, \
        'no per-life demarcation banner in the appended log'


def test_lock_death_grep_is_boot_scoped():
    """With append-mode logs the [4/5] lock-death grep must only see THIS
    launch's lines — a stale 'instance lock held' line from a prior life
    would false-trigger the retry path."""
    src = _read(RESTART_SH)
    assert 'LOG_MARK' in src, 'no pre-launch line-count marker'
    mark = src.index('LOG_MARK')
    grep = src.index('instance lock held by a LIVE local server', mark)
    region = src[mark:grep]
    assert 'tail -n +' in region, \
        'the lock-death grep is not scoped to lines written after LOG_MARK'


def test_probe_b_reads_the_stream_that_carries_the_line():
    """[5/5](b) proved the new _serve code ran by grepping server_15000.log
    for 'Loop blocking-guard' — an INFO line that NEVER reaches the
    WARNING+-only stdout stream (measured 0 INFO vs 990 WARNING+ lines). It
    must read logs/app.log, boot-scoped (app.log is append-only)."""
    src = _read(RESTART_SH)
    region = src[src.index('guard_ok=0'):src.index('(c) WINDOWED')]
    assert 'app.log' in region, '(b) still greps the stdout log'
    assert 'LAUNCH_STAMP' in region, \
        'app.log is append-only — the grep must be scoped to lines at/after launch'
    assert 'Loop blocking-guard' in region


def test_all_three_scripts_are_valid_bash():
    for script in (STOP_SH, GUARD_SH, RESTART_SH):
        proc = subprocess.run(['bash', '-n', script], capture_output=True, text=True)
        assert proc.returncode == 0, f'bash -n rejected {script}: {proc.stderr}'

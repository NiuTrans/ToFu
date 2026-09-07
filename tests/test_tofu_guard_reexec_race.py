"""tests/test_tofu_guard_reexec_race.py — guard must not race a re-exec/boot.

Epic pt_aa3cd224b3b346e7. Two production incidents drove this:

  * 2026-07-28 12:20/12:23 — the guard relaunched INTO an HTTP re-exec
    window (old process dead, new one not yet bound) and died on the
    instance lock. os.execv KEEPS the pid, so the guard's etimes check
    (d) can never see a re-exec — its process-age clock predates it.
  * 2026-07-28 14:21–14:44 — a memory-pressured boot took ~17 min
    (5.7x BOOT_GRACE=180s); the guard declared 4 false deaths, raced 4
    duplicate relaunches (all died on the instance lock) and polluted the
    crash-storm counter into two false "NOT relaunching" trips.

Fix (design: docs/modules/infra_runtime.md):
  (b1) re-exec marker data/.reexec_in_progress (written by
       routes/api_v1/update.py::_perform_server_reexec, cleared by
       server.py at boot-ready) — yield while fresh (<300s);
  (b2) instance-lock yield — no listener/HTTP but a LIVE server.py holds
       data/.server.lock → boot in progress; a DEAD recorded pid means a
       stale lock (SIGKILL / orphan fd / FUSE lag) → do NOT yield.

These tests run a COPY of the guard with PROJ auto-retargeted (the copy
lives at <tmp>/deploy/tofu_guard.sh), a fake .tofu_env.json pointing PY at
/bin/true (so a relaunch "dies during startup" instantly — rc 1, no real
server ever boots), and PORT pointed at a dead test port. The (d) pgrep
fallback is defanged to a never-match pattern for determinism (the
PRODUCTION `python server.py` would otherwise match it — same class of
hazard as the 14:21 incident, see tests/test_lifecycle_approval.py).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
GUARD_SRC = os.path.join(ROOT, 'deploy', 'tofu_guard.sh')
_TEST_PORT = 15598


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _make_guard_copy(tmpdir: str, *, neuter: str | None = None,
                     py: str = '/bin/true') -> str:
    """Copy the guard into <tmp>/deploy/ so PROJ auto-resolves to <tmp>.

    ``neuter`` strips a yield layer to prove it is load-bearing:
    'b1' → the re-exec marker check; 'b2' → the instance-lock check;
    'tls_env_inline' → restores the pre-5a5a37ce relaunch form (the
    ``${tls_env}`` variable sitting in the assignment-prefix slot, which
    bash does NOT re-parse as an assignment).
    ``py`` overrides the .tofu_env.json python (default /bin/true so a
    relaunch "dies during startup" instantly and never boots a server).
    """
    with open(GUARD_SRC, encoding='utf-8') as f:
        text = f.read()
    # determinism: the (d) young-process pgrep would match the PRODUCTION
    # server on this box — defang it in the copy (never matches anything).
    text = text.replace("pgrep -f 'python server\\.py'",
                        "pgrep -f 'tofu_guard_test_never_matches'")
    if neuter == 'b1':
        start = text.index('  # (b1) re-exec marker')
        end = text.index('  # (c) the positive proofs of life')
        text = text[:start] + text[end:]
    elif neuter == 'b2':
        start = text.index('  # (b2) boot-in-progress via the instance lock')
        end = text.index('  # (d) mid-boot:')
        text = text[:start] + text[end:]
    elif neuter == 'tls_env_inline':
        anchor = '    env ${tls_env:+"${tls_env}"} \\\n'
        assert anchor in text, (
            'neuter anchor drifted — the guard no longer carries the env(1) '
            'relaunch form this pin protects')
        text = text.replace(anchor, '    ${tls_env} \\\n')
    gdir = os.path.join(tmpdir, 'deploy')
    os.makedirs(gdir, exist_ok=True)
    gpath = os.path.join(gdir, 'tofu_guard.sh')
    with open(gpath, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(gpath, 0o755)
    # PY → /bin/true (or the caller's stub): a relaunch "dies during
    # startup" instantly (rc 1) and NEVER boots a real second server.
    with open(os.path.join(tmpdir, '.tofu_env.json'), 'w', encoding='utf-8') as f:
        json.dump({'python': py}, f)
        f.write('\n')
    os.makedirs(os.path.join(tmpdir, 'data'), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, 'logs'), exist_ok=True)
    return gpath


def _run_once(gpath: str, tmpdir: str) -> tuple:
    """Run the guard copy once on the dead test port. Returns (rc, watchdog log)."""
    env = dict(os.environ, PORT=str(_TEST_PORT))
    proc = subprocess.run(['bash', gpath, '--once'], capture_output=True,
                          text=True, timeout=90, env=env)
    wlog = ''
    try:
        with open(os.path.join(tmpdir, 'logs', 'watchdog.log'),
                  errors='replace') as f:
            wlog = f.read()
    except OSError:
        pass
    return proc.returncode, wlog + proc.stderr


def _write_marker(tmpdir: str, *, age_s: int = 0) -> str:
    marker = os.path.join(tmpdir, 'data', '.reexec_in_progress')
    with open(marker, 'w') as f:
        f.write('{"pid": 1, "ts": %d}\n' % time.time())
    if age_s:
        old = time.time() - age_s
        os.utime(marker, (old, old))
    return marker


class _LockStub:
    """A process holding a flock on <tmp>/data/.server.lock.

    argv[0] is spoofed to 'python server.py' (bash exec -a) so the guard's
    cmdline check matches, exactly like the real server's. The first line
    of the lock file carries ``<pid>@h`` — the stub's own pid (live case)
    or a dead pid (stale case).
    """

    def __init__(self, tmpdir: str, recorded_pid: int | None = None):
        self.lock_path = os.path.join(tmpdir, 'data', '.server.lock')
        # The data dir may not exist yet (the stub can run before
        # _make_guard_copy creates it) — the bash redirect would fail
        # silently into DEVNULL and the "stub" would hold nothing.
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        record = recorded_pid if recorded_pid is not None else -1
        # When recorded_pid is None the stub records ITSELF ($$).
        script = (
            'exec 9>"{lock}"; '
            'printf "%s@h\\n" {record} >&9; '
            'flock 9; '
            'exec -a "python server.py" sleep 120'
        ).format(lock=self.lock_path,
                 record='$$' if record == -1 else str(record))
        self.proc = subprocess.Popen(['bash', '-c', script],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.pid = self.proc.pid
        # wait until the flock is actually held — and FAIL FAST if it never
        # is (a dead/misbuilt stub must not degrade into a confusing
        # downstream relaunch assertion).
        held = False
        for _ in range(50):
            rc = subprocess.run(['flock', '-n', self.lock_path, '-c', 'true'],
                                capture_output=True).returncode
            if rc != 0:
                held = True
                break
            time.sleep(0.1)
        if not held:
            raise RuntimeError(
                f'lock stub failed to hold {self.lock_path} '
                f'(proc alive={self.proc.poll() is None})')

    def kill(self):
        try:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=5)
        except Exception:
            pass


@pytest.mark.skipif(_port_listening(_TEST_PORT),
                    reason=f'test port :{_TEST_PORT} occupied — unsafe to run')
class TestReexecRace(unittest.TestCase):

    def test_fresh_marker_yields(self):
        tmp = tempfile.mkdtemp()
        try:
            g = _make_guard_copy(tmp)
            _write_marker(tmp)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 0, f'expected yield (rc 0):\n{log}')
            self.assertIn('re-exec in progress', log)
            self.assertNotIn('relaunching', log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stale_marker_does_not_yield(self):
        tmp = tempfile.mkdtemp()
        try:
            g = _make_guard_copy(tmp)
            _write_marker(tmp, age_s=301)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'expected relaunch-attempt (rc 1):\n{log}')
            self.assertIn('stale re-exec marker', log)
            self.assertIn('relaunching', log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neuter_b1_marker_ignored_relaunch_fires(self):
        """NEUTER: strip the marker check — the same fresh marker no longer
        protects the window, proving (b1) is load-bearing."""
        tmp = tempfile.mkdtemp()
        try:
            g = _make_guard_copy(tmp, neuter='b1')
            _write_marker(tmp)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'neutered copy must relaunch:\n{log}')
            self.assertIn('relaunching', log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_live_lock_holder_yields(self):
        tmp = tempfile.mkdtemp()
        stub = _LockStub(tmp)
        try:
            g = _make_guard_copy(tmp)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 0, f'expected yield (rc 0):\n{log}')
            self.assertIn('instance lock held by live server.py', log)
            self.assertNotIn('relaunching', log)
        finally:
            stub.kill()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neuter_b2_live_lock_relaunch_fires(self):
        """NEUTER: strip the instance-lock check — the boot-in-progress
        window is unprotected again, proving (b2) is load-bearing."""
        tmp = tempfile.mkdtemp()
        stub = _LockStub(tmp)
        try:
            g = _make_guard_copy(tmp, neuter='b2')
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'neutered copy must relaunch:\n{log}')
            self.assertIn('relaunching', log)
        finally:
            stub.kill()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dead_recorded_pid_does_not_yield(self):
        """Stale lock (flock held by an orphan, recorded pid dead): the guard
        MUST proceed — otherwise a SIGKILLed server would never be relaunched
        (the FUSE/orphan failure mode found in design review)."""
        tmp = tempfile.mkdtemp()
        stub = _LockStub(tmp, recorded_pid=999999)
        try:
            g = _make_guard_copy(tmp)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'expected relaunch-attempt (rc 1):\n{log}')
            self.assertIn('STALE, proceeding', log)
            self.assertIn('relaunching', log)
        finally:
            stub.kill()
            shutil.rmtree(tmp, ignore_errors=True)

    def _tls_env_setup(self, tmp, neuter=None):
        """Guard copy whose launched 'python' is a stub recording TOFU_TLS.

        Returns (marker_path, slog_path). The stub writes the env var it
        ACTUALLY received — the only honest proof the optional VAR=VAL rode
        the launch line instead of dying as a would-be command name.
        """
        marker = os.path.join(tmp, 'launch_marker')
        fake_directory = os.path.join(tmp, '运行 tools')
        os.makedirs(fake_directory)
        fakepy = os.path.join(fake_directory, 'fake python.sh')
        with open(fakepy, 'w') as f:
            f.write('#!/bin/bash\n'
                    'echo "TOFU_TLS=[${TOFU_TLS:-unset}]" >> "%s"\n' % marker)
        os.chmod(fakepy, 0o755)
        g = _make_guard_copy(tmp, neuter=neuter, py=fakepy)
        with open(os.path.join(tmp, 'data', '.last_serve_mode'), 'w') as f:
            f.write('http\n')
        return g, marker, os.path.join(tmp, 'server_%d.log' % _TEST_PORT)

    def _read(self, path):
        try:
            with open(path, errors='replace') as f:
                return f.read()
        except OSError:
            return ''

    def test_relaunch_serve_mode_http_tls_env_reaches_child(self):
        """2026-08-06 outage pin: with .last_serve_mode=http the relaunch must
        deliver TOFU_TLS=0 to the child — via env(1), NOT the assignment-
        prefix slot. A variable expanded there is not re-parsed as an
        assignment: bash executed 'TOFU_TLS=0' as the COMMAND NAME, and 11
        guard relaunches died during a real OOM crash, leaving the server
        down for 22 minutes."""
        tmp = tempfile.mkdtemp()
        try:
            g, marker, slog_path = self._tls_env_setup(tmp)
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'expected relaunch-attempt (rc 1):\n{log}')
            self.assertIn('serve-mode=http TOFU_TLS=0', log)
            slog = self._read(slog_path)
            self.assertNotIn('command not found', slog)
            # the stub child may write a beat after relaunch() gives up on it
            deadline = time.time() + 5
            while not os.path.exists(marker) and time.time() < deadline:
                time.sleep(0.1)
            self.assertIn('TOFU_TLS=[0]', self._read(marker),
                          'the launched child never received TOFU_TLS=0 — '
                          'the optional env var did not ride the launch line')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_neuter_inline_tls_env_relaunch_dies(self):
        """NEUTER: restore the pre-fix inline form — the identical relaunch
        must die with 'command not found' and the child must never run,
        proving the pin above is load-bearing and not vacuously green."""
        tmp = tempfile.mkdtemp()
        try:
            g, marker, slog_path = self._tls_env_setup(tmp,
                                                       neuter='tls_env_inline')
            rc, log = _run_once(g, tmp)
            self.assertEqual(rc, 1, f'neutered copy must still attempt (rc 1):\n{log}')
            deadline = time.time() + 2
            while time.time() < deadline:
                if 'command not found' in self._read(slog_path):
                    break
                time.sleep(0.1)
            slog = self._read(slog_path)
            self.assertIn('command not found', slog,
                          'the inline ${tls_env} form did NOT die — either '
                          'bash changed its assignment-prefix rules, or the '
                          'neuter anchor silently stopped matching')
            self.assertFalse(os.path.exists(marker),
                             'the neutered launch line still exec\'d the child')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_static_anchors(self):
        """The marker write (update.py), clear (server_boot_report.py) and both guard
        layers are present in the shipped sources."""
        with open(os.path.join(ROOT, 'routes', 'api_v1', 'update.py'),
                  encoding='utf-8') as f:
            upd = f.read()
        with open(os.path.join(ROOT, 'lib', 'server_boot_report.py'),
                  encoding='utf-8') as f:
            boot_report = f.read()
        with open(GUARD_SRC, encoding='utf-8') as f:
            grd = f.read()
        self.assertIn("'.reexec_in_progress'", upd)
        self.assertIn("'.reexec_in_progress'", boot_report)
        self.assertIn('# (b1) re-exec marker', grd)
        self.assertIn('# (b2) boot-in-progress via the instance lock', grd)


def test_guard_stale_environment_marker_fails_closed(tmp_path):
    missing_python = tmp_path / 'old host' / 'missing python'
    guard = _make_guard_copy(str(tmp_path), py=str(missing_python))

    result = subprocess.run(
        ['bash', guard, '--status'],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, 'PORT': str(_TEST_PORT)},
    )

    assert result.returncode == 1
    assert '.tofu_env.json points to missing Python' in result.stderr
    assert 'Rerun install.sh after moving this checkout' in result.stderr


if __name__ == '__main__':
    unittest.main()

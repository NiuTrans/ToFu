"""tests/test_lifecycle_approval.py — human-approval gate for server lifecycle.

Epic pt_40d00fd526e5479a (2026-07-28 incident): an autopilot conversation
curl'ed ``/api/v1/update/restart`` twice in 3 minutes on a VU's "approval"
(an LLM role-playing the owner), killing 23 in-flight tasks; the second fire
was a crash-resume replay. Owner ruling: restart/shutdown of a LIVE server
requires HUMAN approval. This suite covers:

  * the token store (lib/lifecycle_approval.py): create → decide → validate
    → consume transitions, one-time consumption, action mismatch, expiry,
    cooldown math, and the restart-class call detector;
  * the shell-script gate: the python helper (--script-gate) and the REAL
    restart_15000.sh run non-interactively — it MUST refuse without a token
    and leave the live server untouched;
  * NEUTER A/B: a copy of the script with the gate block stripped DOES kill a
    dummy listener on a test port — proving the gate is load-bearing;
  * the recovery no-refire note: regenerating an interrupted tail that
    carries a restart-class tool call injects the "result unknown — do not
    re-fire" caution into the last user message.

Endpoint-level coverage (202/403/409/429 on the HTTP route) lives in
tests/test_update_restart_guard.py — this file is the module + script +
builder ring.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import pytest

import lib.lifecycle_approval as la
from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit

# The destructive A/B fault-injection cases remain in the internal lifecycle
# lane; portable source behavior is covered by the non-destructive contracts.
_OPENSOURCE = is_opensource_build()

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SCRIPT = os.path.join(ROOT, 'restart_15000.sh')
_TEST_PORT = 15599


def _fresh_store(tmp):
    la._APPROVALS_FILE = os.path.join(tmp, 'approvals.json')
    la._STATE_FILE = os.path.join(tmp, 'state.json')


@pytest.fixture()
def store(tmp_path, monkeypatch):
    _fresh_store(str(tmp_path))
    yield tmp_path


# ── token store transitions ──────────────────────────────────────────

class TestTokenStore:

    def test_happy_path_one_time(self, store):
        rec = la.create_request('restart', origin={'ua': 'pytest'})
        assert rec['status'] == 'pending'
        ok, why = la.validate(rec['id'], 'restart')
        assert not ok and 'not-approved' in why

        decided = la.decide(rec['id'], True)
        assert decided['status'] == 'approved'
        ok, _ = la.validate(rec['id'], 'restart')
        assert ok

        ok, _ = la.consume(rec['id'], 'restart')
        assert ok
        # one-time: a second consume fails closed
        ok, why = la.consume(rec['id'], 'restart')
        assert not ok and 'consumed' in why

    def test_deny_is_terminal(self, store):
        rec = la.create_request('shutdown')
        la.decide(rec['id'], False)
        ok, why = la.validate(rec['id'], 'shutdown')
        assert not ok and 'denied' in why
        ok, _ = la.consume(rec['id'], 'shutdown')
        assert not ok

    def test_unknown_and_mismatch(self, store):
        ok, why = la.validate('nope', 'restart')
        assert not ok and why == 'unknown-id'
        rec = la.create_request('shutdown')
        la.decide(rec['id'], True)
        ok, why = la.validate(rec['id'], 'restart')
        assert not ok and 'action-mismatch' in why

    def test_approved_token_expires(self, store, monkeypatch):
        monkeypatch.setattr(la, 'APPROVED_TTL_SEC', -1)
        rec = la.create_request('restart')
        la.decide(rec['id'], True)
        ok, why = la.validate(rec['id'], 'restart')
        assert not ok and 'expired' in why
        # …and an expired record cannot be approved again (decide fail-closed)
        assert la.decide(rec['id'], True) is None

    def test_decide_twice_rejected(self, store):
        rec = la.create_request('restart')
        assert la.decide(rec['id'], True) is not None
        assert la.decide(rec['id'], False) is None  # already terminal

    def test_list_records_filters(self, store):
        r1 = la.create_request('restart')
        r2 = la.create_request('shutdown')
        la.decide(r2['id'], True)
        pending = la.list_records(status='pending')
        assert any(r['id'] == r1['id'] for r in pending)
        assert all(r['id'] != r2['id'] for r in pending)
        restarts = la.list_records(action='restart')
        assert all(r['action'] == 'restart' for r in restarts)


class TestCooldown:

    def test_stamp_then_remaining(self, store):
        assert la.restart_cooldown_remaining() == 0
        la.stamp_restart()
        remaining = la.restart_cooldown_remaining()
        assert la.RESTART_COOLDOWN_SEC - 2 <= remaining <= la.RESTART_COOLDOWN_SEC

    def test_old_stamp_expires(self, store):
        la.write_json_atomic(la._STATE_FILE,
                             {'last_restart_at': time.time() - 10 * 3600})
        assert la.restart_cooldown_remaining() == 0


class TestDetector:

    def test_matches_restart_class_calls(self):
        rounds = [
            {'toolArgs': '{"command":"curl -sS -X POST http://127.0.0.1:15000/api/v1/update/restart -d {}"}'},
        ]
        assert la.detect_lifecycle_calls(rounds) == ['update/restart']
        assert la.detect_lifecycle_calls(
            [{'query': 'bash restart_15000.sh'}]) == ['restart_15000.sh']
        assert la.detect_lifecycle_calls(
            [{'toolArgs': '{"command":"sudo supervisorctl restart tofu"}'}])
        # complement: ordinary commands must NOT match
        assert la.detect_lifecycle_calls(
            [{'toolArgs': '{"command":"ls -la && git status"}'}]) == []
        assert la.detect_lifecycle_calls([]) == []
        assert la.detect_lifecycle_calls(None) == []


# ── shell-script gate helper ─────────────────────────────────────────

class TestScriptGateHelper:

    def test_blocked_without_token(self, store, capsys):
        rc = la._script_gate('restart')
        assert rc == 3
        out = capsys.readouterr().out
        assert 'REFUSING' in out

    def test_consumes_approved_token(self, store, capsys):
        rec = la.create_request('restart')
        la.decide(rec['id'], True)
        rc = la._script_gate('restart')
        assert rc == 0
        capsys.readouterr()
        # consumed — a second run is blocked
        assert la._script_gate('restart') == 3


# ── real script: static guard + live negative e2e ────────────────────

def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _real_store_has_fireable_restart_token() -> bool:
    """True when the REAL approvals file holds an approved+unexpired+
    unconsumed restart token (the script would CONSUME it and really fire).
    The live negative test must skip rather than risk a real restart."""
    approvals = os.path.join(ROOT, 'data', 'lifecycle_approvals.json')
    orig = la._APPROVALS_FILE
    try:
        la._APPROVALS_FILE = approvals
        for rec in la.list_records(status='approved', action='restart'):
            ok, _ = la.validate(rec['id'], 'restart')
            if ok:
                return True
        return False
    finally:
        la._APPROVALS_FILE = orig


def _defang_pgrep_fallback(text: str) -> str:
    """Neutralise the script's ``pgrep -f 'server\\.py'`` kill fallback in a copy.

    The fallback exists for the mid-crash case (no listener socket) — but its
    pattern ALSO matches the PRODUCTION `python server.py`. In a test copy
    whose dummy listener is already dead (or never started), [1/5]/[2/5]
    would SIGTERM the REAL server. THIS FIRED FOR REAL on 2026-07-28 14:21:
    an earlier version of the fd-9 test ran (a) — which killed the dummy —
    then (b) hit the fallback and killed the live :15000 server, triggering
    a 25-minute guard-race crash loop. Every test copy MUST be defanged.
    """
    out = text.replace("pgrep -f 'server\\.py'",
                       "pgrep -f 'tofu_test_never_matches'")
    assert out != text, 'pgrep server.py fallback not found — script changed?'
    return out


def _allow_isolated_lifecycle_test(text: str) -> str:
    """Opt a retargeted/defanged script copy into lifecycle test execution."""
    marker = '#!/usr/bin/env bash\n'
    assert text.startswith(marker), 'restart script shebang changed?'
    test_environment = (
        'export TOFU_ALLOW_LIFECYCLE_TEST=1\n'
        f'export TOFU_PROJECT_ROOT={shlex.quote(ROOT)}\n'
        f'export TOFU_RUNTIME_PYTHON={shlex.quote(sys.executable)}\n'
    )
    return text.replace(
        marker,
        marker + test_environment, 1)


def _run_orphaned(script_path: str, timeout: int = 120) -> tuple:
    """Run ``script_path`` detached (orphaned → PPID 1), like a setsid watcher.

    The script's descendant guard refuses to run as a CHILD of the :15000
    server — and pytest IS one (an agent shell). The 2026-07-27 incident
    runner was a detached watcher, whose ppid chain ends at init, which is
    exactly the shape that REACHES the [pre/5c] approval gate. This helper
    reproduces that shape: double-fork, parent exits, the orphaned grandchild
    (ppid→1, stdin /dev/null — no TTY) runs the script and drops an rc file.

    Returns (returncode, stdout+stderr).
    """
    tmpdir = tempfile.mkdtemp()
    rc_file = os.path.join(tmpdir, 'rc')
    out_file = os.path.join(tmpdir, 'out')
    launcher = (
        'import os,sys;'
        'pid=os.fork();'
        'sys.exit(0) if pid>0 else None;'
        'os.setsid();'
        'fd=os.open("/dev/null",os.O_RDONLY);os.dup2(fd,0);'
        'out=os.open(%r,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o644);'
        'os.dup2(out,1);os.dup2(out,2);'
        'rc=os.system("bash %s");'
        'open(%r,"w").write(str(os.waitstatus_to_exitcode(rc)));'
        'os._exit(0)'
    ) % (out_file, script_path, rc_file)
    subprocess.run([sys.executable, '-c', launcher], timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(rc_file):
            break
        time.sleep(0.5)
    rc = -1
    try:
        with open(rc_file) as f:
            rc = int(f.read().strip())
    except Exception:
        pass
    output = ''
    try:
        with open(out_file, errors='replace') as f:
            output = f.read()
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    return rc, output


@pytest.mark.serial
class TestRealScriptGate:
    """Real shell-process tests share one fixed listener and restart lock.

    They must never be distributed across xdist workers: both copies use the
    deliberately isolated port 15599 and the fd-inheritance proof observes the
    project-global ``data/.restart.lock``.  Parallel execution turns that
    safety setup into a port/lock race before the lifecycle gate is exercised.
    """

    def test_shipped_script_carries_the_gate(self):
        """Static anchor: the gate block + the token check are in the shipped
        script (the e2e below proves they are load-bearing)."""
        with open(SCRIPT, encoding='utf-8') as f:
            text = f.read()
        assert '[pre/5c]' in text
        assert 'lib.lifecycle_approval --script-gate restart' in text
        assert 'PYTEST_CURRENT_TEST' in text
        assert 'TOFU_ALLOW_LIFECYCLE_TEST' in text

    @pytest.mark.skipif(_OPENSOURCE, reason='legacy live-process fault injection is internal-only')
    def test_noninteractive_run_refuses_and_server_survives(self):
        """A detached non-interactive run must refuse on an isolated listener.

        Never execute the shipped script against :15000 from a test.  A
        collection-time ``skipif(port_listening)`` is a TOCTOU trap: the live
        server can disappear before this case executes, at which point the
        script correctly treats it as dead-server recovery and launches a real
        replacement.  A retargeted copy exercises the identical gate while
        making the strongest possible assertion: production is not a target.
        """
        if _real_store_has_fireable_restart_token():
            pytest.skip('a fireable approved restart token exists in the real '
                        'store — running the script would REALLY restart the server')
        with open(SCRIPT, encoding='utf-8') as f:
            src = f.read()
        ported = src.replace('PORT=15000', f'PORT={_TEST_PORT}', 1)
        assert ported != src
        ported = _defang_pgrep_fallback(ported)
        ported = _allow_isolated_lifecycle_test(ported)
        tmpdir = tempfile.mkdtemp()
        gated_path = os.path.join(tmpdir, 'gated-only.sh')
        with open(gated_path, 'w', encoding='utf-8') as f:
            f.write(ported)
        os.chmod(gated_path, 0o755)
        dummy = subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(_TEST_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if _port_listening(_TEST_PORT):
                    break
                time.sleep(0.1)
            assert _port_listening(_TEST_PORT), 'isolated listener failed to start'
            rc, output = _run_orphaned(gated_path, timeout=90)
            assert rc == 3, (
                f'expected the gate to refuse (exit 3), got {rc}:\n{output}')
            assert 'lifecycle-gate' in output
            assert _port_listening(_TEST_PORT), (
                'the refused isolated listener was harmed')
        finally:
            try:
                dummy.terminate()
                dummy.wait(timeout=5)
            except OSError:
                pass
            except subprocess.TimeoutExpired:
                dummy.kill()
                dummy.wait(timeout=5)
            shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.skipif(_OPENSOURCE, reason='legacy live-process fault injection is internal-only')
    def test_neutered_gate_kills_dummy_on_test_port(self):
        """NEUTER A/B: strip the [pre/5c] block and retarget to a test port —
        the SAME script now kills the dummy listener, proving the gate is
        what stopped it (the relaunch dies harmlessly on the instance lock).
        """
        if _real_store_has_fireable_restart_token():
            pytest.skip('fireable approved restart token in the real store')
        with open(SCRIPT, encoding='utf-8') as f:
            src = f.read()
        # retarget the port on BOTH copies, and defang the pgrep fallback
        # (would SIGTERM the PRODUCTION server if the dummy is dead — see
        # _defang_pgrep_fallback).
        ported = src.replace('PORT=15000', f'PORT={_TEST_PORT}', 1)
        assert ported != src
        ported = _defang_pgrep_fallback(ported)
        ported = _allow_isolated_lifecycle_test(ported)
        # strip the gate block ([pre/5c] … up to the [pre/5b] marker)
        start = ported.index('# ── [pre/5c]')
        end = ported.index('# ── [pre/5b]')
        neutered = ported[:start] + ported[end:]
        # …and stub the relaunch interpreter in the NEUTERED copy only, so the
        # script NEVER boots a real second server in the test (the kill phase
        # is what we prove; /bin/true "launches" and exits instantly, so [4/5]
        # fails health and exits 4 harmlessly). A real relaunch would ALSO
        # inherit the script's restart-lock fd and hold it hostage (separate
        # ticket pt_2a05e161b9814bc2). The gated copy keeps the real python —
        # its gate check runs `python -m lib.lifecycle_approval`.
        py_line = neutered.index('PY="')
        py_end = neutered.index('\n', py_line)
        neutered = neutered[:py_line] + 'PY="/bin/true"' + neutered[py_end:]

        tmpdir = tempfile.mkdtemp()
        gated_path = os.path.join(tmpdir, 'gated.sh')
        neutered_path = os.path.join(tmpdir, 'neutered.sh')
        for path, text in ((gated_path, ported), (neutered_path, neutered)):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            os.chmod(path, 0o755)

        dummy = subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(_TEST_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if _port_listening(_TEST_PORT):
                    break
                time.sleep(0.1)
            if not _port_listening(_TEST_PORT):
                pytest.skip(f'dummy listener on :{_TEST_PORT} failed to start')

            # (a) gated copy: refuses, dummy survives
            rc_g, out_g = _run_orphaned(gated_path, timeout=90)
            assert rc_g == 3, f'gated copy did not refuse (rc={rc_g}):\n{out_g}'
            assert _port_listening(_TEST_PORT), 'gated copy harmed the dummy'

            # (b) neutered copy: NO gate → the kill phase reaches the dummy
            rc_n, out_n = _run_orphaned(neutered_path, timeout=180)
            assert rc_n != 3
            # the dummy was killed (the relaunch then dies on the instance lock)
            for _ in range(30):
                if not _port_listening(_TEST_PORT):
                    break
                time.sleep(0.2)
            assert not _port_listening(_TEST_PORT), (
                'neutered copy did NOT kill the dummy — the gate would not be '
                f'load-bearing (rc={rc_n}):\n{out_n}')
        finally:
            try:
                dummy.send_signal(signal.SIGTERM)
            except OSError:
                pass  # dummy already dead (the neutered copy killed it)
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── restart-lock fd-9 inheritance (pt_2a05e161b9814bc2) ─────────────

@pytest.mark.serial
class TestRestartLockInheritance:
    """The relaunched server must NOT inherit the [pre/5b] restart-lock fd.

    Incident shape: a relaunched `python server.py` inherited fd 9 (the
    script's flock on data/.restart.lock) and held it for 20+ minutes, so
    every later script run blocked 60s at [pre/5b] and aborted doing
    nothing. Fix: `9>&-` on the relaunch line. This test runs a retargeted
    copy (test port, stub interpreter, shortened health wait) and checks the
    lock's holder AFTER the script exits:

      * FIXED copy (with 9>&-): nothing holds the lock once the script exits;
      * NEUTER copy (9>&- stripped): the relaunched child holds it — proving
        the assertion bites and the fix is load-bearing.
    """

    _STUB_NAME = 'tofu_fd9_stub'

    def _make_copies(self, tmpdir, test_port):
        with open(SCRIPT, encoding='utf-8') as f:
            src = f.read()
        ported = src.replace('PORT=15000', f'PORT={test_port}', 1)
        assert ported != src
        # CRITICAL: defang the pgrep fallback BEFORE anything runs — this
        # exact gap SIGTERM'd the production server on 2026-07-28 14:21
        # (run (a) kills the dummy; run (b) then matched 'server\\.py' on
        # the REAL process). See _defang_pgrep_fallback.
        ported = _defang_pgrep_fallback(ported)
        ported = _allow_isolated_lifecycle_test(ported)
        # Each pytest process gets its own serialization and instance locks.
        # ``serial`` only isolates workers inside one invocation; a fixed
        # project lock made independent test invocations contaminate this fd
        # inheritance probe.
        restart_lock = os.path.join(tmpdir, '.restart.lock')
        instance_lock = os.path.join(tmpdir, '.server.lock')
        ported = ported.replace(
            'RESTART_LOCK="${PROJ}/data/.restart.lock"',
            f'RESTART_LOCK="{restart_lock}"', 1)
        ported = ported.replace(
            'ILOCK="${PROJ}/data/.server.lock"',
            f'ILOCK="{instance_lock}"', 1)
        # strip the [pre/5c] approval gate (it has its own e2e; here we
        # exercise the relaunch line only)
        start = ported.index('# ── [pre/5c]')
        end = ported.index('# ── [pre/5b]')
        nogate = ported[:start] + ported[end:]
        # stub interpreter: ignores args, sleeps long enough to outlive the
        # script (never serves — the [4/5] health wait expires → exit 4)
        stub = os.path.join(tmpdir, self._STUB_NAME)
        with open(stub, 'w') as f:
            # `exec` so the stub process IS the sleep (no bash wrapper whose
            # orphaned sleep child would keep holding fd 9 after pkill of the
            # wrapper — that leak blocked the whole suite for 60s once). The
            # portable launcher now probes its interpreter version with stdin;
            # acknowledge only that probe before preserving the long-lived
            # worker behavior this test needs.
            f.write('#!/bin/bash\n'
                    '[ "${1:-}" = "-" ] && exit 0\n'
                    'exec sleep 297\n')
        os.chmod(stub, 0o755)
        py_line = nogate.index('PY="')
        py_end = nogate.index('\n', py_line)
        stubbed = nogate[:py_line] + f'PY="{stub}"' + nogate[py_end:]
        # shorten the [4/5] health wait 40s → 6s (test-only copy tweak)
        stubbed = stubbed.replace('for i in $(seq 1 40); do',
                                  'for i in $(seq 1 6); do', 1)
        # neuter: remove the 9>&- close from the relaunch line
        neutered = stubbed.replace(' 9>&- &', ' &')
        assert neutered != stubbed, 'relaunch 9>&- not found to strip'
        fixed_path = os.path.join(tmpdir, 'fixed.sh')
        neuter_path = os.path.join(tmpdir, 'neutered.sh')
        for path, text in ((fixed_path, stubbed), (neuter_path, neutered)):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            os.chmod(path, 0o755)
        return fixed_path, neuter_path, restart_lock

    def _lock_holders(self, restart_lock):
        try:
            out = subprocess.run(['fuser', restart_lock],
                                 capture_output=True, text=True, timeout=10)
            return set(out.stdout.split())
        except Exception:
            return set()

    def _stub_pids(self):
        out = subprocess.run(['pgrep', '-x', 'sleep'],
                             capture_output=True, text=True, timeout=10)
        mine = set()
        for pid in out.stdout.split():
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                    argv = [a for a in fh.read().split(b'\x00') if a]
            except OSError:
                continue
            if argv == [b'sleep', b'297']:
                mine.add(pid)
        return mine

    def _kill_stubs(self):
        for pid in self._stub_pids():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass

    @pytest.mark.skipif(_OPENSOURCE, reason='legacy live-process fault injection is internal-only')
    def test_relaunched_child_does_not_hold_restart_lock(self):
        if _real_store_has_fireable_restart_token():
            pytest.skip('fireable approved restart token in the real store')
        tmpdir = tempfile.mkdtemp()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            test_port = probe.getsockname()[1]
        fixed_path, neuter_path, restart_lock = self._make_copies(
            tmpdir, test_port)
        dummy = subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(test_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if _port_listening(test_port):
                    break
                time.sleep(0.1)
            if not _port_listening(test_port):
                pytest.skip(f'dummy listener on :{test_port} failed to start')

            # (a) FIXED: after the script exits, NOTHING holds the lock
            rc_f, out_f = _run_orphaned(fixed_path, timeout=120)
            assert rc_f == 4, f'expected health-wait exit 4, got {rc_f}:\n{out_f}'
            holders = self._lock_holders(restart_lock)
            assert not holders, (
                f'lock held after fixed script exited (fd-9 inheritance '
                f'regressed): {holders}')
            self._kill_stubs()

            # (b) NEUTER: without 9>&- the relaunched child KEEPS the lock —
            # proving (a) actually measures the fix, not empty air
            rc_n, out_n = _run_orphaned(neuter_path, timeout=120)
            assert rc_n == 4, f'expected health-wait exit 4, got {rc_n}:\n{out_n}'
            holders_n = self._lock_holders(restart_lock)
            assert holders_n & self._stub_pids(), (
                'neutered copy: relaunched child does NOT hold the lock — '
                'the regression assertion would not bite')
            self._kill_stubs()
        finally:
            try:
                dummy.send_signal(signal.SIGTERM)
            except OSError:
                pass  # dummy already dead (the script copies killed it)
            self._kill_stubs()
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── recovery no-refire note (conv_message_builder seam) ─────────────

class TestRecoveryNote:

    def _build(self, messages, exclude_last):
        import lib.tasks_pkg.conv_message_builder._load as facade
        from unittest.mock import patch
        with patch.object(facade, '_load_messages_from_db', return_value=messages):
            return facade.build_api_messages_from_db(
                'convTest', {}, exclude_last=exclude_last, user_id=1)

    _RESTART_TAIL = {
        'role': 'assistant', 'content': 'partial',
        'interruptedReason': 'manual',
        'toolRounds': [{'toolArgs': '{"command":"curl -X POST http://x/api/v1/update/restart"}'}],
    }

    def test_note_injected_on_regenerate_of_lifecycle_tail(self):
        msgs = [{'role': 'user', 'content': 'restart the server please'},
                dict(self._RESTART_TAIL)]
        built = self._build(msgs, exclude_last=True)
        assert built, 'builder returned nothing'
        users = [m for m in built if m.get('role') == 'user']
        assert users, 'no user message in built context'
        content = users[-1]['content']
        text = content if isinstance(content, str) else ' '.join(
            b.get('text', '') for b in content if isinstance(b, dict))
        assert '严禁再次发出' in text or 'Do NOT issue it again' in text

    def test_no_note_without_lifecycle_call(self):
        tail = dict(self._RESTART_TAIL)
        tail['toolRounds'] = [{'toolArgs': '{"command":"ls -la"}'}]
        msgs = [{'role': 'user', 'content': 'do work'}, tail]
        built = self._build(msgs, exclude_last=True)
        users = [m for m in built if m.get('role') == 'user']
        content = users[-1]['content']
        text = content if isinstance(content, str) else ' '.join(
            b.get('text', '') for b in content if isinstance(b, dict))
        assert '严禁再次发出' not in text and 'Do NOT issue it again' not in text

    def test_no_note_when_tail_not_interrupted(self):
        tail = dict(self._RESTART_TAIL)
        tail.pop('interruptedReason')
        msgs = [{'role': 'user', 'content': 'do work'}, tail]
        built = self._build(msgs, exclude_last=True)
        users = [m for m in built if m.get('role') == 'user']
        content = users[-1]['content']
        text = content if isinstance(content, str) else ' '.join(
            b.get('text', '') for b in content if isinstance(b, dict))
        assert '严禁再次发出' not in text and 'Do NOT issue it again' not in text

    def test_no_note_without_exclude_last(self):
        msgs = [{'role': 'user', 'content': 'do work'}, dict(self._RESTART_TAIL)]
        built = self._build(msgs, exclude_last=False)
        users = [m for m in built if m.get('role') == 'user']
        content = users[-1]['content']
        text = content if isinstance(content, str) else ' '.join(
            b.get('text', '') for b in content if isinstance(b, dict))
        assert '严禁再次发出' not in text and 'Do NOT issue it again' not in text


if __name__ == '__main__':
    unittest.main()

"""Regression: a HALF-OPEN push WebSocket must force-close on ping timeout so it
reconnects — otherwise push frames silently stop forever on a poor network.

WHY
---
`static/js/push.js` piggybacks a ping/pong RTT probe on the live WebSocket. On a
poor network a socket can go HALF-OPEN: TCP is dead but the browser still reports
`readyState === OPEN`, so `_ws.send()` does NOT throw and no `onclose` fires on
its own. Push frames (paper/translate/chat/project events) then silently stop
being delivered and NOTHING re-establishes the socket.

Two coupled defects in the original `_sendPing`:
  1. `_lastPingSentAt` was OVERWRITTEN on every 4s interval, resetting the
     outstanding ping's age before the 8s PING_TIMEOUT_MS window could elapse —
     so a half-open socket on a foregrounded tab was NEVER detected.
  2. Even if detected, the timeout branch only set the latency state; it never
     closed the dead socket, so `onclose` → `_scheduleReconnect` never fired.

THE FIX
-------
`_sendPing` now keeps ONE outstanding ping at a time (does not re-send until
`_onPong` clears `_lastPingSentAt`, so age accumulates) and, on timeout, calls
`_ws.close()` to force `onclose` → reconnect.

CHECKS (drive the REAL shipped push.js under node)
--------------------------------------------------
(A) A pong that never returns → after PING_TIMEOUT_MS the next `_sendPing`
    CLOSES the socket (FakeWS.close called) — the load-bearing fix.
(B) The single-outstanding-ping rule holds: a second `_sendPing` inside the
    timeout window does NOT overwrite the outstanding ping timestamp (so the age
    can actually cross the threshold).
(C) The close drives onclose → a reconnect is scheduled (a new WS is built).
(D) PROOF-OF-LIFE (pt_afbaf3d7 ①b): an inbound DATA frame while a ping is
    outstanding clears the watchdog — no force-close past the original
    window; but true silence AFTER that frame still closes.
(E) ADAPTIVE TIMEOUT (pt_afbaf3d7 ①c): a measured 10s RTT widens the verdict
    to 30s — alive-slow links survive the 8s floor; silence past 30s closes.

DOUBLE-NEUTER: revert the force-close on a COPY of the source → (A)+(C) fail.
NEUTER ①b/①c: drop the proof-of-life clear → (D) fails; pin the timeout to
8s → (E) fails. Each neuter bites exactly its own check.
Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# The harness reads push.js from argv[2] so a neutered COPY can be swapped in.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.location = { protocol: 'http:', host: 'localhost' };
global.apiUrl = (p) => p;
global.console = console;

// ── Controllable clock: _sendPing gates on Date.now() - _lastPingSentAt. ──
let _clock = 1_000_000;
const _realNow = Date.now;
Date.now = () => _clock;

// ── Capture setInterval callbacks so we can fire pings by hand (no real timer),
//    and setTimeout so a scheduled reconnect can be detected + run. ──
let _pingCb = null;
const _timeouts = [];
global.setInterval = (fn) => { _pingCb = fn; return 1; };
global.clearInterval = () => {};
global.setTimeout = (fn, ms) => { _timeouts.push({ fn, ms }); return _timeouts.length; };
global.clearTimeout = () => {};

// ── FakeWS: OPEN on construct; send() no-ops (simulates a half-open socket
//    that accepts writes but is TCP-dead — the server never echoes pong). ──
let _wsBuilt = 0;
let _lastWs = null;
function FakeWS(url) {
  this.url = url;
  this.readyState = 1;         // OPEN
  this.closed = false;
  _wsBuilt++;
  _lastWs = this;
  // push.js assigns onopen/onmessage/onclose AFTER construction.
  Promise.resolve().then(() => { if (this.onopen) this.onopen(); });
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
FakeWS.prototype.send = function () { /* half-open: silently accepted, no pong */ };
FakeWS.prototype.close = function () {
  this.closed = true;
  this.readyState = 3;
  // A real WS fires onclose asynchronously with a non-1000 code on an
  // abnormal close; mimic that so onclose → _scheduleReconnect runs.
  const self = this;
  Promise.resolve().then(() => { if (self.onclose) self.onclose({ code: 1006 }); });
};
global.WebSocket = FakeWS;

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL push.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  // Open the socket + let the onopen microtask run (starts pinging → captures
  // _pingCb and sends the first ping, stamping _lastPingSentAt = current clock).
  pushConnect();
  await Promise.resolve(); await Promise.resolve();

  check('ws_built', _wsBuilt === 1 && !!_pingCb);
  const firstWs = _lastWs;

  // (B) A second ping call still INSIDE the timeout window must NOT overwrite
  //     the outstanding ping — advance the clock a little and fire again; the
  //     socket must NOT be closed yet (age below PING_TIMEOUT_MS=8000).
  _clock += 4000;                 // 4s later — one probe interval, < 8s timeout
  _pingCb();
  check('not_closed_within_window', firstWs.closed === false);

  // (A) Advance PAST the timeout window and fire the probe again. Because only
  //     ONE ping is outstanding (age now 4000+5000 = 9000 > 8000), the timeout
  //     branch must FORCE-CLOSE the half-open socket.
  _clock += 5000;                 // now 9s since the outstanding ping
  _pingCb();
  check('half_open_socket_closed', firstWs.closed === true);

  // (C) The close fired onclose → a reconnect must be scheduled. Run it and
  //     confirm a NEW socket is built (delivery can resume).
  await Promise.resolve(); await Promise.resolve();
  const scheduled = _timeouts.length > 0;
  check('reconnect_scheduled', scheduled);
  if (scheduled) {
    _clock = 2_000_000;             // ws2's first ping stamps a KNOWN value
    _timeouts[_timeouts.length - 1].fn();     // run the scheduled reconnect
    await Promise.resolve(); await Promise.resolve();
    check('new_socket_built', _wsBuilt >= 2);
  } else {
    check('new_socket_built', false);
  }

  // (D) PROOF-OF-LIFE (pt_afbaf3d7 ①b): an inbound DATA frame while a ping
  //     is outstanding proves the socket is alive — the watchdog must NOT
  //     force-close even after the timeout window from the ORIGINAL ping.
  const ws2 = _lastWs;
  _clock += 4000;                   // 2_004_000 — outstanding age 4s
  _pingCb();                        // single-outstanding rule: no new ping, no close
  check('ws2_open_before_pol', ws2.closed === false);
  ws2.onmessage({ data: JSON.stringify({ channel: 'chat', taskId: 't', type: 'content_delta', delta: 'x' }) });
  _clock += 10000;                  // 2_014_000 — 14s since the ORIGINAL ping (> 8s floor)
  _pingCb();                        // without proof-of-life this force-closes
  check('proof_of_life_prevents_force_close', ws2.closed === false);
  // The probe just sent a FRESH ping at 2_014_000; true silence must STILL close.
  _clock += 9000;                   // 2_023_000 — outstanding age 9s > 8s floor
  _pingCb();
  check('silence_after_pol_still_closes', ws2.closed === true);

  // (E) ADAPTIVE TIMEOUT (pt_afbaf3d7 ①c): a measured 10s RTT widens the
  //     half-open verdict to 30s (min(30s, max(8s, 4×RTT))) — a slow-but-alive
  //     link is not force-closed at the 8s floor, but true silence past the
  //     widened window still closes.
  await Promise.resolve(); await Promise.resolve();   // onclose → _scheduleReconnect
  _clock = 3_000_000;
  _timeouts[_timeouts.length - 1].fn();               // scheduled reconnect → ws3
  await Promise.resolve(); await Promise.resolve();   // onopen → ping stamped 3_000_000
  const ws3 = _lastWs;
  check('ws3_built', _wsBuilt >= 3 && ws3 !== ws2);
  _clock = 3_010_000;
  ws3.onmessage({ data: JSON.stringify({ channel: 'system', type: 'pong', t: 3_000_000 }) });  // RTT = 10s
  _clock += 4000;                   // 3_014_000 — interval tick sends a fresh ping
  _pingCb();
  _clock += 20000;                  // 3_034_000 — age 20s: > 8s floor, < 30s adaptive
  _pingCb();
  check('adaptive_grace_not_closed', ws3.closed === false);
  _clock += 11000;                  // 3_045_000 — age 31s > 30s adaptive ceiling
  _pingCb();
  check('adaptive_ceiling_eventually_closes', ws3.closed === true);

  Date.now = _realNow;
  console.log(out.join('\n'));
})();
"""


def _run_harness(push_js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_push_half_open_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, push_js_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_half_open_socket_force_closes_and_reconnects():
    push_js = os.path.join(JS_DIR, 'push.js')
    proc = _run_harness(push_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'push.js half-open reconnect failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_half_open_force_close_double_neuter(tmp_path):
    """DOUBLE-NEUTER: revert the force-close (`_ws.close()` on ping timeout) on a
    COPY of push.js → the half-open socket is never closed and no reconnect is
    scheduled, so (A) + (C) FAIL. Proves the assertions discriminate the fix.
    The shipped file is left byte-identical."""
    push_js = os.path.join(JS_DIR, 'push.js')
    with open(push_js, encoding='utf-8') as f:
        src = f.read()

    # Neuter the force-close: drop the try/close so the timeout branch only sets
    # state (the original buggy behaviour).
    needle = "      try { _ws.close(); }\n      catch (e) { console.debug('[Push] force-close after ping timeout failed:', e); }\n      return;   // do NOT probe again on a socket we've just declared dead"
    assert needle in src, 'force-close fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        "      /* neutered: no force-close */",
        1,
    )
    # ALSO neuter the single-outstanding-ping guard so the original overwrite
    # behaviour returns (otherwise the neutered copy still wouldn't re-send).
    guard = "    if (_lastPingSentAt) return;\n"
    assert guard in neutered, 'single-ping guard fragment drifted'
    neutered = neutered.replace(guard, '', 1)

    copy = tmp_path / 'push_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL half_open_socket_closed' in output, (
        'DOUBLE-NEUTER did not bite: the socket was still closed without the '
        'force-close fix.\n' + output
    )

    # Shipped file untouched.
    with open(push_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped push.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_proof_of_life_and_adaptive_timeout_neuters(tmp_path):
    """NEUTER ①b: drop the proof-of-life clear on a COPY → (D) fails (the
    socket is force-closed despite the inbound data frame). NEUTER ①c: pin
    the timeout to the 8s floor on a COPY → (E) fails (alive-slow link closed
    at 8s). Each neuter bites EXACTLY its own check — proving the new checks
    discriminate their own fix, not the whole file. Shipped file untouched."""
    push_js = os.path.join(JS_DIR, 'push.js')
    with open(push_js, encoding='utf-8') as f:
        src = f.read()

    # ── NEUTER ①b: proof-of-life clear removed ──
    pol = ("      if (_lastPingSentAt) {\n"
           "        _lastPingSentAt = 0;\n"
           "        if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }\n"
           "      }")
    assert pol in src, 'proof-of-life fragment drifted — update the neuter target'
    neutered = src.replace(pol, '      /* neutered: data frames no longer reset the probe */', 1)
    copy = tmp_path / 'push_no_pol.js'
    copy.write_text(neutered, encoding='utf-8')
    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails_b = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert fails_b == ['FAIL proof_of_life_prevents_force_close'], (
        'NEUTER ①b should bite EXACTLY the proof-of-life check:\n' + output)

    # ── NEUTER ①c: adaptive timeout pinned to the 8s floor ──
    adaptive = 'return Math.min(PING_TIMEOUT_MAX_MS, Math.max(PING_TIMEOUT_MS, rtt * 4));'
    assert adaptive in src, 'adaptive-timeout fragment drifted — update the neuter target'
    neutered = src.replace(adaptive, 'return PING_TIMEOUT_MS;', 1)
    copy2 = tmp_path / 'push_fixed_timeout.js'
    copy2.write_text(neutered, encoding='utf-8')
    proc2 = _run_harness(str(copy2))
    output2 = proc2.stdout.strip()
    assert proc2.returncode == 0, f'node failed: {proc2.stderr}\n{output2}'
    fails_c = [ln for ln in output2.splitlines() if ln.startswith('FAIL')]
    assert fails_c == ['FAIL adaptive_grace_not_closed'], (
        'NEUTER ①c should bite EXACTLY the adaptive-grace check:\n' + output2)

    # Shipped file untouched.
    with open(push_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped push.js'

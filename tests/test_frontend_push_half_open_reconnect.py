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

DOUBLE-NEUTER: revert the force-close on a COPY of the source → (A)+(C) fail.
Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
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
    _timeouts[_timeouts.length - 1].fn();     // run the scheduled reconnect
    await Promise.resolve(); await Promise.resolve();
    check('new_socket_built', _wsBuilt >= 2);
  } else {
    check('new_socket_built', false);
  }

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
    assert output.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{output}'


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

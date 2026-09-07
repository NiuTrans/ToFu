"""The topbar badge projects canonical Push RTT/close and chat-SSE health.

The Push owner already has one per-ping timeout that emits ``timeout`` and
force-closes a half-open socket; ``onclose`` emits ``offline``. The badge must
merge those events with the typed SSE aggregate without creating a second
four-second liveness interval or guessing transport state from elapsed time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


# argv[2]=push.js path, argv[3]=net-latency.js path.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.location = { protocol: 'http:', host: 'localhost' };
global.apiUrl = (p) => p;
global.t = (k) => k;                 // i18n: return the key (assert on keys)
global.console = console;

// ── Controllable clock ──
let _clock = 2_000_000;
Date.now = () => _clock;

// Push owns one ping interval plus demand timeouts for ping failure/reconnect.
// The badge must add no interval of its own.
let _intervals = [];
let _timeouts = [];
global.setInterval = (fn, ms) => { _intervals.push({ fn, ms }); return _intervals.length; };
global.clearInterval = () => {};
global.setTimeout = (fn, ms) => { _timeouts.push({ fn, ms }); return _timeouts.length; };
global.clearTimeout = (id) => { if (id && _timeouts[id-1]) _timeouts[id-1] = { fn: () => {}, ms: 0 }; };
global.requestAnimationFrame = (fn) => { return 0; };
global.cancelAnimationFrame = () => {};

// ── FakeWS: OPEN on construct; captures the ping frame's `t` so the test can
//    echo a pong back through onmessage (drives a REAL good RTT reading). ──
let _lastWs = null;
let _lastPingT = null;
function FakeWS(url) {
  this.url = url; this.readyState = 1; this.closed = false; _lastWs = this;
  Promise.resolve().then(() => { if (this.onopen) this.onopen(); });
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
FakeWS.prototype.send = function (raw) {
  try { const f = JSON.parse(raw); if (f.action === 'ping') _lastPingT = f.t; } catch (e) {}
};
FakeWS.prototype.close = function () {
  this.closed = true; this.readyState = 3;
  const self = this;
  Promise.resolve().then(() => { if (self.onclose) self.onclose({ code: 1006 }); });
};
global.WebSocket = FakeWS;

// ── Minimal DOM: the netLatencyBadge span + its two children. ──
function makeEl(id) {
  return {
    id, title: '', dataset: {}, className: '',
    _children: [], innerHTML: '', textContent: '',
    querySelector(sel) {
      if (sel === '.net-bars') return this._bars || (this._bars = makeBars());
      if (sel === '.net-ms') return this._ms || (this._ms = makeEl('ms'));
      return null;
    },
  };
}
function makeBars() {
  const bars = { _kids: [], innerHTML: '',
    appendChild(c) { this._kids.push(c); },
    get children() { return this._kids; },
  };
  // net-latency clears innerHTML then appends 4; emulate the clear.
  Object.defineProperty(bars, 'innerHTML', {
    get() { return ''; },
    set(v) { if (v === '') this._kids = []; },
  });
  return bars;
}
function makeBar() {
  return { className: '', _lit: false,
    classList: { toggle(cls, on) { if (cls === 'lit') this._owner._lit = !!on; } } };
}
const _badge = makeEl('netLatencyBadge');
// Wire bar factory: net-latency does document.createElement('span').
global.document = {
  readyState: 'complete',
  getElementById: (id) => (id === 'netLatencyBadge' ? _badge : null),
  createElement: () => { const b = makeBar(); b.classList._owner = b; return b; },
  addEventListener: () => {},
};

// Load push.js FIRST (net-latency.js calls pushOnLatency / pushIsConnected /
// pushConnect / pushGetLatency from it), then net-latency.js.
eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL push.js
eval(fs.readFileSync(process.argv[3], 'utf8'));   // REAL net-latency.js

// ── Fake typed connection-health source. net-latency.js calls
//    streamHealthSubscribe(fn) — we capture fn so the test can toggle degraded. ──
let _streamCb = null;
global.streamHealthSubscribe = (fn) => { _streamCb = fn; fn({ degraded: false, count: 0, at: Date.now() }); return () => {}; };

// Re-init now that the seam exists (the IIFE already self-init'd on load, but
// streamHealthSubscribe wasn't defined yet — call again; it's idempotent).
initNetLatency();

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Deliver a REAL good RTT: pushConnect opened the FakeWS + sent a ping (captured
// as _lastPingT). Echo the pong back through the socket's onmessage so push.js
// computes RTT and emits {state:'good'} to net-latency's listener.
function feedGoodPong(rttMs) {
  _clock += rttMs;
  if (_lastWs && _lastWs.onmessage) {
    _lastWs.onmessage({ data: JSON.stringify({ channel: 'system', type: 'pong', t: _lastPingT }) });
  }
}

(async () => {
  // Let onopen fire (starts pinging → sends first ping), then echo a fast pong.
  await Promise.resolve(); await Promise.resolve();
  feedGoodPong(50);   // → push emits good/50ms; net-latency paints it

  // (A ①) SSE degraded with a good RTT → badge warns.
  _streamCb({ degraded: true, count: 1, at: _clock });
  check('A_sse_degraded_state_poor', _badge.dataset.state === 'poor');
  check('A_sse_degraded_label_reconnecting', _badge._ms.textContent === 'net.reconnecting');

  // (B ①) SSE recovers → badge returns to good.
  _streamCb({ degraded: false, count: 0, at: _clock });
  check('B_sse_recovered_state_good', _badge.dataset.state === 'good');
  check('B_sse_recovered_label_ms', _badge._ms.textContent === '50ms');

  check('C_badge_adds_no_liveness_interval',
    _intervals.filter(iv => iv.ms === 4000).length === 1);

  // (D) A real Push close emits the canonical offline verdict.
  if (_lastWs && _lastWs.onclose) _lastWs.onclose({ code: 1006 });
  await Promise.resolve(); await Promise.resolve();
  check('D_closed_socket_offline', _badge.dataset.state === 'offline');

  // (E) The Push per-ping watchdog is a setTimeout(_firePingTimeout,
  //   PING_TIMEOUT_MS) armed when a ping is SENT. Reconnect a fresh socket,
  //   let it open + send a ping, then fire the armed watchdog by hand: it must
  //   emit {state:'timeout'} promptly (not wait for the next 4s interval tick)
  //   and force-close the socket. We assert the badge flips to 'timeout'.
  //   Reconnect: run the scheduled reconnect timer from the (D) onclose.
  const rc = _timeouts.find(to => to.ms > 0 && to.ms < 30001 && to.fn);
  if (rc) rc.fn();                       // connect() → new FakeWS
  await Promise.resolve(); await Promise.resolve();   // onopen → sends ping, arms watchdog
  // The most-recently armed setTimeout with ms === PING_TIMEOUT_MS(8000) is the
  // per-ping watchdog. Fire it: no pong arrived, so it must emit timeout.
  const pingWd = [..._timeouts].reverse().find(to => to.ms === 8000);
  check('E_ping_watchdog_armed', !!pingWd);
  if (pingWd) {
    pingWd.fn();
    check('E_ping_timeout_emitted', _badge.dataset.state === 'timeout');
  } else {
    check('E_ping_timeout_emitted', false);
  }

  console.log(out.join('\n'));
})();
"""


def _run_harness(push_js: str, net_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_net_latency_signal_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, push_js, net_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_signal_badge_reflects_sse_and_proxy_jitter():
    push_js = os.path.join(JS_DIR, 'push.js')
    net_js = os.path.join(JS_DIR, 'net-latency.js')
    proc = _run_harness(push_js, net_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'signal-badge behavior failures:\n' + output
    assert output.count('PASS') == 8, f'expected 8 PASS lines, got:\n{output}'


def test_badge_source_has_no_competing_liveness_clock():
    source = Path(os.path.join(JS_DIR, 'net-latency.js')).read_text(
        encoding='utf-8')
    assert 'setInterval' not in source
    assert '_watchdogTimer' not in source
    assert '_STALE_MS' not in source
    assert 'pushIsConnected' not in source
    assert 'retainedCompositionLifecycle.add(destroyNetLatency)' in source

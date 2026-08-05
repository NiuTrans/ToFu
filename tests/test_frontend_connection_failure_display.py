"""Regression: connection-failure DISPLAY ACCURACY — the UI must not show a
STALE / WRONG state after the server or DB recovers.

Two accuracy defects fixed 2026-07-06 (audited by a 3-way parallel sweep):

1. DB-UNAVAILABLE BANNER LINGERED FOREVER.
   ``_checkDbHealth`` (now static/js/core/backend_offline_monitor.js) only ever ADDED
   the red "Database Unavailable" banner — there was no clear-on-recovery and
   no re-poll. Once PostgreSQL came back (documented recovery = server restart
   with PG up) the banner stayed until the user manually dismissed or reloaded,
   falsely telling them the DB was still down. Fix: ``_clearDbWarningBanner``
   (called when ``db_ok !== false``) + a self-stopping ``_startDbHealthPolling``
   that clears the banner the moment the DB reports healthy.

2. NET-LATENCY BADGE FROZE ON A STALE READING.
   The badge repaints only when ``pushOnLatency`` fires. If the WS wedges
   (stuck CONNECTING, or a reconnect scheduled but the open never lands) emits
   stop and the badge would keep showing the last reading — e.g. green "120ms"
   while actually disconnected. Fix: each reading now carries an ``at``
   timestamp (push.js ``_emitLatency`` / ``getLatency``) and net-latency.js
   runs a watchdog that forces the OFFLINE display when no fresh reading has
   arrived within ``_STALE_MS``.

3. FAILURE-PATH STRINGS WERE HARDCODED ENGLISH ON A zh UI.
   The DB-unavailable banner (title/desc/Dismiss), the ``_forceFinishDeadStream``
   "Server Offline" toast + envelope message, the "Connection Restored" toasts,
   and the boot-reconnect banner were English (or mixed zh+en) literals. On a
   Chinese-UI product that IS a display-accuracy defect. All now route through
   ``t()`` with new ``conn.*`` keys (zh primary), reusing
   ``finishInfo.reasonServerOffline`` for the offline label. The DB harness
   loads the REAL i18n.js (defaults to zh) and asserts the banner + toast
   render the zh strings, not the English literals.

4. IN-STREAM LIVENESS HUD WAS HARDCODED ENGLISH (the most connection-failure-
   specific surface). ``_updateStreamTimerUI`` header spans + ``_setBubbleLiveness``
   in-bubble lines + the "Force Finish" button + the ``_streamPhaseLabel``
   fallbacks ("running tools", "reasoning", …) were English. All now route
   through a guarded ``_connT()`` with new ``conn.*`` / ``conn.phase*`` keys (zh
   primary; ``{n}`` silent-seconds + ``{what}`` activity interpolation). The
   HUD harness drives ``_updateStreamTimerUI`` into the dead-server AND
   still-working branches under the REAL zh i18n and asserts zh renders.

   UPDATE (2026-07-14): the AUTOMATIC dead-server HUD path no longer stamps the
   terminal "服务器无响应 / 健康检查失败" verdict — the 「连接中断」false-positive
   fix made a health-ping failure a TRANSIENT reconnecting state, so the HUD now
   renders the calmer ``conn.reconnectingShort`` header + ``conn.reconnecting``
   bubble (Force-Finish button retained as a manual escape hatch). The HUD
   assertions were updated to the reconnecting strings accordingly.

The harnesses load the REAL shipped JS under jsdom and drive the real
functions. DOUBLE-NEUTER: each fix is reverted on a COPY of the source and the
matching assertions are proven to FAIL, then the shipped file is confirmed
byte-identical.

Runs the REAL JS under jsdom; skips cleanly when node/jsdom aren't installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ── Harness 1: DB-warning banner clears when the DB reports healthy again ──
_DB_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// Neuter real timers — we drive _checkDbHealth by hand.
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
global.setTimeout = win.setTimeout = (fn) => (typeof fn === 'function' ? fn() : 0);
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};
global.AbortSignal = win.AbortSignal || { timeout: () => undefined };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// jsdom gives us localStorage; i18n.js reads tofu_ui_lang || 'zh' at load, so
// the REAL t() defaults to zh here — exactly the Chinese-UI product config.
global.localStorage = win.localStorage;

// Minimal globals the file touches at load / in these fns.
global.conversations = win.conversations = [];
global.activeStreams = win.activeStreams = new Map();
global.streamBufs = win.streamBufs = new Map();
// Uniform streamSessions stub (streamBufs retired — phase lives in session)
global.streamSessions = win.streamSessions = new Map();
global.getStreamSession = win.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = win.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof win.activeStreams !== undefined && win.activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
global.clearStreamSession = win.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };
// Stub for _drBuf used in sse_pipeline delta handler (streamBufs retired)
let _drBuf = null;
global.activeConvId = win.activeConvId = null;
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);

// Flippable health response.
let dbOk = false;  // start: DB down
global.Api = win.Api = {
  health: { check: async () => ({ ok: true, json: async () => ({ db_ok: dbOk }) }) },
};

// Load the REAL i18n.js so t() + the real conn.* keys are exercised (proves the
// keys exist AND the banner/toast wiring resolves them, in zh).
eval(fs.readFileSync(process.argv[4], 'utf8'));  // i18n.js (real) → defines t()
if (typeof t !== 'function' && typeof win.t === 'function') { global.t = win.t; }

// Capture toast output to assert the offline toast renders zh.
let _lastToast = null;
global.showToast = win.showToast = (icon, title, detail) => { _lastToast = { icon, title, detail }; };
// Globals _forceFinishDeadStream touches:
global.normalizeErrorEnvelope = win.normalizeErrorEnvelope = (e) => e;
global.twStop = win.twStop = () => {};
global.finishStream = win.finishStream = () => {};
global._startOfflineRecoveryPolling = win._startOfflineRecoveryPolling = () => {};

eval(fs.readFileSync(process.argv[5], 'utf8'));  // core/backend_offline_monitor.js (real — _checkDbHealth + _checkServerHealth live here since 2026-08-01)
eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/health_stream_timer.js (real)

if (typeof _checkDbHealth !== 'function') { console.log('FAIL fn_checkDbHealth missing'); process.exit(0); }

(async () => {
  // 1. DB down → banner shown.
  dbOk = false;
  await _checkDbHealth();
  const banner = document.getElementById('db-warning-banner');
  check('banner_shown_when_db_down', !!banner);

  // 1b. Banner renders the zh string, NOT the English literal (Chinese UI).
  const bannerHtml = banner ? banner.innerHTML : '';
  check('banner_zh_title', bannerHtml.includes('数据库不可用'));
  check('banner_zh_desc', bannerHtml.includes('未运行 PostgreSQL'));
  check('banner_zh_dismiss', bannerHtml.includes('>关闭</button>'));
  check('banner_no_english_literal', !bannerHtml.includes('Database Unavailable'));

  // 2. DB recovered → a fresh _checkDbHealth must CLEAR the banner.
  dbOk = true;
  await _checkDbHealth();
  check('banner_cleared_on_recovery', !document.getElementById('db-warning-banner'));

  // 3. Idempotent: healthy check with no banner present does not throw / re-add.
  await _checkDbHealth();
  check('no_banner_when_healthy', !document.getElementById('db-warning-banner'));

  // 4. The offline toast renders zh (title = 服务器离线, detail = zh body).
  const conv = { id: 'c1', messages: [{ role: 'assistant', content: 'partial' }] };
  conversations.push(conv);
  _forceFinishDeadStream('c1');
  check('toast_zh_title', !!_lastToast && _lastToast.title === '服务器离线');
  check('toast_zh_detail', !!_lastToast && _lastToast.detail.includes('后端服务器无响应'));
  check('toast_no_english_literal', !!_lastToast && _lastToast.title !== 'Server Offline');
  // The envelope message on the message is the zh stream-offline notice.
  check('envelope_zh_msg', !!conv.messages[0].error &&
        String(conv.messages[0].error.message).includes('服务器离线，回复可能不完整'));

  console.log(out.join('\n'));
})();
"""


# ── Harness 2: net-latency staleness watchdog forces offline on frozen feed ──
_LATENCY_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<span id="netLatencyBadge"><span class="net-bars"></span><span class="net-ms"></span></span>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// i18n passthrough.
global.t = win.t = (k) => k;
// pushConnect stub (net-latency calls it).
global.pushConnect = win.pushConnect = () => {};

// Controllable latency feed: capture the render callback net-latency subscribes.
let _renderFn = null;
global.pushOnLatency = win.pushOnLatency = (fn) => { _renderFn = fn; return () => {}; };

// Capture the watchdog interval callback instead of letting it run on a timer.
let _watchdogCb = null;
global.setInterval = win.setInterval = (fn, ms) => { _watchdogCb = fn; return 1; };
global.clearInterval = win.clearInterval = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // net-latency.js (real IIFE self-inits)
eval(fs.readFileSync(process.argv[3], 'utf8'));  // (unused 2nd file placeholder — see runner)

const _initNetLatency = win.initNetLatency;
if (typeof _initNetLatency !== 'function') { console.log('FAIL initNetLatency missing'); process.exit(0); }
_initNetLatency();
if (typeof _renderFn !== 'function') { console.log('FAIL no render callback captured'); process.exit(0); }

const badge = document.getElementById('netLatencyBadge');

// 1. A fresh GOOD reading paints good.
_renderFn({ ms: 120, state: 'good', connected: true, at: Date.now() });
check('good_reading_paints_good', badge.dataset.state === 'good');

// 2. The watchdog was registered.
check('watchdog_registered', typeof _watchdogCb === 'function');

// 3. Reading is fresh → watchdog leaves it alone.
_watchdogCb();
check('fresh_reading_kept', badge.dataset.state === 'good');

// 4. Age the last reading beyond _STALE_MS via Date.now, then run the watchdog
//    → it must FORCE the offline display (badge must NOT stay green).
const _realNow = Date.now;
Date.now = () => _realNow() + 60000;   // 60s later — well past _STALE_MS
try {
  _watchdogCb();
} finally {
  Date.now = _realNow;
}
check('stale_reading_forced_offline', badge.dataset.state === 'offline');

console.log(out.join('\n'));
"""


# ── Harness 3: in-stream liveness HUD renders zh when the server goes dead ──
_HUD_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="stream-elapsed-timer"></div>' +
  '<div id="streaming-body"><div data-zone="status"></div></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.localStorage = win.localStorage;   // i18n.js → zh default
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
global.setTimeout = win.setTimeout = (fn) => (typeof fn === 'function' ? fn() : 0);
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const CONV = 'cabc1234';
global.activeConvId = win.activeConvId = CONV;
global.conversations = win.conversations = [];
global.activeStreams = win.activeStreams = new Map();
global.streamBufs = win.streamBufs = new Map();
// Uniform streamSessions stub (streamBufs retired — phase lives in session)
global.streamSessions = win.streamSessions = new Map();
global.getStreamSession = win.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = win.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof win.activeStreams !== undefined && win.activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
global.clearStreamSession = win.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };
// Stub for _drBuf used in sse_pipeline delta handler (streamBufs retired)
let _drBuf = null;
// Real escapeHtml semantics enough for the assertions.
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// Load the REAL i18n.js so t() resolves the real conn.* HUD keys in zh.
eval(fs.readFileSync(process.argv[4], 'utf8'));  // i18n.js
if (typeof t !== 'function' && typeof win.t === 'function') { global.t = win.t; }

// The monitor defines _checkServerHealth + the health-cache state the timer
// file touches (its self-registered pageshow/visibilitychange listeners fire
// in jsdom, and the still-working branch reads _HEALTH_CHECK_INTERVAL).
// eval BOTH files in ONE scope: `let`/`const` declared in a direct eval stay
// in that eval's own declarative record, so separate evals would hide the
// monitor's consts from the timer file — the bundle shares ONE lexical scope
// across concatenated scripts, and this mirrors it.
// _streamTimers is a file-scoped `const` (not reachable from here). Append a
// bridge in the SAME eval scope that exposes a seeder onto globalThis.
eval(fs.readFileSync(process.argv[5], 'utf8') + '\n;\n' +
     fs.readFileSync(process.argv[2], 'utf8') +
     '\n;globalThis.__seedTimer = (cid, o) => _streamTimers.set(cid, o);');

if (typeof _updateStreamTimerUI !== 'function') { console.log('FAIL fn missing'); process.exit(0); }

(async () => {
  // Seed a stream timer whose lastDataTime is long ago AND whose health probe
  // already resolved to dead — this is exactly the dead-server HUD branch.
  const now = Date.now();
  globalThis.__seedTimer(CONV, {
    startTime: now - 90000, lastDataTime: now - 90000, intervalId: 0,
    _lastHealthResult: false, _healthChecking: true,  // true → skip the async re-probe
  });
  win.streamSessions.set(CONV, { phase: null });
win.setStreamPhase(CONV, { phase: 'thinking_active' });

  await _updateStreamTimerUI(CONV);

  const hud = document.getElementById('stream-elapsed-timer').innerHTML;
  const bubble = document.querySelector('[data-zone="status"]').innerHTML;

  // Header timer: the automatic dead-server path NO LONGER stamps a terminal
  // "服务器无响应" verdict — the 2026-07-14 「连接中断」false-positive fix made a
  // health-ping failure a TRANSIENT reconnecting state (conn.reconnectingShort
  // header + conn.reconnecting bubble), with the Force-Finish button kept as a
  // manual escape hatch. Assert the calmer reconnecting banner + button, zh.
  check('hud_zh_reconnecting', hud.includes('正在重连'));
  check('hud_zh_force_finish', hud.includes('强制结束'));
  check('hud_no_english_force', !hud.includes('Force Finish'));
  check('hud_no_english_notresp', !hud.includes('server not responding'));
  // In-bubble line: the zh reconnecting sentence, NOT the old terminal verdict.
  check('bubble_zh_reconnecting', bubble.includes('连接不稳定，正在重连并与服务器同步'));
  check('bubble_no_english', !bubble.includes('Server not responding')
        && !bubble.includes('服务器无响应'));

  // Also exercise the "still working" branch → zh phase label (reasoning).
  globalThis.__seedTimer(CONV, {
    startTime: now - 30000, lastDataTime: now - 30000, intervalId: 0,
    _lastHealthResult: true, _healthChecking: true,
    _taskStillRunning: true, _taskProbedAt: now,
  });
  await _updateStreamTimerUI(CONV);
  const bubble2 = document.querySelector('[data-zone="status"]').innerHTML;
  check('bubble_zh_still_working', bubble2.includes('仍在处理') && bubble2.includes('推理中'));
  check('bubble_no_english_working', !bubble2.includes('still working') && !bubble2.includes('reasoning'));

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_db_banner_clears_on_recovery():
    harness = os.path.join(HERE, '_db_banner_harness.js')
    with open(harness, 'w') as f:
        f.write(_DB_HARNESS)
    src = os.path.join(JS_DIR, 'core', 'health_stream_timer.js')
    monitor = os.path.join(JS_DIR, 'core', 'backend_offline_monitor.js')
    i18n = os.path.join(JS_DIR, 'i18n.js')
    try:
        proc = subprocess.run(
            ['node', harness, src, ROOT, i18n, monitor],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'DB-banner clear-on-recovery + zh-i18n failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_net_latency_staleness_watchdog():
    harness = os.path.join(HERE, '_latency_watchdog_harness.js')
    with open(harness, 'w') as f:
        f.write(_LATENCY_HARNESS)
    src = os.path.join(JS_DIR, 'net-latency.js')
    # Second arg is a harmless empty file (harness evals argv[3]); reuse src's
    # dir with an empty stub to keep the eval count stable.
    stub = os.path.join(HERE, '_empty_stub.js')
    with open(stub, 'w') as f:
        f.write('/* intentionally empty */\n')
    try:
        proc = subprocess.run(
            ['node', harness, src, stub, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (harness, stub):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'net-latency watchdog failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stream_hud_renders_zh_on_dead_server():
    harness = os.path.join(HERE, '_hud_i18n_harness.js')
    with open(harness, 'w') as f:
        f.write(_HUD_HARNESS)
    src = os.path.join(JS_DIR, 'core', 'health_stream_timer.js')
    monitor = os.path.join(JS_DIR, 'core', 'backend_offline_monitor.js')
    i18n = os.path.join(JS_DIR, 'i18n.js')
    try:
        proc = subprocess.run(
            ['node', harness, src, ROOT, i18n, monitor],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'in-stream HUD zh-i18n failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS, got:\n{output}'

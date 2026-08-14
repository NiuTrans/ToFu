"""Regression: a backend kill must raise a PROMINENT global offline indicator,
not leave the page looking alive.

WHY
---
When the backend process is killed (the nightly OOM SIGKILL pattern), every
chat SSE hangs and the only visible signal used to be the tiny topbar signal
badge (net-latency.js) going gray. The page "looked alive", so the owner kept
waiting on dead conversations instead of intervening:

  ① With ZERO active streams, nothing watched backend liveness at all — the
     per-stream health checks (health_stream_timer.js) only run while a
     stream is ACTIVE and silent.
  ② No prominent indicator: a small gray signal-bars icon is easy to miss.

THE FIX (core/backend_offline_monitor.js)
-----------------------------------------
  Two passive signals (push.js socket state via pushOnLatency + browser
  online/offline events) + one active ARBITER (a /api/health probe). The
  banner requires TWO consecutive probe failures so a buffering-proxy hiccup
  (VS Code port-forward WS drop with a healthy backend) never raises it.
  OFFLINE → fixed-top red banner + live elapsed counter + document.title
  prefix (visible on a backgrounded TAB) + 5s recovery poll. RECOVERY →
  banner removed, title restored, toast, and the SAME recovery machinery the
  visibilitychange/online hooks use (pushConnect nudge +
  _probeAllStuckStreamsOnWake + _recoverOfflineConversations +
  _revalidateOnResume).

CHECKS (drive the REAL shipped JS under node, one process per scenario)
-----------------------------------------------------------------------
  A: push drop + 2 failed probes → banner + title prefix + poll/elapsed
     timers; probe OK → banner removed, title restored, recovery fns fired.
  B: push drop + 1 fail + 1 OK (proxy hiccup) → NO banner, quiet recovery.
  C: browser 'offline' event → network-variant banner/title; 'online' → recover.
  D: snooze hides the banner; the elapsed ticker re-shows it after 60s.

DOUBLE-NEUTER: drop the 2-fail confirmation gate (alarm on the FIRST failure)
→ (B) FAILS because the banner appears on a mere hiccup. Proves the gate is
load-bearing. The shipped file is left byte-identical.

Source-scan guards pin the registration: file in _BUNDLE_FILES after push.js,
no raw app tag in index.html, and the i18n keys in i18n.js.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
MODULE = runtime_section_path('core/backend_offline_monitor.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# argv[2]=module path (a neutered COPY can be swapped in), argv[3]=scenario.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Controllable clock ──
let _clock = 2_000_000;
Date.now = () => _clock;

// ── Timer capture (addressable setInterval/setTimeout) ──
let _intervals = [];
let _timeouts = [];
global.setInterval = (fn, ms) => { const it = { fn, ms, dead: false }; _intervals.push(it); return _intervals.length; };
global.clearInterval = (id) => { const it = _intervals[id - 1]; if (it) it.dead = true; };
global.setTimeout = (fn, ms) => { const it = { fn, ms, dead: false }; _timeouts.push(it); return _timeouts.length; };
global.clearTimeout = (id) => { const it = _timeouts[id - 1]; if (it) it.dead = true; };
if (typeof AbortSignal === 'undefined' || typeof AbortSignal.timeout !== 'function') {
  global.AbortSignal = { timeout: () => ({}) };
}

// ── navigator ──
global.navigator = { onLine: true };

// ── Minimal DOM ──
function makeEl(tag) {
  return {
    tag, id: '', style: {}, dataset: {}, className: '',
    innerHTML: '', textContent: '', removed: false, children: [],
    _parent: null, _qs: {},
    appendChild(c) { this.children.push(c); },
    remove() {
      this.removed = true;
      if (this._parent) this._parent.children = this._parent.children.filter(x => x !== this);
    },
    querySelector(sel) {
      if (!this._qs[sel]) this._qs[sel] = makeEl('span');
      return this._qs[sel];
    },
    setAttribute() {}, getAttribute() { return null; },
  };
}
const _body = makeEl('body');
_body.prepend = function (el) { el._parent = this; this.children.unshift(el); };
let _docListeners = {};
global.document = {
  readyState: 'complete',
  visibilityState: 'visible',
  title: 'Tofu',
  body: _body,
  getElementById: () => null,
  createElement: (t) => makeEl(t),
  addEventListener: (ev, fn) => { (_docListeners[ev] = _docListeners[ev] || []).push(fn); },
  removeEventListener: (ev, fn) => {
    _docListeners[ev] = (_docListeners[ev] || []).filter(x => x !== fn);
  },
};

// ── window events (window === global) ──
let _winListeners = {};
global.addEventListener = (ev, fn) => { (_winListeners[ev] = _winListeners[ev] || []).push(fn); };
global.removeEventListener = (ev, fn) => {
  _winListeners[ev] = (_winListeners[ev] || []).filter(x => x !== fn);
};

let _scopeCreates = 0, _scopeDestroys = 0;
if (process.argv[3] === 'E') {
  global.TofuModules = {
    createLifecycleScope: () => {
      _scopeCreates++;
      const cleanups = [];
      return {
        signal: {},
        listen(target, type, fn) {
          target.addEventListener(type, fn);
          cleanups.push(() => target.removeEventListener(type, fn));
        },
        interval(fn, ms) {
          const id = setInterval(fn, ms);
          cleanups.push(() => clearInterval(id));
          return id;
        },
        timeout(fn, ms) {
          const id = setTimeout(fn, ms);
          cleanups.push(() => clearTimeout(id));
          return id;
        },
        add(fn) { cleanups.push(fn); },
        destroy() {
          _scopeDestroys++;
          while (cleanups.length) cleanups.pop()();
        },
      };
    },
  };
}

// ── Controllable health probe ──
let _healthOk = true;
let _healthStatus = 503;
let _probeCount = 0;
global.Api = {
  health: { check: async () => {
    _probeCount++;
    return _healthOk ? { ok: true, status: 200 } : { ok: false, status: _healthStatus };
  } },
};

// ── push seams ──
let _pushCbs = [];
global.pushOnLatency = (fn) => {
  _pushCbs.push(fn);
  try { fn({ ms: 50, state: 'good', connected: true, at: Date.now() }); } catch (e) {}
  return () => {};
};
global.pushOnReconnect = () => () => {};
let _pushConnectCalls = 0;
global.pushConnect = () => { _pushConnectCalls++; };

// ── Recovery spies ──
let _probeStuck = 0, _recoverConv = 0, _revalidate = 0;
global._probeAllStuckStreamsOnWake = () => { _probeStuck++; };
global._recoverOfflineConversations = async () => { _recoverConv++; return 0; };
global._revalidateOnResume = () => { _revalidate++; };
let _toasts = [];
global.showToast = (icon, title, desc, ms) => { _toasts.push({ icon, title, desc, ms }); };

// ── i18n: zh literals with {n}/{t} interpolation ──
global.t = (k, p) => ({
  'conn.backendOfflineTitle': '后端服务器已离线',
  'conn.backendOfflineDesc': '每 ' + (p && p.n) + ' 秒自动重试',
  'conn.networkOfflineTitle': '本机网络已断开',
  'conn.networkOfflineDesc': '网络断开desc',
  'conn.backendOfflineElapsed': '已离线 ' + (p && p.t),
  'conn.backendRetryNow': '立即重试',
  'conn.backendSnooze': '暂时隐藏',
  'conn.backendRestored': '后端已恢复',
  'conn.backendRestoredDesc': '重新同步中',
  'conn.backendOfflineTitlePrefix': '【后端离线】',
  'conn.networkOfflineTitlePrefix': '【网络断开】',
}[k] || k);

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL backend_offline_monitor.js
const scenario = process.argv[3];

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
function fireTimeout(ms) {
  const it = [..._timeouts].reverse().find(x => !x.dead && x.ms === ms);
  if (it) { it.dead = true; it.fn(); return true; }
  return false;
}
function fireInterval(ms) {
  const it = [..._intervals].reverse().find(x => !x.dead && x.ms === ms);
  if (it) { it.fn(); return true; }
  return false;
}
function banner() {
  return _body.children.find(c => c.id === 'backend-offline-banner' && !c.removed) || null;
}

(async () => {
  if (scenario === 'A') {
    // ── Full offline → prominent banner → recovery resync ──
    _healthOk = false;
    _pushCbs.forEach(fn => fn({ ms: null, state: 'offline', connected: false, at: Date.now() }));
    await flush();
    check('A1_probe1_fired', _probeCount === 1);
    check('A2_no_banner_after_1_fail', banner() === null);
    check('A3_confirm_timer_armed', fireTimeout(4000));
    await flush();
    check('A4_banner_after_2_fails', banner() !== null);
    check('A5_title_prefixed', document.title.startsWith('【后端离线】'));
    check('A6_poll_interval_registered', _intervals.some(x => !x.dead && x.ms === 5000));
    check('A7_elapsed_ticker_registered', _intervals.some(x => !x.dead && x.ms === 1000));
    _healthOk = true;
    check('A8_poll_fires', fireInterval(5000));
    await flush();
    check('A9_banner_removed', banner() === null);
    check('A10_title_restored', document.title === 'Tofu');
    check('A11_recovery_probe_stuck', _probeStuck === 1);
    check('A12_recovery_convs', _recoverConv === 1);
    check('A13_revalidate', _revalidate === 1);
    check('A14_push_nudge', _pushConnectCalls >= 1);
    check('A15_toast', _toasts.length === 1 && _toasts[0].title === '后端已恢复');
  } else if (scenario === 'B') {
    // ── Proxy hiccup tolerance: 1 fail + 1 OK → NO banner ──
    _healthOk = false;
    _pushCbs.forEach(fn => fn({ ms: null, state: 'offline', connected: false, at: Date.now() }));
    await flush();
    check('B1_probe1_fired', _probeCount === 1);
    _healthOk = true;   // backend fine — the WS drop was a tunnel stutter
    check('B2_confirm_timer_armed', fireTimeout(4000));
    await flush();
    check('B3_no_banner', banner() === null);
    check('B4_title_untouched', document.title === 'Tofu');
    check('B5_state_back_online', window.BackendOfflineMonitor.phase === 'online');
    check('B6_no_recovery_actions', _probeStuck === 0 && _recoverConv === 0 && _pushConnectCalls === 0);
  } else if (scenario === 'C') {
    // ── Browser offline event → network-variant banner; online → recover ──
    global.navigator.onLine = false;
    _healthOk = false;
    (_winListeners['offline'] || []).forEach(fn => fn());
    await flush();
    check('C1_confirm_timer', fireTimeout(4000));
    await flush();
    check('C2_banner', banner() !== null);
    check('C3_network_title_prefix', document.title.startsWith('【网络断开】'));
    check('C4_network_desc', !!banner() && banner().innerHTML.includes('网络断开desc'));
    global.navigator.onLine = true;
    _healthOk = true;
    (_winListeners['online'] || []).forEach(fn => fn());
    await flush();
    check('C5_recovered', banner() === null && document.title === 'Tofu');
  } else if (scenario === 'D') {
    // ── Snooze hides the banner; elapsed ticker re-shows it after 60s ──
    _healthOk = false;
    _pushCbs.forEach(fn => fn({ ms: null, state: 'offline', connected: false, at: Date.now() }));
    await flush();
    fireTimeout(4000);
    await flush();
    check('D1_banner', banner() !== null);
    BackendOfflineMonitorSnooze();
    check('D2_snoozed_hidden', banner() === null);
    check('D3_still_offline_phase', window.BackendOfflineMonitor.phase === 'offline');
    _clock += 61000;
    check('D4_elapsed_tick_fires', fireInterval(1000));
    check('D5_reshown_after_snooze', banner() !== null);
  } else if (scenario === 'E') {
    // ── Vite lifecycle scope owns listeners/subscriptions/timers ──
    check('E1_scope_created', _scopeCreates === 1);
    check('E2_booted', window.BackendOfflineMonitor.booted === true);
    check('E3_window_listeners_owned',
      (_winListeners.offline || []).length === 1 && (_winListeners.online || []).length === 1);
    window.destroyBackendOfflineMonitor();
    check('E4_scope_destroyed', _scopeDestroys === 1);
    check('E5_listeners_released',
      (_winListeners.offline || []).length === 0 && (_winListeners.online || []).length === 0
      && (_docListeners.visibilitychange || []).length === 0);
    check('E6_reset', window.BackendOfflineMonitor.booted === false);
    window.initBackendOfflineMonitor();
    check('E7_restart_one_owner', _scopeCreates === 2
      && (_winListeners.offline || []).length === 1
      && (_winListeners.online || []).length === 1);
  } else if (scenario === 'F') {
    // ── Proxy auth denial is not evidence that the app process is offline ──
    _healthOk = false;
    _healthStatus = 401;
    _pushCbs.forEach(fn => fn({ ms: null, state: 'offline', connected: false, at: Date.now() }));
    await flush();
    check('F1_probe_fired', _probeCount === 1);
    check('F2_no_backend_banner', banner() === null);
    check('F3_title_untouched', document.title === 'Tofu');
    check('F4_state_back_online', window.BackendOfflineMonitor.phase === 'online');
    check('F5_no_confirm_timer', !fireTimeout(4000));
  }
  console.log(out.join('\n'));
})();
"""


def _run_harness(module_path: str, scenario: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_backend_offline_monitor_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, module_path, scenario],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


def _assert_scenario_green(module_path: str, scenario: str, min_pass: int) -> str:
    proc = _run_harness(module_path, scenario)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed (scenario {scenario}): {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'scenario {scenario} failures:\n{output}'
    assert output.count('PASS') >= min_pass, f'expected >={min_pass} PASS, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_offline_banner_then_recovery_resync():
    _assert_scenario_green(MODULE, 'A', 15)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_proxy_hiccup_never_raises_banner():
    _assert_scenario_green(MODULE, 'B', 6)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_browser_offline_event_network_variant():
    _assert_scenario_green(MODULE, 'C', 5)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_snooze_hides_then_reshows_banner():
    _assert_scenario_green(MODULE, 'D', 5)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_vite_lifecycle_scope_releases_and_can_restart():
    _assert_scenario_green(MODULE, 'E', 7)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_proxy_auth_denial_never_claims_backend_is_offline():
    _assert_scenario_green(MODULE, 'F', 5)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_confirm_gate_double_neuter(tmp_path):
    """DOUBLE-NEUTER: replace the 2-fail confirmation gate with an immediate
    alarm on the FIRST failed probe. Scenario B (proxy hiccup) must then FAIL
    because the banner appears on a mere tunnel stutter. Proves the gate is
    load-bearing. Shipped file untouched."""
    with open(MODULE, encoding='utf-8') as f:
        src = f.read()
    needle = "if (_bomState.fails >= _BOM_CONFIRM_FAILS) { _bomGoOffline(reason); return; }"
    assert needle in src, 'confirm-gate fragment drifted — update the neuter target'
    neutered = src.replace(needle, "if (true) { _bomGoOffline(reason); return; }", 1)
    copy = tmp_path / 'backend_offline_monitor_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(copy), 'B')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL B3_no_banner' in output, (
        'DOUBLE-NEUTER did not bite: banner still suppressed on a hiccup '
        'without the 2-fail confirmation gate.\n' + output
    )

    with open(MODULE, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped backend_offline_monitor.js'


# ── Registration guards (no node needed) ─────────────────────────────

def test_registered_in_bundle_manifest_after_push():
    name = 'core/backend_offline_monitor.js'
    names = runtime_section_names()
    assert name in names, f'{name} missing from the migrated Vite runtime'
    assert names.index('push.js') < names.index(name), (
        f'{name} must load AFTER push.js (it subscribes pushOnLatency at boot)'
    )


def test_index_has_no_raw_backend_monitor_script():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert 'static/js/core/backend_offline_monitor.js' not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html


def test_i18n_keys_present():
    locale_dir = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    sources = [open(os.path.join(locale_dir, name), encoding='utf-8').read()
               for name in ('zh.json', 'en.json')]
    for key in (
        'conn.backendOfflineTitle', 'conn.backendOfflineDesc',
        'conn.networkOfflineTitle', 'conn.networkOfflineDesc',
        'conn.backendOfflineElapsed', 'conn.backendRetryNow',
        'conn.backendSnooze', 'conn.backendRestored',
        'conn.backendRestoredDesc', 'conn.backendOfflineTitlePrefix',
        'conn.networkOfflineTitlePrefix',
    ):
        assert all(f'"{key}"' in src for src in sources), f'locales missing key {key}'

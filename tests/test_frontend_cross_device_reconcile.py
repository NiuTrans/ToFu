"""tests/test_frontend_cross_device_reconcile.py — regression for the
cross-device conversation-list drift fix (option 2: periodic visible-tab
reconciliation poll).

WHY
---
The conversation list is reconciled with the server by PULL only — there is
no cross-DEVICE push channel (BroadcastChannel is same-machine only; Epic B
push fan-out is §10-gated/parked). So two devices (a phone over a flaky VS Code
port-forward + a desktop) drift apart and only self-heal when the stale
device's tab REGAINS focus while idle (`visibilitychange`). A phone left
visible-but-untouched never re-pulls in the normal case — the only other
list-refresh interval (`_startOfflineRecoveryPolling`) is gated on the presence
of `server_offline` convs and no-ops otherwise.

THE FIX (static/js/core/cross_tab_sync.js)
------------------------------------------
`_crossDeviceReconcile()` fires on a fixed 25s cadence and re-pulls the list
via `loadConversationsFromServer()` — but ONLY under the exact same idle guard
the `conv_saved` cross-tab refresh already uses:

    document.visibilityState === "visible"
      AND activeStreams.size === 0
      AND _editingMsgIdx === null

and only when `window._bootLoadInFlight` is falsy — sharing that latch with the
boot-reconnect backoff and the 60s main.js timer so overlapping loads are
impossible. It reuses `loadConversationsFromServer` verbatim (no re-implemented
merge), whose 304 / count-drop / allowTruncate guards already prevent a stale
device from truncating fresher server state.

This harness loads the REAL shipped `cross_tab_sync.js` under bare node, stubs
the window globals + `loadConversationsFromServer`, and drives
`_crossDeviceReconcile()` directly under each condition:
  • idle             → fires exactly one reconciling load, latch set+cleared.
  • active stream    → suppressed.
  • hidden tab       → suppressed.
  • editing a msg    → suppressed.
  • latch held       → suppressed (shared in-flight guard).

SOURCE-LEVEL DOUBLE-NEUTER (performed in-harness on a MUTATED copy; the shipped
file is never modified):
  • Strip the idle guard block → `_crossDeviceReconcile()` now FIRES a load
    under the active-stream condition (it "fires when it shouldn't"), proving
    the guard is load-bearing.
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


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Window-scope globals cross_tab_sync.js touches at load + runtime ──
global._syncChannel = null;
global.TAB_ID = 'tab-test';
global.debugLog = () => {};
global.conversations = [];
global.activeStreams = new Map();
global._editingMsgIdx = null;
// addEventListener (window + document) must exist — the file registers
// visibilitychange / online listeners at load time.
global.addEventListener = () => {};
global.document = { visibilityState: 'visible', addEventListener: () => {} };
// Neutralise the trailing `setInterval(_crossDeviceReconcile, ...)` so it can't
// keep the node process alive or fire on its own — we drive the fn directly.
global.setInterval = () => 0;
global.setTimeout = (fn, ms) => 0;
global.clearInterval = () => {};
global.clearTimeout = () => {};

// Count reconciling loads.
let loadCalls = 0;
global.loadConversationsFromServer = async () => { loadCalls++; };
// The reconcile fn piggybacks the /api/v1/chat/active probe for its stale-pin
// sweep (Api.chat.active). Stub it so the fn doesn't throw ReferenceError.
global.Api = { chat: { active: async () => [] } };
global.pushIsConnected = () => false;
global._healStuckPlaceholder = () => false;
global.AbortSignal = { timeout: () => null };
global.requestIdleCallback = null;

const SRC = fs.readFileSync(process.argv[2], 'utf8');

function loadModule(src) {
  // Re-eval in global scope — the function declaration rebinds _crossDeviceReconcile.
  (0, eval)(src);
}

// Reset per-scenario state.
//
// ★ `_bootLoadInFlight` is now a self-healing LEASE: the source defines it as a
//   FUNCTION (predicate) plus _acquireBootLoad/_releaseBootLoad, and stores the
//   acquire TIMESTAMP (not a bare boolean) in window._bootLoadInFlight. So the
//   harness must NOT overwrite window._bootLoadInFlight with a boolean (that
//   would clobber the predicate the source reads). Release via the real helper;
//   simulate a HELD lease by acquiring one (fresh timestamp → predicate true).
function reset(state) {
  loadCalls = 0;
  _releaseBootLoad();
  global.document.visibilityState = state.vis || 'visible';
  global.activeStreams = state.streams || new Map();
  global._editingMsgIdx = ('editing' in state) ? state.editing : null;
  if (state.inFlight) _acquireBootLoad();
}

// Await microtasks so the async loadConversationsFromServer + .finally settle.
const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  loadModule(SRC);
  if (typeof _crossDeviceReconcile !== 'function') {
    console.log('FAIL fn_exposed _crossDeviceReconcile missing'); process.exit(0);
  }
  check('fn_exposed', true);

  // ══ 1. IDLE → fires exactly one load, latch set then cleared ══
  {
    reset({ vis: 'visible', streams: new Map(), editing: null });
    const ret = _crossDeviceReconcile();
    check('idle_returned_true', ret === true);
    check('idle_latch_set_sync', _bootLoadHeld() === true);
    await flush();
    check('idle_loaded_once', loadCalls === 1);
    check('idle_latch_cleared', _bootLoadHeld() === false);
  }

  // ══ 2. ACTIVE STREAM → suppressed ══
  {
    const streams = new Map(); streams.set('conv-1', { controller: {} });
    reset({ vis: 'visible', streams });
    const ret = _crossDeviceReconcile();
    await flush();
    check('stream_suppressed_ret', ret === false);
    check('stream_no_load', loadCalls === 0);
    check('stream_latch_untouched', _bootLoadHeld() === false);
  }

  // ══ 3. HIDDEN TAB → suppressed ══
  {
    reset({ vis: 'hidden', streams: new Map() });
    const ret = _crossDeviceReconcile();
    await flush();
    check('hidden_suppressed_ret', ret === false);
    check('hidden_no_load', loadCalls === 0);
  }

  // ══ 4. EDITING A MESSAGE → suppressed ══
  {
    reset({ vis: 'visible', streams: new Map(), editing: 3 });
    const ret = _crossDeviceReconcile();
    await flush();
    check('editing_suppressed_ret', ret === false);
    check('editing_no_load', loadCalls === 0);
  }

  // ══ 5. IN-FLIGHT LATCH HELD → suppressed (shared guard) ══
  {
    reset({ vis: 'visible', streams: new Map(), inFlight: true });
    const ret = _crossDeviceReconcile();
    await flush();
    check('inflight_suppressed_ret', ret === false);
    check('inflight_no_load', loadCalls === 0);
    check('inflight_latch_preserved', _bootLoadHeld() === true);
  }

  // ══ 6. DOUBLE-NEUTER: strip the idle guard → it fires when it shouldn't ══
  {
    // Remove the whole idle-guard `if (...) return false;` block.
    const GUARD = 'if (\n' +
      '    document.visibilityState !== "visible" ||\n' +
      '    activeStreams.size !== 0 ||\n' +
      '    _editingMsgIdx !== null\n' +
      '  )\n' +
      '    return false;';
    const neutered = SRC.replace(GUARD, '/* NEUTERED idle guard */');
    check('neuter_patch_applied', neutered !== SRC);
    loadModule(neutered);
    // Under an ACTIVE STREAM the guard would normally suppress; neutered, it fires.
    const streams = new Map(); streams.set('conv-1', { controller: {} });
    reset({ vis: 'hidden', streams, editing: 7 });  // every idle condition violated
    const ret = _crossDeviceReconcile();
    await flush();
    check('neuter_fires_when_it_should_not', ret === true && loadCalls === 1);
  }

  console.log('loadCalls(final)=' + loadCalls);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_cross_device_reconcile_idle_guard():
    harness = os.path.join(HERE, '_cross_device_reconcile_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'),  # argv[2]
             ],
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
    assert not fails, 'cross-device reconcile guard failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'

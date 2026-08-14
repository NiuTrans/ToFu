"""tests/test_frontend_myday_instant_paint.py — regression for "每次点击都不
立刻显示，要加载很久" (My Day day-click blocked the first paint on the network).

WHY
---
``_mydaySelectDay`` (static/js/myday.js) used to render from the in-memory
``_myday.cache`` ONLY; on a cold reload (in-memory empty) it showed a skeleton
and then *awaited* ``Api.daily.status(dateStr)`` before painting anything — a
blocking round-trip on every day-click, and no persistence across reloads.

THE FIX
-------
A per-day IndexedDB read cache (``_mydayIDB``) + an instant-paint path:
  1. in-memory full report → render immediately;
  2. else IndexedDB → paint from cache, THEN revalidate in the background;
  3. else skeleton.
The first paint NEVER blocks on the network, and the server remains the source
of truth (the background ``status`` call reconciles + rewrites the cache).

This harness loads the REAL shipped ``myday.js`` under bare node with a fake
``indexedDB`` (pre-seeded with a cached report), a stub ``document``, and a
CONTROLLABLE ``Api.daily.status`` that stays pending. It asserts the cached
report is painted into #mydayTasks BEFORE the status promise resolves and that
no skeleton was shown, then resolves status and asserts the server report
reconciles the view.

SOURCE-LEVEL NEGATIVE CONTROL (proven by hand; restore byte-identical):
  • Revert ``_mydaySelectDay`` to the old "in-memory only → skeleton → await
    status" flow → on a cold cache the cached render never appears before the
    network resolves and a skeleton IS shown → ``cached_paint_before_network``
    and ``no_skeleton_on_cache_hit`` FAIL.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Minimal fake IndexedDB (enough for _mydayIDB: open/get/put) ──
const _stores = {};
function _mkReq() { return { onsuccess: null, onerror: null, result: undefined }; }
global.indexedDB = {
  open(name, ver) {
    const req = { onupgradeneeded: null, onsuccess: null, onerror: null, result: null };
    Promise.resolve().then(() => {
      const db = {
        objectStoreNames: { contains: (n) => Object.prototype.hasOwnProperty.call(_stores, n) },
        createObjectStore: (n) => { _stores[n] = _stores[n] || new Map(); return {}; },
        transaction: (n, mode) => {
          const tx = { oncomplete: null, onerror: null };
          tx.objectStore = (sn) => ({
            get: (k) => {
              const r = _mkReq();
              Promise.resolve().then(() => {
                r.result = _stores[sn] ? _stores[sn].get(k) : undefined;
                if (r.onsuccess) r.onsuccess();
              });
              return r;
            },
            put: (v) => {
              const r = _mkReq();
              Promise.resolve().then(() => {
                (_stores[sn] = _stores[sn] || new Map()).set(v.date, v);
                if (r.onsuccess) r.onsuccess();
                if (tx.oncomplete) tx.oncomplete();
              });
              return r;
            },
          });
          return tx;
        },
      };
      if (req.onupgradeneeded) req.onupgradeneeded({ target: { result: db } });
      if (req.onsuccess) req.onsuccess({ target: { result: db } });
    });
    return req;
  },
};

// ── Stub document ──
const _els = {};
function _mkEl() {
  const el = { _html: '', textContent: '', style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    querySelectorAll: () => [] };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html; },
    set(v) { el._html = v; el._writes = el._writes || []; el._writes.push(v); },
  });
  el._writes = [];
  return el;
}
global.document = {
  getElementById: (id) => { if (!_els[id]) _els[id] = _mkEl(); return _els[id]; },
  querySelectorAll: () => [],
  addEventListener: () => {},
  readyState: 'complete',
};
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.t = (k) => k;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.showToast = () => {};
global.localStorage = { getItem: () => null, setItem: () => {} };

// ── Controllable status endpoint ──
let _resolveStatus = null;
let _statusCalls = 0;
global.Api = { daily: {
  status: () => { _statusCalls++; return new Promise((res) => { _resolveStatus = res; }); },
  calendar: async () => ({ days: {}, conv_days: {}, cost_days: {} }),
  convCount: async () => ({ count: 0 }),
} };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // myday.js (real)

if (typeof _mydaySelectDay !== 'function') {
  console.log('FAIL fn_exposed _mydaySelectDay missing'); console.log(out.join('\n')); process.exit(0);
}
check('fn_exposed', true);

// Compute the date string _mydaySelectDay will use (same formula as the module).
const now = new Date();
const y = now.getFullYear(), m = now.getMonth(), day = now.getDate();
const dateStr = `${y}-${String(m + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;

// Pre-seed the fake IDB with a cached report (simulates a prior session).
_stores['reports'] = new Map();
_stores['reports'].set(dateStr, { date: dateStr, cachedAt: Date.now(), report: {
  streams: [{ id: 'stream-cached', title: 'CACHED_STREAM', summary: '', status: 'in_progress', conv_ids: [], conv_count: 0 }],
  tomorrow: [], stats: { totalConversations: 1 },
} });

const tick = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const tasksEl = document.getElementById('mydayTasks');
  // Fire the click but DON'T await — status stays pending.
  const p = _mydaySelectDay(day);

  // Let the IDB read + instant paint flush (network still pending).
  await tick(30);

  const writesBeforeNet = (tasksEl._writes || []).join('\n');
  check('status_called', _statusCalls === 1);
  check('cached_paint_before_network', /CACHED_STREAM/.test(writesBeforeNet));
  check('no_skeleton_on_cache_hit', !/myday-task-skel/.test(writesBeforeNet));

  // Now the server responds with an authoritative (different) report.
  _resolveStatus({ status: 'done', report: {
    streams: [{ id: 'stream-server', title: 'SERVER_STREAM', summary: '', status: 'done', conv_ids: [], conv_count: 0 }],
    tomorrow: [], stats: { totalConversations: 2 },
  } });
  await p;
  await tick(20);

  const allWrites = (tasksEl._writes || []).join('\n');
  check('server_reconciled', /SERVER_STREAM/.test(allWrites));
  // The server report must also be written back into the persistent cache.
  const cached = _stores['reports'].get(dateStr);
  check('cache_rewritten', !!cached && /SERVER_STREAM/.test(JSON.stringify(cached.report)));

  console.log(out.join('\n'));
  process.exit(0);  // module-load schedules a 3h reminder timer; don't hang on it
})().catch((e) => { console.log('FAIL exception ' + (e && e.message)); console.log(out.join('\n')); process.exit(0); });
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_myday_instant_paint_from_cache():
    harness = os.path.join(HERE, '_myday_instant_paint_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'myday.js')],
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
    assert not fails, 'My Day instant-paint failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


# ════════════════════════════════════════════════════════════════════
#  Quick-action launch: the pre-fill must land BEFORE newChat()
#
#  Root cause (owner-caught on the project-brain createConv launcher, same
#  disease here): newChat() measures hasInput from the composer and, finding
#  it EMPTY, clears the project attachment + tool config
#  (_clearProjectStateLocal / _resetToolsToDefaults). The old launch order
#  called newChat() first and pre-filled after, so a quick action fired from
#  a project conversation always lost the project — while a comment claimed
#  the project "stays". The spy below records what the composer held AT THE
#  MOMENT newChat ran: a call-counting spy is exactly how this hid.
# ════════════════════════════════════════════════════════════════════

_LAUNCH_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stub document — the composer needs a settable value + style + focus.
const _els = {};
global.document = {
  getElementById: (id) => {
    if (!_els[id]) {
      _els[id] = { value: '', textContent: '', style: {}, scrollHeight: 10,
        classList: { add() {}, remove() {}, contains() { return false; } },
        querySelectorAll: () => [], focus() {}, addEventListener() {} };
    }
    return _els[id];
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
  readyState: 'complete',
};
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.t = (k) => k;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.showToast = () => {};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.Api = { daily: { status: async () => ({}), calendar: async () => ({}),
  convCount: async () => ({ count: 0 }) } };
global.indexedDB = { open: () => ({ set onsuccess(f) {}, set onerror(f) {},
  set onupgradeneeded(f) {} }) };

// ORDER-SENSITIVE newChat spy: record the composer content AT INVOCATION
// TIME — the real newChat clears the project when the composer is empty
// then, so the launcher must have pre-filled already.
let composerAtNewChat = null;
let newChatCalls = 0;
global.newChat = () => {
  newChatCalls++;
  if (composerAtNewChat === null) {
    composerAtNewChat = document.getElementById('userInput').value;
  }
};
global.projectState = { active: true, path: '/proj/real' };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // myday.js (real)

if (typeof _mydayLaunchConvFromAction !== 'function') {
  console.log('FAIL fn_exposed _mydayLaunchConvFromAction missing');
  console.log(out.join('\n')); process.exit(0);
}
check('fn_exposed', true);

_mydayLaunchConvFromAction({
  text: 'fallback text',
  quick_action: { prefill: 'PREFILLED PROMPT', projectEnabled: true,
    searchMode: 'multi', fetchEnabled: true },
});

check('newchat_called_once', newChatCalls === 1);
check('prefill_before_newchat', composerAtNewChat === 'PREFILLED PROMPT');
check('composer_holds_prefill',
      document.getElementById('userInput').value === 'PREFILLED PROMPT');

console.log(out.join('\n'));
process.exit(0);  // module-load schedules a 3h reminder timer; don't hang on it
"""


def _run_launch(myday_src):
    harness = os.path.join(HERE, '_myday_launch_harness.js')
    with open(harness, 'w') as f:
        f.write(_LAUNCH_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, myday_src],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_myday_quick_action_prefills_before_newchat():
    """The shipped myday.js pre-fills the composer BEFORE invoking newChat,
    so newChat's empty-composer branch never strips the project attachment."""
    output = _run_launch(os.path.join(JS_DIR, 'myday.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'My Day launch-order failures:\n' + output
    for must in ('PASS fn_exposed', 'PASS newchat_called_once',
                 'PASS prefill_before_newchat', 'PASS composer_holds_prefill'):
        assert must in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_myday_launch_order_is_load_bearing():
    """NC: strip the pre-fill in a COPY (the degenerate form of the inverted
    order — composer empty when newChat runs) → the spy records an empty
    composer at newChat time → prefill_before_newchat FAILS. Shipped file
    untouched."""
    src = os.path.join(JS_DIR, 'myday.js')
    with open(src, encoding='utf-8') as f:
        original = f.read()
    anchor = "    input.value = qa.prefill || item.text || '';"
    assert anchor in original, 'pre-fill anchor not found (source changed?)'
    patched = original.replace(
        anchor, "    if (false) input.value = qa.prefill || item.text || '';  // NC", 1)
    copy_path = os.path.join(HERE, '_myday_launch_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_launch(copy_path)
        assert 'FAIL prefill_before_newchat' in output, \
            ('NC: without a pre-fill before newChat, the order spy must '
             'record an empty composer:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(src, encoding='utf-8') as f:
        assert f.read() == original, 'shipped myday.js must be byte-identical'


def test_newchat_empty_composer_contract_holds():
    """The fix rides newChat's OWN contract: a non-empty composer keeps the
    project + tool config armed. Pin that contract at its source — if newChat
    ever stops honouring it, BOTH launchers (myday + project-brain createConv)
    silently regress, and this guard forces the re-review."""
    lifecycle = os.path.join(JS_DIR, 'main', 'main_conv_lifecycle.js')
    with open(lifecycle, encoding='utf-8') as f:
        src = f.read()
    assert 'if (!hasInput) {' in src, 'newChat hasInput branch not found'
    branch = src.index('if (!hasInput) {')
    window = src[branch:branch + 200]
    assert '_clearProjectStateLocal();' in window, \
        'newChat no longer clears the project on an empty composer — the ' \
        'prefill-first launchers must be re-reviewed against the new contract'

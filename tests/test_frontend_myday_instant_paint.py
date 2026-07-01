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

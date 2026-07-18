"""Regression: `ConvCache.put()` must RESOLVE on a transaction abort
(QuotaExceededError) — otherwise the durable poor-network send-rescue hangs.

WHY
---
`static/js/idb-cache.js` `put()` is the durable local write behind the
poor-network send-rescue (`markConvPendingSync` → `await ConvCache.put(conv)`).
The write transaction wired `tx.oncomplete` and `tx.onerror` but NO
`tx.onabort`. A `QuotaExceededError` (storage pressure) ABORTS the transaction
WITHOUT bubbling as a request `onerror` — only `onabort` fires. Without that
handler:
  1. the `await ConvCache.put(conv)` promise NEVER resolves → the rescue caller
     hangs (the message is neither on the server nor confirmed-durable), and
  2. no reactive `evict()` runs, so the NEXT write hits the same full quota.

THE FIX
-------
`put()` now:
  • wires `tx.onabort` → logs, fires a reactive `evict()`, and resolves;
  • routes `oncomplete` / `onerror` / `onabort` through a single-resolve latch
    (`_done`) so a request-error that bubbles to `onerror` AND then aborts the
    tx resolves the promise exactly once.

CHECKS (drive the REAL shipped idb-cache.js under node against a fake IDB)
--------------------------------------------------------------------------
(A) A `put()` whose transaction ABORTS (QuotaExceeded) RESOLVES within a hard
    deadline — the load-bearing fix (before it: the promise hangs forever).
(B) The abort triggers a reactive evict() (a second transaction is opened after
    the aborted one).
(C) A normal `put()` still resolves via oncomplete (no regression).

DOUBLE-NEUTER: strip the `tx.onabort` handler on a COPY of idb-cache.js → (A)
times out (promise never resolves) → the harness reports the hang. Shipped file
left byte-identical.

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


# A minimal fake IndexedDB with a working transaction event model. The store's
# behaviour is switched per-open: the FIRST readwrite tx after `armAbort()`
# fires tx.onabort (mimicking QuotaExceeded); all others complete normally. The
# harness reads idb-cache.js from argv[2] so a neutered COPY can be swapped in.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.navigator = {};       // no storage.estimate/persist — skip quota probe
global.console = console;

// ── microtask queue helper (transactions settle async, like real IDB) ──
function soon(fn) { Promise.resolve().then(fn); }

let _abortNextRW = false;    // when true, the next readwrite tx aborts
let _rwTxCount = 0;          // number of readwrite transactions opened

function FakeRequest() { this.onsuccess = null; this.onerror = null; this.result = undefined; }

function FakeStore() {}
FakeStore.prototype.get = function () {
  const req = new FakeRequest();
  soon(() => { req.result = undefined; if (req.onsuccess) req.onsuccess(); });
  return req;
};
FakeStore.prototype.put = function () { return new FakeRequest(); };
FakeStore.prototype.delete = function () { return new FakeRequest(); };
FakeStore.prototype.clear = function () { return new FakeRequest(); };
FakeStore.prototype.count = function () {
  const req = new FakeRequest();
  soon(() => { req.result = 0; if (req.onsuccess) req.onsuccess(); });
  return req;
};
FakeStore.prototype.index = function () {
  return { openCursor: function () { const r = new FakeRequest(); soon(() => { r.result = null; if (r.onsuccess) r.onsuccess({ target: r }); }); return r; } };
};
FakeStore.prototype.openCursor = function () {
  const r = new FakeRequest(); soon(() => { r.result = null; if (r.onsuccess) r.onsuccess({ target: r }); }); return r;
};

function FakeTx(mode) {
  this.mode = mode; this.error = null;
  this.oncomplete = null; this.onerror = null; this.onabort = null;
  const isRW = mode === 'readwrite';
  if (isRW) _rwTxCount++;
  const willAbort = isRW && _abortNextRW;
  if (willAbort) _abortNextRW = false;   // one-shot
  const self = this;
  // After the metaReq.onsuccess microtask has run (the put() body wires
  // oncomplete/onerror/onabort inside it), settle the tx.
  soon(() => soon(() => {
    if (willAbort) {
      self.error = { name: 'QuotaExceededError' };
      if (self.onabort) self.onabort();
    } else {
      if (self.oncomplete) self.oncomplete();
    }
  }));
}
FakeTx.prototype.objectStore = function () { return new FakeStore(); };

function FakeDB() { this.objectStoreNames = { contains: () => true }; this.onclose = null; }
FakeDB.prototype.transaction = function (stores, mode) { return new FakeTx(mode || 'readonly'); };

global.indexedDB = {
  open: function () {
    const req = new FakeRequest();
    req.onupgradeneeded = null; req.onblocked = null;
    soon(() => { req.result = new FakeDB(); if (req.onsuccess) req.onsuccess({ target: req }); });
    return req;
  },
  deleteDatabase: function () { return {}; },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL idb-cache.js → defines ConvCache

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Resolve a put() against a hard deadline: if the promise never settles (the
// bug), the deadline wins and we report a HANG.
function withDeadline(promise, ms, label) {
  return Promise.race([
    promise.then(() => ({ ok: true })),
    new Promise((res) => setTimeout(() => res({ ok: false, label }), ms)),
  ]);
}

const conv = {
  id: 'c-quota',
  title: 'poor network',
  messages: [{ role: 'user', content: 'durable rescue', _msgId: 'u1' }],
  updatedAt: 1700000000000,
};

(async () => {
  if (typeof ConvCache !== 'object' || typeof ConvCache.put !== 'function') {
    console.log('FAIL convcache_missing'); process.exit(0);
  }

  // Let the pre-warm _open()/evict() microtasks settle first.
  await new Promise((r) => setTimeout(r, 20));
  const rwBefore = _rwTxCount;

  // (A) Arm the next readwrite tx to ABORT (QuotaExceeded) and put(). The
  //     promise MUST resolve within the deadline — before the fix it hangs.
  _abortNextRW = true;
  const r1 = await withDeadline(ConvCache.put(conv), 2000, 'put_hang');
  check('put_resolves_on_abort', r1.ok === true);

  // (B) The abort must have triggered a reactive evict() → at least one more
  //     readwrite transaction opened after the aborted put's own tx.
  await new Promise((r) => setTimeout(r, 30));
  check('reactive_evict_fired', _rwTxCount > rwBefore + 1);

  // (C) A normal put() still resolves via oncomplete (no regression).
  const r2 = await withDeadline(ConvCache.put(conv), 2000, 'put_hang_normal');
  check('normal_put_resolves', r2.ok === true);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(idb_js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_idb_onabort_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, idb_js_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_put_resolves_and_evicts_on_quota_abort():
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    proc = _run_harness(idb_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'idb-cache put onabort failures:\n' + output
    assert output.count('PASS') >= 3, f'expected >=3 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_put_onabort_double_neuter(tmp_path):
    """DOUBLE-NEUTER: strip the `tx.onabort` handler on a COPY of idb-cache.js →
    the aborted put's promise never resolves → (A) reports a HANG. Proves the
    handler is load-bearing. Shipped file left byte-identical."""
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    with open(idb_js, encoding='utf-8') as f:
        src = f.read()

    marker = '            tx.onabort = function () {'
    start = src.index(marker)
    end = src.index('            };', start) + len('            };\n')
    assert start > 0 and end > start, 'tx.onabort block not found — anchor drifted'
    neutered = src[:start] + src[end:]
    assert marker not in neutered, 'neuter failed to remove the tx.onabort handler'

    copy = tmp_path / 'idb_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL put_resolves_on_abort' in output, (
        'DOUBLE-NEUTER did not bite: put() still resolved on abort without the '
        'tx.onabort handler.\n' + output
    )

    with open(idb_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped idb-cache.js'

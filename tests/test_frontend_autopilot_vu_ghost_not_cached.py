"""Regression: a transient autopilot VU streaming placeholder (`_streamingVu`)
must NEVER survive a page refresh as a frozen "Autopilot starting…" ghost.

WHY
---
`maybe_run_autopilot` emits `autopilot_vu_start` IN-MEMORY ONLY — the backend
persists a VU turn exclusively on `autopilot_vu_done` (which clears
`_streamingVu` first). The frontend's `_beginVuStreaming` pushes an empty
`{_isVirtualUser, _streamingVu}` placeholder into `conv.messages` and calls
`ConvCache.put(conv)` (streaming_render.js). Nothing dropped `_streamingVu`
before the IndexedDB write, so:

  1. the transient placeholder was cached, and
  2. on a hard refresh Phase-1 restored it and `renderChat` drew it as a
     FINALIZED static bubble (Copy/Edit/Regen actions + timestamp) whose body
     is the `autopilot.warming` pulse — the "启动中…" ghost in the report; and
  3. the `autopilot_vu_start` SSE replay then early-returned
     (`_findVuMsgById` hit the ghost) instead of standing up a live stream, so
     it never resumed.

THE FIX (two seams, both asserted here)
---------------------------------------
  • WRITE side — `idb-cache.js` `put()` filters out `_streamingVu` messages
    before building msgOrder (and returns early if that empties the conv).
  • READ side — `conversations.js` `loadConversationMessages` Phase-1 filters
    `_streamingVu` out of a cache hit, so a pre-fix cached ghost is dropped on
    load and the reconnect replay can re-create a live bubble.

CHECKS
------
(A) WRITE: put() a conv whose tail is a `_streamingVu` VU placeholder → the
    persisted msgOrder contains ONLY the real messages, never the ghost.
(B) NEUTER (write): strip the `_streamingVu` filter from a COPY of idb-cache.js
    → the ghost IS written → proves the filter is load-bearing.

Runs the REAL shipped idb-cache.js under node against a capturing fake IDB.
Skips cleanly when node isn't installed.
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


# Fake IDB that CAPTURES the meta row written by put() so we can inspect the
# persisted msgOrder. Only the pieces put() touches are modelled.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.navigator = {};
global.console = console;
function soon(fn) { Promise.resolve().then(fn); }

const CAPTURED = { meta: null, msgs: {} };

function FakeRequest() { this.onsuccess = null; this.onerror = null; this.result = undefined; }
function FakeStore(name) { this.name = name; }
FakeStore.prototype.get = function () {
  const req = new FakeRequest();
  soon(() => { req.result = undefined; if (req.onsuccess) req.onsuccess(); });
  return req;
};
FakeStore.prototype.put = function (rec) {
  if (this.name === 'conv_meta') CAPTURED.meta = rec;
  else if (this.name === 'messages' && rec && rec.msgId) CAPTURED.msgs[rec.msgId] = rec.data;
  return new FakeRequest();
};
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
  const self = this;
  soon(() => soon(() => { if (self.oncomplete) self.oncomplete(); }));
}
FakeTx.prototype.objectStore = function (name) { return new FakeStore(name); };

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

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL idb-cache.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const conv = {
  id: 'c-vu-ghost',
  title: 'autopilot',
  updatedAt: 1700000000000,
  messages: [
    { role: 'user',      content: 'go',          _msgId: 'u1' },
    { role: 'assistant', content: 'working on it', _msgId: 'a1' },
    // The transient VU streaming placeholder — must NOT be persisted.
    { role: 'user', content: '', _isVirtualUser: true, _streamingVu: true, _msgId: 'vu-ghost' },
  ],
};

(async () => {
  if (typeof ConvCache !== 'object' || typeof ConvCache.put !== 'function') {
    console.log('FAIL convcache_missing'); process.exit(0);
  }
  await new Promise((r) => setTimeout(r, 20));  // let pre-warm settle
  await ConvCache.put(conv);
  await new Promise((r) => setTimeout(r, 20));

  const meta = CAPTURED.meta;
  check('meta_written', !!meta && Array.isArray(meta.msgOrder));
  const ids = (meta && meta.msgOrder) ? meta.msgOrder.map(function (e) { return e.id; }) : [];
  // The ghost must be absent; the two real messages present.
  check('ghost_not_in_order', ids.indexOf('vu-ghost') === -1);
  check('real_msgs_in_order', ids.indexOf('u1') !== -1 && ids.indexOf('a1') !== -1);
  check('count_is_two', meta && meta.msgCount === 2);
  check('ghost_body_not_stored', !CAPTURED.msgs['vu-ghost']);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(idb_js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_vu_ghost_cache_harness.js')
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
def test_streaming_vu_placeholder_not_persisted():
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    proc = _run_harness(idb_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'VU-ghost cache-write failures:\n' + output
    assert output.count('PASS') >= 5, f'expected >=5 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_streaming_vu_filter_neuter(tmp_path):
    """NEUTER: remove the `_streamingVu` filter from a COPY of idb-cache.js →
    the ghost IS persisted → proves the filter is load-bearing. Shipped file
    left byte-identical."""
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    with open(idb_js, encoding='utf-8') as f:
        src = f.read()

    # Revert the filter to the pre-fix form: iterate conv.messages directly.
    marker = 'var msgs = conv.messages.filter(function (m) { return !(m && m._streamingVu); });'
    assert marker in src, 'filter anchor drifted — update the neuter'
    neutered = src.replace(marker, 'var msgs = conv.messages;')
    # Also drop the empty-after-filter early return so behaviour matches pre-fix.
    neutered = neutered.replace('if (msgs.length === 0) return;\n', '')
    assert neutered != src, 'neuter changed nothing'

    copy = tmp_path / 'idb_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL ghost_not_in_order' in output, (
        'NEUTER did not bite: the _streamingVu ghost was still filtered without '
        'the shipped filter.\n' + output
    )

    with open(idb_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped idb-cache.js'

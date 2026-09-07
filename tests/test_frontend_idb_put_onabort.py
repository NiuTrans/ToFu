"""The IndexedDB metadata cache put resolves on transaction abort.

WHY
---
A `QuotaExceededError` can abort the IndexedDB transaction without bubbling as
a request `onerror`. The catalog cache is optional, so this failure must settle
the promise and leave TurnStore/server authority untouched.

THE FIX (current owner)
-----------------------
The Vite migration retired ``static/js/ui/idb-cache.js``. The same behavior now
lives in ``frontend/src/core/conversation-metadata-cache.ts``: the IndexedDB
storage's ``putMetadata`` wires both ``transaction.onerror`` and
``transaction.onabort`` to a best-effort resolution.

CHECKS (drive the REAL shipped conversation-metadata-cache.ts, bundled to an
IIFE by the repo's ``vite_test_bundle`` adapter, under node against a fake IDB)
--------------------------------------------------------------------------
(A) A put whose transaction ABORTS (QuotaExceeded) RESOLVES within a hard
    deadline — the load-bearing fix (before it: the promise hangs forever).
(B) The attempted write is metadata-only: the bounded cache row plus its
    ``cacheKey``, with no arbitrary conversation fields leaking through.
(C) A normal put still resolves via oncomplete.

DOUBLE-NEUTER: strip the ``putMetadata`` ``transaction.onabort`` handler on a
COPY of the compiled bundle → (A) times out (promise never resolves) → the
harness reports the hang. Shipped source left byte-identical.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'frontend/src/core/conversation-metadata-cache.ts'


def _node_available() -> bool:
    return bool(shutil.which('node'))


# A minimal fake IndexedDB with a working transaction event model. The store's
# behaviour is switched per-open: the FIRST readwrite tx after `armAbort()`
# fires tx.onabort (mimicking QuotaExceeded); all others complete normally. The
# harness reads the compiled owner from argv[1] so a neutered COPY can be
# swapped in.
_HARNESS = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

// ── microtask queue helper (transactions settle async, like real IDB) ──
function soon(fn) { Promise.resolve().then(fn); }

let _abortNextRW = false;    // when true, the next readwrite tx aborts
let _rwTxCount = 0;          // number of readwrite transactions opened
let _lastPutValue = null;

function FakeRequest() {
  this.onsuccess = null; this.onerror = null;
  this.onupgradeneeded = null; this.onblocked = null;
  this.result = undefined;
}

function FakeStore() {}
FakeStore.prototype.createIndex = function () { return {}; };
FakeStore.prototype.get = function () {
  const req = new FakeRequest();
  soon(() => { req.result = undefined; if (req.onsuccess) req.onsuccess(); });
  return req;
};
FakeStore.prototype.put = function (value) {
  _lastPutValue = value;
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
  return {
    openCursor: function () {
      const req = new FakeRequest();
      soon(() => { req.result = null; if (req.onsuccess) req.onsuccess({ target: req }); });
      return req;
    },
  };
};
FakeStore.prototype.openCursor = function () {
  const req = new FakeRequest();
  soon(() => { req.result = null; if (req.onsuccess) req.onsuccess({ target: req }); });
  return req;
};

function FakeTx(mode) {
  this.mode = mode; this.error = null;
  this.oncomplete = null; this.onerror = null; this.onabort = null;
  const isRW = mode === 'readwrite';
  if (isRW) _rwTxCount += 1;
  const willAbort = isRW && _abortNextRW;
  if (willAbort) _abortNextRW = false;   // one-shot
  const self = this;
  // putMetadata() wires oncomplete/onerror/onabort synchronously after the
  // transaction is created, so settle on a later microtask.
  soon(() => soon(() => {
    if (willAbort) {
      self.error = { name: 'QuotaExceededError' };
      if (self.onabort) self.onabort();
    } else if (self.oncomplete) {
      self.oncomplete();
    }
  }));
}
FakeTx.prototype.objectStore = function () { return new FakeStore(); };

function FakeDB() { this.objectStoreNames = []; this.onversionchange = null; }
FakeDB.prototype.createObjectStore = function () { return new FakeStore(); };
FakeDB.prototype.deleteObjectStore = function () {};
FakeDB.prototype.transaction = function (stores, mode) { return new FakeTx(mode || 'readonly'); };

globalThis.indexedDB = {
  open: function () {
    const req = new FakeRequest();
    soon(() => {
      req.result = new FakeDB();
      if (req.onupgradeneeded) req.onupgradeneeded({ oldVersion: 0 });
      if (req.onsuccess) req.onsuccess({ target: req });
    });
    return req;
  },
  deleteDatabase: function () { return new FakeRequest(); },
};

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

const storage = createIndexedDbConversationMetadataCacheStorage(globalThis.indexedDB);
const cache = createConversationMetadataCache({
  storage,
  resolveOwnerId: () => 1,
  now: () => 1700000000000,
});

const conv = {
  id: 'c-quota',
  title: 'metadata row',
  _serverTurnCount: 1,
  updatedAt: 1700000000000,
  // Arbitrary non-metadata fields that must NOT reach IndexedDB.
  messages: [{ role: 'user', content: 'secret transcript' }],
  tools: ['run_command'],
  transcript: 'full transcript',
};

(async () => {
  if (typeof createIndexedDbConversationMetadataCacheStorage !== 'function'
      || typeof createConversationMetadataCache !== 'function') {
    console.log('FAIL owner_missing'); process.exit(0);
  }

  // Let the pre-warm openDatabase() microtasks settle first.
  await new Promise((r) => setTimeout(r, 20));
  const rwBefore = _rwTxCount;

  // (A) Arm the next readwrite tx to ABORT (QuotaExceeded) and put(). The
  //     promise MUST resolve within the deadline — before the fix it hangs.
  _abortNextRW = true;
  const r1 = await withDeadline(cache.put(conv), 2000, 'put_hang');
  check('put_resolves_on_abort', r1.ok === true);

  // (B) The attempted write is metadata-only (bounded row + cacheKey); no
  //     transcript/tools/messages fields may leak through.
  const keys = Object.keys(_lastPutValue).sort();
  const expected = ['cacheKey', 'cachedAt', 'createdAt', 'id', 'msgCount',
                    'ownerId', 'rev', 'settings', 'title', 'updatedAt'].sort();
  check('metadata_row_shape', JSON.stringify(keys) === JSON.stringify(expected));
  check('no_transcript_leak', !!_lastPutValue && !!_lastPutValue.settings
        && _lastPutValue.settings.messages === undefined
        && _lastPutValue.settings.tools === undefined
        && _lastPutValue.settings.transcript === undefined);

  // (C) A normal put() still resolves via oncomplete (no regression).
  const r2 = await withDeadline(cache.put(conv), 2000, 'put_hang_normal');
  check('normal_put_resolves', r2.ok === true);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def _run_harness(bundle: str, *, neutered: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['node', '-e', _HARNESS, neutered or bundle],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def _neuter_put_onabort(src: str) -> str:
    """Remove the ``putMetadata`` ``transaction.onabort`` wiring from a COPY.

    The compiled owner contains three identical ``transaction.onabort = () =>
    resolve();`` lines (putMetadata, remove, clearOwner). Target only the one
    in putMetadata by locating its unique ``store.put(storedRow(row));`` line
    and removing the matching onabort line that follows within the same block.
    """
    lines = src.splitlines(keepends=True)
    for index, line in enumerate(lines):
        # `replaceSidebar` also contains `store.put(storedRow(row));` inside a
        # for-loop; only putMetadata has it as a standalone statement.
        if line.strip() != 'store.put(storedRow(row));':
            continue
        for offset in range(1, 8):
            candidate_index = index + offset
            if candidate_index >= len(lines):
                break
            if 'transaction.onabort = () => resolve();' in lines[candidate_index]:
                del lines[candidate_index]
                return ''.join(lines)
        break
    raise AssertionError(
        'putMetadata onabort wiring not found in the compiled '
        'conversation-metadata-cache bundle'
    )


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_metadata_put_resolves_on_quota_abort():
    bundle = native_module_path('conversation-metadata-cache.js', SOURCE)
    proc = _run_harness(bundle)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'metadata-cache put onabort failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_put_onabort_double_neuter(tmp_path):
    """DOUBLE-NEUTER: strip the putMetadata `transaction.onabort` handler on a
    COPY of the compiled owner → the aborted put's promise never resolves →
    (A) reports a HANG. Proves the handler is load-bearing. Shipped source left
    byte-identical."""
    bundle = native_module_path('conversation-metadata-cache.js', SOURCE)
    with open(bundle, encoding='utf-8') as f:
        src = f.read()

    neutered = _neuter_put_onabort(src)
    assert 'transaction.onabort = () => resolve();' in neutered, (
        'neuter removed the wrong/multiple onabort handlers — putMetadata is '
        'no longer isolated'
    )
    copy = tmp_path / 'conversation-metadata-cache_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(bundle, neutered=str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL put_resolves_on_abort' in output, (
        'DOUBLE-NEUTER did not bite: put() still resolved on abort without the '
        'putMetadata onabort handler.\n' + output
    )

    with open(bundle, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversation-metadata-cache bundle'

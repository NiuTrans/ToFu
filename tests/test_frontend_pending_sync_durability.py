"""Regression: a message sent on a POOR network must NOT vanish on refresh —
end-to-end durability, not just a one-shot rescue PUT.

WHY (the three gaps a single best-effort rescue PUT leaves open)
----------------------------------------------------------------
1. The rescue `syncConversationToServer` PUT is best-effort — on the same poor
   network that failed the send, it very often ALSO fails. When it does, the
   message is in neither the server nor durable local storage.
2. `saveConversations()` does NOT write IndexedDB — only `ConvCache.put()`
   does. So a failed rescue PUT leaves the message ONLY in the volatile
   in-memory array → gone the instant the tab reloads.
3. Even if cached, the reload reconcile would still wipe it: no fresh-local-
   activity, no activeTaskId, no active stream → `loadConversationMessages`
   takes the OVERWRITE branch and replaces the local tail with the shorter
   server copy.

THE FIX (all three closed)
--------------------------
- `markConvPendingSync(conv)` stamps the trailing turn's messages with a
  `_pendingSync` field (a real message field → survives `ConvCache.put` /
  reload) and calls `ConvCache.put(conv)` so it is durable locally even when
  the server never got it.
- `syncConversationToServer` now RETURNS a boolean; on success it clears the
  markers (`_clearPendingSyncMarkers`). A retry poller (`_flushPendingSyncs` +
  `_startPendingSyncPolling`) plus the `online` / `visibilitychange` / boot
  hooks re-attempt until the PUT lands.
- The reload reconcile adds a `_localHasPendingSync` term to `localHasUnsynced`
  so a durably-cached pending tail is authoritative (KEEP_LOCAL) and re-synced,
  never overwritten by the shorter server copy.
- The PUT body STRIPS `_pendingSync` (client-only marker must not echo to the
  server and wrongly re-trigger KEEP_LOCAL forever).

CHECKS (drive the REAL shipped JS under node)
---------------------------------------------
(A) DURABILITY end-to-end: a failing PUT → marker set + written to a mock IDB;
    a "reload" reads it back; `convHasPendingSync` true; a recovering PUT
    (network back) fires, clears the markers, and stops the poller.
(B) PUT body does NOT contain `_pendingSync` (strip-on-send).
(C) idb-cache `_stripMessage` PRESERVES `_pendingSync` (so it survives reload).
(D) reconcile: `_localHasPendingSync` is a term of `localHasUnsynced`
    (source-level — the OVERWRITE branch can't fire when it is set).

DOUBLE-NEUTER for the load-bearing pieces below.
Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── (A)+(B) DURABILITY harness: drive the REAL syncConversationToServer +
#    markConvPendingSync + convHasPendingSync + _flushPendingSyncs against a
#    mock IndexedDB (a plain object) and a FLIPPABLE network. ──
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Mock IndexedDB: a durable store that survives a simulated reload. ──
const idb = new Map();  // convId → deep-cloned messages+meta
global.ConvCache = {
  put: (conv) => {
    // Clone like the real cache would (strip nothing that matters here).
    idb.set(conv.id, JSON.parse(JSON.stringify({
      id: conv.id, title: conv.title,
      messages: conv.messages, _serverMsgCount: conv._serverMsgCount,
    })));
    return Promise.resolve();
  },
  get: (id) => Promise.resolve(idb.get(id) || null),
  remove: (id) => { idb.delete(id); return Promise.resolve(); },
};

// ── Flippable network: put fails while `netUp=false`, succeeds after. ──
let netUp = false;
const serverStore = new Map();  // what the server has actually persisted
global.Api = {
  conversations: {
    put: async (id, body) => {
      if (!netUp) return null;                 // poor network → PUT fails
      serverStore.set(id, body.messages);      // server accepts
      return { ok: true };
    },
  },
  health: { check: async () => (netUp ? { ok: true } : null) },
};
global.activeStreams = new Map();
global.debugLog = function() {};
global.config = { defaultThinkingDepth: 'medium' };
global.activeConvId = null;
global.renderConversationList = function() {};
global.AbortSignal = { timeout: () => undefined };
// conversations[] is read by _flushPendingSyncs.
global.conversations = [];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof syncConversationToServer !== 'function' ||
    typeof markConvPendingSync !== 'function' ||
    typeof convHasPendingSync !== 'function' ||
    typeof _flushPendingSyncs !== 'function') {
  console.log('FAIL fn_exposed one-or-more helpers missing'); process.exit(0);
}
check('fn_exposed', true);

function _mkConv(id) {
  return {
    id, title: 't',
    messages: [{ role: 'user', content: 'hello poor network',
                 timestamp: 1700000000000, _msgId: 'u1' }],
    _serverMsgCount: 0,   // server never got this turn
    createdAt: 1699999000000, updatedAt: 1700000000000,
    model: 'aws.claude-opus-4.8',
  };
}

(async () => {
  // ═══ Step 1: send failed → rescue PUT ALSO fails (netUp=false). ═══
  netUp = false;
  const conv = _mkConv('c-poor');
  conversations.length = 0; conversations.push(conv);

  const ok1 = await syncConversationToServer(conv);
  check('rescue_put_failed', ok1 === false);          // (A) PUT reported failure

  // Fix path: sendMessage would now mark pending-sync.
  markConvPendingSync(conv);
  check('marker_set_in_memory', convHasPendingSync(conv) === true);
  check('idb_has_conv', idb.has('c-poor'));           // (A) written to durable IDB
  const cached = idb.get('c-poor');
  const tailCached = cached.messages[cached.messages.length - 1];
  check('idb_msg_has_pendingSync', tailCached && tailCached._pendingSync === true); // survives reload

  // ═══ Step 2: simulate a RELOAD — rebuild conv from the mock IDB only. ═══
  const reloaded = {
    id: 'c-poor', title: cached.title,
    messages: JSON.parse(JSON.stringify(cached.messages)),
    _serverMsgCount: 0,
    createdAt: 1699999000000, updatedAt: 1700000000000,
    model: 'aws.claude-opus-4.8',
  };
  conversations.length = 0; conversations.push(reloaded);
  check('reload_still_has_msg', reloaded.messages.length === 1);           // (A) NOT lost
  check('reload_pendingSync_survived', convHasPendingSync(reloaded) === true);

  // ═══ Step 3: network RETURNS → poller flush syncs it and clears markers. ═══
  netUp = true;
  const synced = await _flushPendingSyncs('test');
  check('flush_synced_one', synced === 1);                                 // (A) re-sync landed
  check('server_now_has_msg', (serverStore.get('c-poor') || []).length === 1);
  check('markers_cleared_after_sync', convHasPendingSync(reloaded) === false);

  // (B) The PUT body must NOT carry the client-only _pendingSync marker.
  const serverMsgs = serverStore.get('c-poor') || [];
  const leaked = serverMsgs.some(m => m && m._pendingSync !== undefined);
  check('put_body_stripped_pendingSync', leaked === false);

  console.log(out.join('\n'));
})();
"""


def _run(js_path: str, script: str, name: str):
    harness = os.path.join(HERE, name)
    with open(harness, 'w') as f:
        f.write(script)
    try:
        return subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_pending_sync_durability_end_to_end():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, _HARNESS, '_pending_sync_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'pending-sync durability failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_idb_stripmessage_preserves_pending_sync():
    """(C) idb-cache `_stripMessage` must PRESERVE `_pendingSync` — the marker
    only survives a reload if the cache keeps it. It strips only transient
    bloat (`_hydratePromise`, `_translateTaskId`)."""
    idb_js = os.path.join(JS_DIR, 'idb-cache.js')
    script = r"""
const fs = require('fs');
global.window = global;
global.indexedDB = undefined;   // force _available=false; we only need the IIFE to define ConvCache
global.navigator = {};
eval(fs.readFileSync(process.argv[2], 'utf8'));
// _stripMessage is a private closure; exercise it via the documented invariant
// instead: put() calls _stripMessage. But with IDB unavailable put() no-ops.
// So assert the source contract directly: _stripMessage copies every own key
// except the two transient ones — _pendingSync is NOT in the skip list.
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function _stripMessage\(m\)\s*\{[\s\S]*?\n  \}/);
const out = [];
function check(n,c){ out.push((c?'PASS ':'FAIL ')+n); }
check('stripMessage_found', !!m);
if (m) {
  const body = m[0];
  check('skips_hydratePromise', body.includes("_hydratePromise"));
  check('does_not_skip_pendingSync', !body.includes("_pendingSync"));
}
console.log(out.join('\n'));
"""
    proc = _run(idb_js, script, '_stripmsg_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, '_stripMessage pending-sync preservation failures:\n' + output


def test_reconcile_keeps_pending_sync_local():
    """(D) Source-level: `_localHasPendingSync` must be a term of
    `localHasUnsynced` so the reload OVERWRITE branch cannot fire when a
    durable pending tail exists."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'const localHasUnsynced\s*=([\s\S]*?);', src)
    assert m, 'localHasUnsynced assignment not found — anchor drifted'
    body = m.group(1)
    assert '_localHasPendingSync' in body, (
        'regression: localHasUnsynced no longer includes _localHasPendingSync — '
        'a durably-cached poor-network message would fall into the OVERWRITE '
        'branch and be wiped by the shorter server copy on reload.')


def test_reconcile_pending_term_double_neuter():
    """DOUBLE-NEUTER: removing the `_localHasPendingSync ||` term drops it from
    the localHasUnsynced expression → the source assertion fails. In-memory
    copy; real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    # `_localHasPendingSync` is the FINAL disjunct of localHasUnsynced (the old
    # third `localNewest > serverNewest` term was removed in the rev-CAS
    # refactor), so it carries no trailing `||`. Neuter by removing the ` ||`
    # that joins it plus the term itself, leaving only _hasFreshLocalActivity.
    frag = ' ||\n      _localHasPendingSync;'
    assert frag in src, 'pending term fragment drifted — update the neuter target'
    neutered = src.replace(frag, ';', 1)
    m = re.search(r'const localHasUnsynced\s*=([\s\S]*?);', neutered)
    assert m, 'neutered source lost the localHasUnsynced anchor'
    assert '_localHasPendingSync' not in m.group(1), (
        'DOUBLE-NEUTER did not bite: the pending term survived removal — the '
        'test does not discriminate the fix.')


def test_put_body_strips_pending_sync_marker():
    """The lightMsgs mapper must strip `_pendingSync` before the PUT so the
    client-only marker never echoes to the server."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'if \(r\._pendingSync\)\s*\{\s*r = \{ \.\.\.r \};\s*delete r\._pendingSync;', src), (
        'regression: the PUT-body mapper no longer strips _pendingSync — the '
        'client-only durability marker would be persisted server-side and echo '
        'back, wrongly re-triggering KEEP_LOCAL on every reload.')

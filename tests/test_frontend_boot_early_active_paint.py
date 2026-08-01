"""tests/test_frontend_boot_early_active_paint.py — regression for the
"force-refresh feels like it waits on the backend" fix.

WHY
---
`hydrateSidebarFromCache()` paints the sidebar SHELLS from IndexedDB before the
boot server round-trip, but the ACTIVE conversation's chat BODY used to paint
only in the post-await `_bootRestoreActiveConv` — i.e. AFTER
`initActiveTasks`' blocking `Promise.all([loadConversationsFromServer(prefetch),
Api.chat.activeResponse()])`. So even a refresh of an already-cached
conversation blocked its first chat paint on the server combo GET.

THE FIX (static/js/main.js)
---------------------------
`_bootEarlyPaintActiveConv(restoredId)` is chained on `hydrateSidebarFromCache()`
and OPENS the restored conversation the moment its cache shell exists, reusing
`loadConversation` → `loadConversationMessages`' EXISTING two-phase path:
  • Phase 1 — instant IndexedDB paint (renderChat from cache, ZERO network wait);
  • Phase 2 — server GET reconcile that ADOPTS the fresh server body.

The edge case this test pins: Phase 1 sets `conv._needsLoad = false`, which
would let the `?meta=1&prefetch=` combo guard (`pc._needsLoad`) DROP the fresh
prefetched body. That is harmless ONLY because on this first call the shell's
`_needsLoad` is still true, so `loadConversationMessages` does NOT early-return
— its OWN Phase 2 fetch + OVERWRITE reconcile makes the server body win. This
test guards BOTH halves:

  (1) INSTANT PAINT — renderChat fires from cache BEFORE the server fetch
      resolves (proves no server dependency for first paint).
  (2) SERVER RECONCILE — after Phase 2, conv.messages equals the fresh SERVER
      body, NOT the stale cache (proves the server body still wins after
      _needsLoad was cleared by Phase 1).

Both are driven through the REAL shipped `_loadConversationMessagesImpl`
(static/js/core/conversations.js) under bare node.

NEUTERS (on MUTATED copies; shipped file left byte-identical):
  • Strip the Phase-1 cache render → check (1) FAILS (no instant paint).
  • Force the Phase-2 reconcile to keep the cache (neuter the OVERWRITE adopt)
    → check (2) FAILS (stale cache survives, server body lost).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Drives the REAL _loadConversationMessagesImpl for an active cache-shell conv:
#   • ConvCache.get() returns a STALE cached body (older updatedAt);
#   • the server GET returns a FRESHER, DIFFERENT body (newer updatedAt);
#   • the server fetch is DEFERRED one microtask so we can observe that the
#     cache paint (renderChat) happens FIRST, before the server body lands.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const OLD = 1700000000000;
const NEW = 1700000005000;

// The active conv, seeded as a cache SHELL exactly like hydrateSidebarFromCache
// produces: _needsLoad=true, messages:[] (body not yet loaded).
global.activeConvId = 'open';
global.activeStreams = new Map();
global.streamBufs = new Map();
global.streamSessions = new Map();
global.document = { getElementById: () => null, addEventListener: () => {} };
global.getStreamSession = global.getStreamSession = (cid) => { let s = global.streamSessions.get(cid); if (!s) { s = { phase: null }; global.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!global.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = global.clearStreamSession = (cid) => { global.streamSessions.delete(cid); };
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};

// ── Observability: record render calls in order, tagged with what was rendered. ──
// The render seam is ConvView.replaceAll (the RENDER_CONTRACT migration removed
// the bare renderChat call from loadConversationMessages); resolve the conv by id.
let renderLog = [];              // [{ when, len, contents }]
let serverFetchResolved = false; // flips true only after the deferred server GET resolves
global.ConvView = {
  replaceAll: (id) => {
    const conv = (global.conversations || []).find(c => c.id === id);
    renderLog.push({
      afterServer: serverFetchResolved,
      len: conv && conv.messages ? conv.messages.length : -1,
      contents: conv && conv.messages ? conv.messages.map(m => m.content).join(',') : '',
    });
  },
  startStreaming: () => {},
};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global._setCacheVerifying = undefined;   // let the real in-file def be used
global.attachCompactionMarkersToConversation = undefined;
global._bgRefreshChat = undefined;
global.Icon = () => '';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.errorEnvelopeKind = () => '';
global.syncConversationToServer = () => {};
global._retriggerHgTranslations = () => {};
global._resumePendingTranslations = () => {};
global.recordWindowState = () => false;
global.apiUrl = (p) => p;
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);

// STALE cached body: one message, older updatedAt.
const CACHED = {
  id: 'open', title: 'open', updatedAt: OLD, cachedAt: OLD,
  messages: [{ role: 'user', content: 'STALE_CACHE' }],
  settings: {},
};
global.ConvCache = {
  isAvailable: () => true,
  get: () => Promise.resolve(CACHED),
  getMeta: () => Promise.resolve(null),
  getAllMeta: () => Promise.resolve([]),
  put: () => {},
  remove: () => {},
};

// FRESH server body: DIFFERENT + longer, newer updatedAt. The server GET is
// DEFERRED a macrotask so the cache paint provably lands first.
const SERVER_BODY = {
  id: 'open', title: 'open', updatedAt: NEW, updated_at: NEW, rev: 7,
  messages: [
    { role: 'user', content: 'FRESH_SERVER_A' },
    { role: 'assistant', content: 'FRESH_SERVER_B' },
  ],
  settings: {},
};
global.Api = {
  conversations: {
    getResponse: () => new Promise((resolve) => {
      setTimeout(() => {
        serverFetchResolved = true;
        resolve({
          status: 200, ok: true,
          headers: { get: () => null },
          json: async () => SERVER_BODY,
        });
      }, 5);
    }),
    get: async () => SERVER_BODY,
  },
};

global.conversations = [{
  id: 'open', title: 'open', messages: [],
  _serverMsgCount: 1, _needsLoad: true,
  createdAt: OLD, updatedAt: OLD, activeTaskId: null, _fromCache: true,
}];

for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));  // bundle-order sources via _conv_bundle_sources (incl. REAL core/conversations.js + every leaf it references)
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof loadConversationMessages !== 'function') {
    console.log('FAIL fn_exposed loadConversationMessages missing'); process.exit(0);
  }
  check('fn_exposed', true);

  await loadConversationMessages('open');
  // Let any trailing microtasks settle.
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }

  const conv = conversations.find(c => c.id === 'open');

  // (1) INSTANT PAINT: at least one renderChat happened BEFORE the server GET
  //     resolved, and it painted the CACHED content (not empty, not server).
  const preServerRenders = renderLog.filter(r => !r.afterServer);
  const cachePainted = preServerRenders.some(r => r.contents.indexOf('STALE_CACHE') !== -1);
  check('instant_cache_paint_before_server', cachePainted);

  // (2) SERVER RECONCILE: after Phase 2, the in-memory body is the FRESH SERVER
  //     body — the stale cache did NOT survive even though Phase 1 cleared
  //     _needsLoad.
  const finalContents = conv.messages.map(m => m.content).join(',');
  const adoptedServer = finalContents === 'FRESH_SERVER_A,FRESH_SERVER_B';
  check('server_body_wins_after_reconcile', adoptedServer);

  console.log('RENDER_LOG=' + JSON.stringify(renderLog));
  console.log('FINAL=' + finalContents);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_boot_early_active_paint_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    # Eval the WHOLE conv family (conversations.js + every core/conv_* leaf
    # + pending_sync) via the drift-proof family closure — hand-picked pin
    # lists went stale at every slice (5 measured instances in one day:
    # _serverConvCount, _setCacheVerifying, _scheduleConvVerifyRetry, …).
    sys.path.insert(0, HERE)
    from _conv_bundle_sources import conv_family_sources
    override = None
    if os.path.basename(js_path) != 'conversations.js':
        override = {'core/conversations.js': js_path}
    extra_js = conv_family_sources(override=override)
    try:
        return subprocess.run(
            ['node', harness, *extra_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_early_paint_and_server_reconcile():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'boot early-paint / reconcile failures:\n' + output
    assert 'PASS instant_cache_paint_before_server' in output, output
    assert 'PASS server_body_wins_after_reconcile' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_phase1_cache_render_is_load_bearing(tmp_path):
    """NEUTER: strip the Phase-1 cache render on a COPY → check (1) FAILS.
    Proves the instant cache paint (not the server fetch) is what gives the
    zero-wait first paint. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    # The Phase-1 active-conv render is `window.ConvView.replaceAll(...)` immediately
    # followed by the _restoreConvToolState + _setCacheVerifying lines. Neuter
    # ONLY that first (cache) render by disabling it in the cache branch.
    needle = ('          window.ConvView.replaceAll(conv.id, { forceScroll: false });\n'
              '          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);\n'
              '          _setCacheVerifying(convId, _cacheKnownStale);')
    assert src.count(needle) == 1, 'Phase-1 cache-render fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        '          /* NEUTER: Phase-1 cache render removed */\n'
        '          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);\n'
        '          _setCacheVerifying(convId, _cacheKnownStale);', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_phase1.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL instant_cache_paint_before_server' in output, (
        'NEUTER did not bite: instant paint still happened without the Phase-1 '
        'cache render.\n' + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_phase2_overwrite_adopt_is_load_bearing(tmp_path):
    """NEUTER: make the Phase-2 OVERWRITE branch KEEP the local (cache) body
    instead of adopting the server body → check (2) FAILS. Proves the server
    reconcile — not the cache — is authoritative after _needsLoad was cleared
    by Phase 1. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    # The OVERWRITE adopt is `conv.messages = serverMsgs;` inside the
    # cacheIsStale block. Neuter it to a no-op so the stale cache body survives.
    needle = ('        conv.messages = serverMsgs;\n'
              '        conv.title = data.title || conv.title;\n'
              '        conv.updatedAt = serverUpdatedAt || conv.updatedAt;')
    assert src.count(needle) == 1, 'Phase-2 OVERWRITE-adopt fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        '        /* NEUTER: keep local cache body instead of adopting server */\n'
        '        conv.title = data.title || conv.title;\n'
        '        conv.updatedAt = serverUpdatedAt || conv.updatedAt;', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_phase2.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL server_body_wins_after_reconcile' in output, (
        'NEUTER did not bite: server body still won without the OVERWRITE '
        'adopt — the check does not discriminate the reconcile.\n' + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'

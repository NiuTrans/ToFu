"""tests/test_frontend_conv_verify_failure_reheal.py — regression for the
"historical conversation showed a stale 'ended normally' for minutes" bug.

WHY
---
`loadConversationMessages()` Phase 1 paints the IndexedDB cache instantly and
clears `conv._needsLoad` OPTIMISTICALLY; Phase 2 then verifies against the
server. When the Phase-2 fetch never lands (timeout / 5xx / offline — common
on slow mounts), the old bail-out paths left `_needsLoad === false` and even
REMOVED the "verifying" dim, so:

  1. every subsequent open early-returned the unverified stale copy forever
     (`if (!conv._needsLoad && conv.messages.length > 0) return conv;`) —
     the user kept seeing "ended normally" although the server tail was an
     ERROR (e.g. a Project-Brain auto-dispatch whose model reply failed);
  2. the provisional paint posed as final truth (dim cleared), and
  3. nothing self-healed while the user sat in the open conversation — only
     a lucky later push / refocus fixed it, minutes on.

THE FIX (static/js/core/conversations.js + core/cross_tab_sync.js)
------------------------------------------------------------------
On all three Phase-2 failure exits (`!resp`, `!resp.ok` non-404, outer catch)
with a cache hit:
  • restore `conv._needsLoad = true`  → next open re-verifies;
  • keep the verifying dim when `_cacheKnownStale` (server-issued evidence);
  • `_scheduleConvVerifyRetry(convId)` — a BOUNDED self-heal (default
    4s/12s) riding the non-destructive `_verifyActiveConvFromServer`
    (adopt-on-change, no cache repaint, no scroll reset), which now returns
    a three-state verdict (true adopted / false verified-no-change / null
    fetch-failed) so the retry knows whether to stop.

This harness drives the REAL shipped `loadConversationMessages`
(static/js/core/conversations.js) under bare node:

  (A) `_needsLoad` is RESTORED after a failed verify   (RED pre-fix).
  (B) `_cacheKnownStale` survives the failure          (RED pre-fix — the old
      bail deleted it).
  (C) the `chat-cache-verifying` class STAYS on chatInner while unverified
      (RED pre-fix — the old bail removed it).
  (D) the bounded retry self-heals the open conv: `_verifyActiveConvFromServer`
      is called, the server body is adopted, the dim clears (RED pre-fix —
      no scheduler existed).
  (E) a BACKGROUND conv's failure restores `_needsLoad` but schedules NO
      retry (self-heal is scoped to the open conv).

NEUTERS (on MUTATED copies; shipped file left byte-identical):
  • NC1: strip the three `conv._needsLoad = true;` restorations → check A
    FAILS (proves they are what re-arms verification).
  • NC2: make `_scheduleConvVerifyRetry` a no-op → check D FAILS (proves the
    scheduler is what heals the open conv).
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


# Drives the REAL loadConversationMessages for an ACTIVE cache-shell conv whose
# server GET ALWAYS fails (getResponse → null ×3 = the all-retries-exhausted
# bail path). The sidebar shell carries a NEWER updatedAt than the IDB cache,
# so `_cacheKnownStale` is true at paint time. A stub
# `_verifyActiveConvFromServer` (the seam the real retry scheduler calls)
# adopts the fresh server body. Retry delays are shortened via the
# `window._CONV_VERIFY_RETRY_DELAYS` test seam.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const OLD = 1700000000000;
const NEW = 1700000005000;

// Fake chatInner element with a recording classList (the real
// _setCacheVerifying toggles 'chat-cache-verifying' on it).
const _classes = new Set();
const fakeInner = {
  classList: {
    add: (c) => _classes.add(c),
    remove: (c) => _classes.delete(c),
    contains: (c) => _classes.contains(c),
  },
  innerHTML: '',
};
global.document = {
  getElementById: (id) => (id === 'chatInner' ? fakeInner : null),
  addEventListener: () => {},
};

global.activeConvId = 'open';
global.activeStreams = new Map();
global.streamBufs = new Map();
global.streamSessions = new Map();
global.getStreamSession = (cid) => { let s = global.streamSessions.get(cid); if (!s) { s = { phase: null }; global.streamSessions.set(cid, s); } return s; };
global.setStreamPhase = (cid, p) => { if (!global.streamSessions.has(cid) && !(typeof activeStreams !== "undefined" && activeStreams.has(cid))) return; global.getStreamSession(cid).phase = p; };
global.clearStreamSession = (cid) => { global.streamSessions.delete(cid); };
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};

global.showStreamingUIForConv = () => {};
global.ConvView = { replaceAll: () => {}, startStreaming: () => {} };
global._restoreConvToolState = () => {};
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
global._refreshServerQueue = undefined;

// STALE cached body: one message, older updatedAt.
const CACHED = {
  id: 'open', title: 'open', updatedAt: OLD, cachedAt: OLD,
  messages: [{ role: 'user', content: 'STALE_CACHE', timestamp: 1 }],
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

// Server GET ALWAYS fails → the all-retries-exhausted (!resp) bail path.
global.Api = {
  conversations: {
    getResponse: () => Promise.resolve(null),
    get: async () => null,
  },
};

// The fresh server truth the self-heal retry adopts (dispatch + error tail).
const SERVER_MSGS = [
  { role: 'user', content: 'STALE_CACHE', timestamp: 1 },
  { role: 'user', content: 'BRAIN_DISPATCH', timestamp: 2 },
  { role: 'assistant', content: '', finishReason: 'error', timestamp: 3,
    error: { kind: 'endpoint_unreachable' } },
];

// Stub the seam the REAL retry scheduler calls (defined in cross_tab_sync.js,
// not loaded here). Records calls; adopts the server body like the real one.
global._verifyCalls = [];
global._verifyActiveConvFromServer = async (cid) => {
  global._verifyCalls.push(cid);
  const c = conversations.find((x) => x.id === cid);
  if (!c) return null;
  c.messages = SERVER_MSGS;
  c._needsLoad = false;
  return true;   // adopted
};

// Bounded self-heal delays shortened via the test seam.
global._CONV_VERIFY_RETRY_DELAYS = [5, 10, 15];

global.conversations = [{
  id: 'open', title: 'open', messages: [],
  _serverMsgCount: 1, _needsLoad: true,
  createdAt: OLD, updatedAt: NEW,   // sidebar shell is NEWER than the IDB cache
  activeTaskId: null, _fromCache: true,
}];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
// Extracted leaf modules (pt_3879f00e decomposition): the cache-paint path
// calls _applySettingsToConv (core/conv_apply_settings.js) and the load path
// calls helpers from core/pending_sync.js + core/conv_persist_helpers.js,
// none of which still live in conversations.js. Eval them so the harness
// scope matches the shipped bundle (lib/js_bundler.py concatenates them all).
for (const extra of process.argv.slice(3)) eval(fs.readFileSync(extra, 'utf8'));
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  if (typeof loadConversationMessages !== 'function') {
    console.log('FAIL fn_exposed loadConversationMessages missing'); process.exit(0);
  }
  check('fn_exposed', true);

  await loadConversationMessages('open');
  // Let trailing microtasks settle — but do NOT sleep long enough for the 5ms
  // retry timer to fire, so these probes observe the POST-FAILURE state.
  for (let i = 0; i < 10; i++) { await Promise.resolve(); }

  const conv = conversations.find(c => c.id === 'open');

  // Sanity: the stale cache WAS painted (pre-retry state).
  const painted = conv.messages.map(m => m.content).join(',');
  check('cache_paint_happened', painted === 'STALE_CACHE');

  // (A) _needsLoad restored after the failed verify → next open re-verifies.
  check('needsLoad_restored_after_failure', conv._needsLoad === true);

  // (B) server-issued staleness evidence preserved.
  check('known_stale_evidence_kept', conv._cacheKnownStale === true);

  // (C) the "verifying" dim STAYS while the paint is unverified.
  check('verifying_dim_kept', _classes.has('chat-cache-verifying'));

  // (D) bounded self-heal: the retry fires, adopts the server body, clears dim.
  await sleep(120);
  const healed = conv.messages.map(m => m.content).join(',');
  check('self_heal_retry_called', global._verifyCalls.indexOf('open') !== -1);
  check('self_heal_adopts_server_body',
        healed === 'STALE_CACHE,BRAIN_DISPATCH,' &&
        conv.messages[2].finishReason === 'error');
  check('dim_cleared_after_heal', !_classes.has('chat-cache-verifying'));
  check('retry_counter_reset', (conv._verifyRetryCount || 0) === 0);

  // (E) BACKGROUND conv: failure restores _needsLoad but schedules NO retry.
  global._verifyCalls.length = 0;
  conversations.push({
    id: 'bg', title: 'bg', messages: [],
    _serverMsgCount: 1, _needsLoad: true,
    createdAt: OLD, updatedAt: NEW, activeTaskId: null,
  });
  const cachedBg = Object.assign({}, CACHED, { id: 'bg' });
  global.ConvCache.get = (cid) => Promise.resolve(cid === 'bg' ? cachedBg : CACHED);
  await loadConversationMessages('bg');
  for (let i = 0; i < 10; i++) { await Promise.resolve(); }
  const bg = conversations.find(c => c.id === 'bg');
  check('bg_needsLoad_restored', bg._needsLoad === true);
  await sleep(60);
  check('bg_no_retry_scheduled', global._verifyCalls.indexOf('bg') === -1);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_conv_verify_reheal_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    extra_js = [
        os.path.join(JS_DIR, 'core', 'conv_apply_settings.js'),
        os.path.join(JS_DIR, 'core', 'conv_image_hydrate.js'),
        os.path.join(JS_DIR, 'core', 'pending_sync.js'),
        os.path.join(JS_DIR, 'core', 'conv_persist_helpers.js'),
    ]
    try:
        return subprocess.run(
            ['node', harness, js_path, *extra_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


def _assert_all_pass(output: str):
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'verify-failure reheal failures:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_verify_failure_restores_and_reheals():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    _assert_all_pass(output)
    for probe in ('needsLoad_restored_after_failure',
                  'known_stale_evidence_kept',
                  'verifying_dim_kept',
                  'self_heal_retry_called',
                  'self_heal_adopts_server_body',
                  'bg_needsLoad_restored',
                  'bg_no_retry_scheduled'):
        assert f'PASS {probe}' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_needsLoad_restore_is_load_bearing(tmp_path):
    """NEUTER 1: strip the three `conv._needsLoad = true;` failure-exit
    restorations on a COPY → check A FAILS (the unverified copy would be
    early-returned by every later open — the original sticky-stale bug).
    Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    assert src.count('conv._needsLoad = true;') == 3, (
        'expected exactly 3 failure-exit restorations — drifted, update the neuter')
    neutered = src.replace('conv._needsLoad = true;',
                           '/* NEUTER: needsLoad restore removed */')
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_needsload.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL needsLoad_restored_after_failure' in output, (
        'NEUTER did not bite: _needsLoad still restored without the restore '
        'lines.\n' + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_self_heal_scheduler_is_load_bearing(tmp_path):
    """NEUTER 2: make `_scheduleConvVerifyRetry` a no-op on a COPY → check D
    FAILS (nothing heals the open conv; the stale paint survives until some
    unrelated push). Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = '  if (attempt >= delays.length) return;\n  clearTimeout('
    assert src.count(needle) == 1, 'scheduler fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        '  return; /* NEUTER: self-heal scheduler disabled */\n'
        '  if (attempt >= delays.length) return;\n  clearTimeout(', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_scheduler.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL self_heal_retry_called' in output, (
        'NEUTER did not bite: self-heal retry still fired with the scheduler '
        'disabled.\n' + output)

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'

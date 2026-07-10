"""Regression: a poor-network pending-sync message stranded on a conv that
reloads as a metadata-only SHELL must still be flushed — not lost until the
user happens to reopen that exact conversation.

WHY (the gap, one layer up from the message-level durability fix)
-----------------------------------------------------------------
The poor-network durability feature marks a failed-send turn two ways:
  • message-level `_pendingSync` — durable in the IndexedDB *messages* store;
  • conv-level `_pendingSyncAt` — a runtime flag `convHasPendingSync` checks
    FIRST.
`_flushPendingSyncs` iterates `conversations` and tests `convHasPendingSync`,
which reads `conv.messages`. After a reload, a conversation that isn't the
active one comes back as a lazy `_needsLoad` SHELL with `messages: []` — and
the conv-level `_pendingSyncAt` was NEVER persisted (it wasn't in the cache
meta whitelist), so the shell looks clean. The durably-cached pending tail is
therefore invisible to the poller → the message the durability feature promised
to rescue is silently stranded until the user opens that conversation. Same
silent-data-loss class as the message-level bug, one layer up.

THE FIX (root cause, three coupled parts)
------------------------------------------
1. `idb-cache.js` `_extractSettings` now persists `_pendingSyncAt` into the
   META row → the cheap `getAllMeta()` boot scan can see a stranded pending
   tail WITHOUT joining the messages store.
2. `hydrateSidebarFromCache` restores `_pendingSyncAt` onto the reloaded shell
   (so `convHasPendingSync` returns true) and kicks the flush poller when any
   hydrated shell carries a pending tail.
3. `_flushPendingSyncs`, for a pending SHELL (`messages:[]` + `_needsLoad`),
   HYDRATES it via `loadConversationMessages` (the cache path restores the
   durable `_pendingSync` message tail) BEFORE syncing — never syncs an empty
   shell (which would risk clobbering the server).

CHECKS (drive the REAL shipped conversations.js under node)
-----------------------------------------------------------
(A) hydrateSidebarFromCache restores `_pendingSyncAt` onto the shell so
    `convHasPendingSync(shell)` is true (the detection fix).
(B) _flushPendingSyncs hydrates the `messages:[]` shell from the (fake) cache
    and then syncs it — the server ends up with the stranded message.
(C) An empty shell whose tail CANNOT be hydrated (cache miss + server miss) is
    NOT synced (no clobber), and the marker is left for a later retry.

TRIPLE-NEUTER on the load-bearing hydrate-before-sync step:
  • original fix → (B) passes (message reaches the server);
  • invariant → the server never receives an EMPTY message list for the shell;
  • defuse (remove the hydrate call) → (B) fails (empty shell → sync skipped,
    message never delivered).
Real file left byte-identical.
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


# The harness reads conversations.js from argv[2] so a defused COPY can be
# swapped in. A `HYDRATABLE` env toggle switches whether the fake cache can
# materialise the shell's messages (check B vs C).
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const HYDRATABLE = process.env.HYDRATABLE !== '0';
// P2FAIL simulates the worst poor-network interleave: the network flickers up
// enough to pass the flush health gate, Phase-1 cache read succeeds (tail
// restored from IDB), but Phase-2's server verify fetch TIMES OUT (getResponse
// → null) AND the rescue PUT also fails. Nothing must blank the restored tail
// or drop the pending marker; a later poll retries.
const P2FAIL = process.env.P2FAIL === '1';

// ── Fake IndexedDB with the meta/message split the real cache has. ──
// meta carries settings._pendingSyncAt (the persisted conv-level marker);
// the durable pending message tail is returned by get() only when HYDRATABLE.
const PENDING_AT = 1700000000500;
const metaRow = {
  id: 'c-shell', title: 'poor net', updatedAt: 1700000000000,
  cachedAt: 1700000000000, msgCount: 1,
  settings: { model: 'aws.claude-opus-4.8', _pendingSyncAt: PENDING_AT },
};
const fullConv = {
  id: 'c-shell', title: 'poor net', updatedAt: 1700000000000,
  cachedAt: 1700000000000,
  messages: [{ role: 'user', content: 'stranded poor-network message',
               timestamp: 1700000000000, _msgId: 'u1', _pendingSync: true }],
};
global.ConvCache = {
  isAvailable: () => true,
  getAllMeta: () => Promise.resolve([metaRow]),
  get: (id) => Promise.resolve(HYDRATABLE ? JSON.parse(JSON.stringify(fullConv)) : null),
  getMeta: (id) => Promise.resolve(metaRow),
  put: () => Promise.resolve(),
  remove: () => Promise.resolve(),
};

// ── Flippable server: healthy + accepts PUTs, records the body. ──
const serverStore = new Map();
let lastPutBody = null;
global.Api = {
  conversations: {
    // In P2FAIL the rescue PUT also fails (returns a non-ok / null) so the
    // marker must survive; otherwise it succeeds and records the body.
    put: async (id, body) => {
      lastPutBody = body;
      if (P2FAIL) return null;           // PUT fails on the still-flaky network
      serverStore.set(id, body.messages);
      return { ok: true };
    },
    // loadConversationMessages Phase-2 verify fetch. Normally: server has
    // NOTHING (it never received the failed send) → shorter than the durable
    // local tail. In P2FAIL: the fetch itself times out → null after retries.
    getResponse: async (id) => {
      if (P2FAIL) return null;           // network timeout / abort
      return { ok: true, status: 200, json: async () => ({ messages: [], updatedAt: 1699999999000 }) };
    },
    get: async (id) => ({ messages: [], updatedAt: 1699999999000 }),
  },
  health: { check: async () => ({ ok: true }) },
};

global.activeStreams = new Map();
global.activeConvId = null;
global.debugLog = function () {};
global.console = console;
global.config = { defaultThinkingDepth: 'medium' };
global.renderConversationList = function () {};
global.renderChat = function () {};
global.showStreamingUIForConv = function () {};
global._restoreConvToolState = function () {};
global.attachCompactionMarkersToConversation = undefined;
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.document = { getElementById: () => null };
global.conversations = [];
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
global.conversations = conversations;   // rebind to the module-scoped array

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof hydrateSidebarFromCache !== 'function' ||
    typeof _flushPendingSyncs !== 'function' ||
    typeof convHasPendingSync !== 'function') {
  console.log('FAIL fn_exposed helpers missing'); process.exit(0);
}
check('fn_exposed', true);

(async () => {
  // ═══ Simulate a RELOAD: sidebar hydrates shells from cache. ═══
  const added = await hydrateSidebarFromCache();
  check('shell_added', added === 1);
  const shell = conversations.find((c) => c.id === 'c-shell');
  check('shell_is_lazy', !!shell && shell._needsLoad === true && shell.messages.length === 0);

  // (A) The conv-level pending marker was restored onto the shell so the poller
  //     can SEE it — the detection fix.
  check('shell_detected_pending', !!shell && convHasPendingSync(shell) === true);

  if (P2FAIL) {
    // ═══ WORST CASE: Phase-1 cache read succeeds (tail restored) but Phase-2
    //     verify fetch TIMES OUT and the rescue PUT also fails. ═══
    const synced = await _flushPendingSyncs('reload');
    check('p2fail_not_synced', synced === 0);                 // PUT failed
    const after = conversations.find((c) => c.id === 'c-shell');
    // The restored tail must SURVIVE — Phase-2's early return (cacheHit && null
    // resp) never reaches the reconcile dispatch, so no OVERWRITE can blank it.
    check('p2fail_tail_survived',
      !!after && after.messages.length === 1 &&
      after.messages[0].content === 'stranded poor-network message');
    // The durable message-level marker must survive too.
    check('p2fail_msg_marker_survived',
      !!after && after.messages[0]._pendingSync === true);
    // The conv-level marker must survive so a later poll retries — NOT cleared
    // by a failed attempt.
    check('p2fail_pendingAt_survived', !!after && !!after._pendingSyncAt);
    check('p2fail_still_detected', !!after && convHasPendingSync(after) === true);
    // The server never received an empty message list (no clobber).
    check('p2fail_no_empty_put',
      lastPutBody === null || (lastPutBody.messages || []).length > 0);
  } else if (HYDRATABLE) {
    // (B) Flush hydrates the shell + syncs → the stranded message reaches the
    //     server, and the marker is cleared.
    const synced = await _flushPendingSyncs('reload');
    check('flush_synced_shell', synced === 1);
    const delivered = serverStore.get('c-shell') || [];
    check('server_got_stranded_msg',
      delivered.length === 1 && delivered[0].content === 'stranded poor-network message');
    // Triple-neuter invariant: the PUT must NEVER be an empty message list.
    check('put_never_empty', lastPutBody && Array.isArray(lastPutBody.messages) && lastPutBody.messages.length > 0);
    const after = conversations.find((c) => c.id === 'c-shell');
    check('markers_cleared', !!after && convHasPendingSync(after) === false);
  } else {
    // (C) Not hydratable (cache miss + server empty): must NOT sync an empty
    //     shell (no clobber); marker survives for a later retry.
    const synced = await _flushPendingSyncs('reload');
    check('unhydratable_not_synced', synced === 0);
    check('no_empty_put', lastPutBody === null || (lastPutBody.messages || []).length > 0);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str, hydratable: bool = True, p2fail: bool = False) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_pending_sync_shell_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    env = dict(os.environ)
    env['HYDRATABLE'] = '1' if hydratable else '0'
    env['P2FAIL'] = '1' if p2fail else '0'
    try:
        return subprocess.run(
            ['node', harness, js_path],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_pending_sync_shell_detected_and_flushed():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, hydratable=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'pending-sync shell-flush failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_pending_sync_unhydratable_shell_not_clobbered():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, hydratable=False)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'unhydratable-shell clobber-guard failures:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_pending_sync_shell_survives_failed_phase2_hydration():
    """Worst poor-network interleave (positive assertion — no neuter needed):
    reload as a pending shell → the boot flush fires while the network is STILL
    flaky → Phase-1 cache read succeeds (tail restored from IDB) but Phase-2's
    server verify fetch TIMES OUT (getResponse → null after retries) AND the
    rescue PUT also fails. Assert the restored tail + BOTH markers survive so a
    later poll retries, and nothing was clobbered / blanked.

    This is the case the two existing modes don't cover: HYDRATABLE=1 returns a
    SUCCESSFUL empty 200 (reaches the reconcile dispatch, protected by the
    _localHasPendingSync→KEEP_LOCAL term); P2FAIL exercises the *failed* fetch,
    where loadConversationMessages takes the early `return conv` (cacheHit &&
    null resp) and never reaches reconcile at all — so OVERWRITE is structurally
    impossible and the tail is safe by construction."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, hydratable=True, p2fail=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'failed-Phase2-hydration survival failures:\n' + output
    # The load-bearing survival asserts must all be present + passing.
    for tok in ('p2fail_tail_survived', 'p2fail_pendingAt_survived',
                'p2fail_still_detected', 'p2fail_msg_marker_survived'):
        assert f'PASS {tok}' in output, f'missing/failed {tok}:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_shell_flush_triple_neuter(tmp_path):
    """TRIPLE-NEUTER: on a COPY of conversations.js, DEFUSE the load-bearing
    hydrate-before-sync step (remove the `loadConversationMessages` call inside
    `_flushPendingSyncs`) → the pending shell stays `messages:[]`, the guard
    skips it, and the stranded message never reaches the server → (B) FAILS.
    Proves the hydration step is what delivers the message. Also asserts the
    invariant that the neuter changes ONLY that call. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = 'await loadConversationMessages(conv.id);'
    assert src.count(needle) >= 1, 'hydrate call fragment drifted — update the neuter target'
    # Defuse: turn the hydrate into a no-op await so the shell stays empty.
    neutered = src.replace(needle, 'await Promise.resolve();  /* neutered hydrate */', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy), hydratable=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL flush_synced_shell' in output or 'FAIL server_got_stranded_msg' in output, (
        'TRIPLE-NEUTER did not bite: the stranded message still reached the '
        'server without the hydrate-before-sync step.\n' + output
    )

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'

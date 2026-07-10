"""tests/test_frontend_open_conv_body_reconcile.py — regression for the
cross-device OPEN-conversation body-reconcile fix.

WHY
---
The visible-idle cross-device reconcile (`_crossDeviceReconcile` →
`loadConversationsFromServer`, `?meta=1`) re-pulls the OPEN conversation's
message BODY only when the metadata merge sets `conv._needsLoad` — and the old
merge set it *solely* inside `if (serverMsgCount > local.messages.length)` (a
message-COUNT increase). Two real cross-device update shapes slipped through,
so the desktop kept showing stale content until a manual force-refresh:

  (1) CONTENT-ONLY APPEND — another device EXTENDED the trailing turn (the
      server row's `updatedAt` advanced but the message COUNT is unchanged).
      The count-only trigger missed it → `_needsLoad` never set → the body was
      never re-fetched (only stale local content re-rendered).

  (2) PINNED OPEN CONV — the whole update branch was gated on
      `!local.activeTaskId`, so a viewing device that merely HOLDS an
      activeTaskId pin (with NO live stream) skipped the branch entirely and
      never refreshed, even on a genuine count increase.

THE FIX (static/js/core/conversations.js, loadConversationsFromServer merge)
----------------------------------------------------------------------------
  (1) For the OPEN conv (`sc.id === activeConvId`), also set `_needsLoad = true`
      when `sT > mT` (updatedAt advanced) even if the count is unchanged.
  (2) Relax the branch guard to `(!local.activeTaskId || sc.id === activeConvId)`
      so the OPEN conv passes through even while pinned — the `!activeStreams.has`
      guard still bars a LIVE stream, and loadConversationMessages'
      MERGE_ACTIVE_TASK branch merges in place (never truncates, never orphans
      the connectToTask ref).

The re-pull itself is UNCHANGED (`if (ac && ac._needsLoad) await
loadConversationMessages(activeConvId)`), so it still routes through the
existing keep-longer / KEEP_LOCAL / rev-CAS / count-drop guards → a stale device
can only ADD/UPDATE, never truncate fresher server state.

HARNESS — drives the REAL shipped conversations.js under bare node:
  • stubs `fetch` to serve the `?meta=1` sidebar list, and `loadConversationMessages`
    as a re-pull COUNTER (records the convId it was asked to reload);
  • calls the real `loadConversationsFromServer()` and asserts the body re-pull.

CHECKS
  A. content-only append on the OPEN conv (count same, updatedAt newer)   → re-pull fires
  B. PINNED open conv (activeTaskId set, no live stream), updatedAt newer  → re-pull fires
  C. quiet open conv (count same, updatedAt same)                         → NO re-pull (control)
  D. LIVE stream on the open conv                                         → NO re-pull (control)

DOUBLE-NEUTER (on a MUTATED copy; shipped file left byte-identical):
  • revert the content-only `sT > mT` trigger → check A stops firing;
  • revert the pinned-branch guard             → check B stops firing.
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

const OLD = 1700000000000;
const NEW = 1700000005000;   // server row advanced

// ── Sidebar ?meta=1 list served by fetch. Each scenario picks entries. ──
// A conv is "open" when its id === activeConvId.
global.activeConvId = 'open';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.console = console;
global.config = {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.attachCompactionMarkersToConversation = undefined;
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.apiUrl = (p) => p;
global.ConvCache = {
  isAvailable: () => true,
  get: () => Promise.resolve(null),
  getMeta: () => Promise.resolve(null),
  getAllMeta: () => Promise.resolve([]),
  put: () => Promise.resolve(),
  remove: () => Promise.resolve(),
};

// getActiveConv lives in core.js — stub it to the in-memory list.
global.getActiveConv = () => conversations.find((c) => c.id === activeConvId) || null;

// ── The re-pull counter: record every loadConversationMessages(convId). ──
let repullCalls = [];
// Server meta list for the NEXT loadConversationsFromServer() call.
let SERVER_LIST = [];
global.fetch = async () => ({
  status: 200, ok: true,
  headers: { get: () => null },
  json: async () => SERVER_LIST,
  clone() { return this; },
});

global.conversations = [];

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
global.conversations = conversations;

// Override the real loadConversationMessages with a COUNTER (late-bound global
// lookup at the call site inside loadConversationsFromServer picks this up).
loadConversationMessages = async (cid) => {
  repullCalls.push(cid);
  const c = conversations.find((x) => x.id === cid);
  if (c) c._needsLoad = false;
  return c || null;
};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof loadConversationsFromServer !== 'function') {
  console.log('FAIL fn_exposed loadConversationsFromServer missing'); process.exit(0);
}
check('fn_exposed', true);

// Build a fresh open-conv shell with N loaded messages + given local updatedAt.
function seedOpen({ msgs = 2, updatedAt = OLD, activeTaskId = null } = {}) {
  conversations.length = 0;
  const messages = [];
  for (let i = 0; i < msgs; i++) messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i });
  conversations.push({
    id: 'open', title: 'open', messages,
    _serverMsgCount: msgs, _needsLoad: false,
    createdAt: OLD, updatedAt, activeTaskId,
  });
}
function serverEntry({ messageCount = 2, updatedAt = NEW } = {}) {
  return { id: 'open', title: 'open', messageCount, updatedAt, createdAt: OLD, settings: {} };
}

(async () => {
  // ══ A. CONTENT-ONLY APPEND on OPEN conv (count same, updatedAt newer) ══
  {
    repullCalls = [];
    seedOpen({ msgs: 2, updatedAt: OLD });
    SERVER_LIST = [serverEntry({ messageCount: 2, updatedAt: NEW })];  // same count, newer
    await loadConversationsFromServer();
    check('A_content_only_repull_fired', repullCalls.length === 1 && repullCalls[0] === 'open');
  }

  // ══ B. PINNED open conv (activeTaskId set, NO live stream), updatedAt newer ══
  {
    repullCalls = [];
    seedOpen({ msgs: 2, updatedAt: OLD, activeTaskId: 'task-1' });  // pinned, not streaming
    SERVER_LIST = [serverEntry({ messageCount: 3, updatedAt: NEW })];  // a new turn appeared
    await loadConversationsFromServer();
    check('B_pinned_open_repull_fired', repullCalls.length === 1 && repullCalls[0] === 'open');
  }

  // ══ C. CONTROL: quiet open conv (count same, updatedAt SAME) → no re-pull ══
  {
    repullCalls = [];
    seedOpen({ msgs: 2, updatedAt: OLD });
    SERVER_LIST = [serverEntry({ messageCount: 2, updatedAt: OLD })];  // nothing changed
    await loadConversationsFromServer();
    check('C_quiet_no_repull', repullCalls.length === 0);
  }

  // ══ D. CONTROL: LIVE stream on the open conv → no re-pull (never disturb) ══
  {
    repullCalls = [];
    seedOpen({ msgs: 2, updatedAt: OLD });
    activeStreams = new Map(); activeStreams.set('open', { controller: {} });
    SERVER_LIST = [serverEntry({ messageCount: 3, updatedAt: NEW })];
    await loadConversationsFromServer();
    activeStreams = new Map();
    check('D_live_stream_no_repull', repullCalls.length === 0);
  }

  console.log('repull(final)=' + repullCalls.length);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_open_conv_body_reconcile_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, js_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_open_conv_body_reconcile_fires():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'open-conv body-reconcile failures:\n' + output
    for want in ('PASS A_content_only_repull_fired',
                 'PASS B_pinned_open_repull_fired',
                 'PASS C_quiet_no_repull',
                 'PASS D_live_stream_no_repull'):
        assert want in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_content_only_trigger_is_load_bearing(tmp_path):
    """NEUTER: revert the content-only `sT > mT` trigger on a COPY → check A
    (content-only append re-pull) FAILS. Proves that fragment is what fires the
    re-pull for a same-count append. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = 'if (_contentStale) local._needsLoad = true;'
    assert src.count(needle) == 1, 'content-only trigger fragment drifted — update the neuter target'
    # Revert to a condition that can never fire → old count-only behaviour.
    neutered = src.replace(needle, 'if (false) local._needsLoad = true;', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_trigger.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL A_content_only_repull_fired' in output, (
        'NEUTER did not bite: content-only append still re-pulled without the trigger.\n' + output
    )

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_pinned_branch_guard_is_load_bearing(tmp_path):
    """NEUTER: revert the pinned-branch guard on a COPY (back to the strict
    `!local.activeTaskId`) → check B (pinned open conv re-pull) FAILS. Proves
    the relaxed guard is what lets a pinned-but-not-streaming open conv refresh.
    Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = '} else if (!activeStreams.has(sc.id) && (!local.activeTaskId || sc.id === activeConvId)) {'
    assert src.count(needle) == 1, 'pinned-branch guard fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        '} else if (!activeStreams.has(sc.id) && !local.activeTaskId) {', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered_guard.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL B_pinned_open_repull_fired' in output, (
        'NEUTER did not bite: pinned open conv still re-pulled without the relaxed guard.\n' + output
    )

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'

"""tests/test_frontend_warm_open_adopts_reconciled_list.py — RED reproduction
for the "empty Agent air-bubble survives a warm re-open" class.

WHY (root cause, verified in static/js/core/conversations.js)
------------------------------------------------------------
The backend GET-path reconcile (routes/conversations.py::_reconcile_conv_on_get
→ lib/conversations/reconcile.py) is the authoritative sweep that DELETES an
orphaned trailing empty-assistant "ghost" placeholder. It works — but the
frontend never adopts its result in the two exact windows a ghost is born:

  (1) BYPASS — `loadConversationMessages` early-returns for any conv already in
      memory:
          if (!conv._needsLoad && conv.messages.length > 0) return conv;   (:1168)
      So a warm RE-OPEN of a conv that still holds a client-minted empty
      placeholder never re-fetches the reconciled (shorter) server list → the
      ghost renders as a blank "Agent" bubble (chat_render.js renders every
      message unconditionally; branch.js always appends the 分支 button).

  (2) MERGE NON-ADOPTION — when a lingering `activeTaskId` is set (restored from
      server settings, task already finished, NO live stream), Phase-2 takes the
      MERGE_ACTIVE_TASK branch, which only APPENDS trailing server msgs when
          serverMsgs.length > conv.messages.length                          (:1503)
      It never adopts a SHORTER reconciled list, so the swept-away ghost the
      server already removed lives on locally.

THE FIX THIS TEST DRIVES (not yet landed — coordinated with the sibling
conversations that own conversations.js / reconcile.py):
  • Close the warm-open bypass so an idle re-open routes through the
    reconciling GET (or adopts the reconciled list) rather than early-returning.
  • On MERGE_ACTIVE_TASK with NO live stream, adopt the backend's reconciled
    (possibly shorter) list instead of keep-longer-by-append.

Both must preserve two invariants (encoded as controls):
  • A LIVE stream (`activeStreams.has`) is NEVER truncated (orphans the
    connectToTask assistantMsg ref).
  • Genuine un-acked local activity (KEEP_LOCAL) is NEVER truncated.

HARNESS — drives the REAL shipped core/conversations.js under bare node.
Stubs Api.conversations.getResponse to serve the server's reconciled list and
records the post-load conv.messages length so we can assert the ghost is gone.

CHECKS (RED until the fix lands)
  A. warm idle re-open, local has orphan ghost tail, server reconciled shorter → ghost dropped
  B. lingering activeTaskId (no live stream) + ghost tail, server shorter       → ghost dropped
  C. CONTROL: live stream on the conv                                           → messages untouched
  D. CONTROL: genuine fresh local activity (KEEP_LOCAL)                         → local NOT truncated
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

global.activeConvId = 'c1';
global.activeStreams = new Map();
global.streamBufs = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.console = console;
global.config = {};
global.serverModel = 'm';
global.renderConversationList = () => {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global._bgRefreshChat = () => {};
global.attachCompactionMarkersToConversation = undefined;
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.apiUrl = (p) => p;

// Pure paint-cache: never supplies data in this test (force the Phase-2 GET).
global.ConvCache = {
  isAvailable: () => true,
  get: () => Promise.resolve(null),
  getMeta: () => Promise.resolve(null),
  getAllMeta: () => Promise.resolve([]),
  put: () => {},
  remove: () => {},
};

// The server's reconciled response for the NEXT getResponse() call.
let SERVER_MSGS = [];
let SERVER_REV = 5;
global.Api = {
  conversations: {
    getResponse: async () => ({
      status: 200, ok: true,
      headers: { get: () => null },
      json: async () => ({ messages: SERVER_MSGS, title: 'c1', updatedAt: 2000,
                           rev: SERVER_REV, settings: {} }),
      clone() { return this; },
    }),
    get: async () => ({ messages: SERVER_MSGS, title: 'c1' }),
  },
};

global.conversations = [];
eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof loadConversationMessages !== 'function') {
  console.log('FAIL fn_exposed loadConversationMessages missing'); process.exit(0);
}
check('fn_exposed', true);

// A "settled real turn" + an orphaned trailing EMPTY assistant ghost.
function ghostTail() {
  return [
    { role: 'user', content: 'hi', timestamp: 1000 },
    { role: 'assistant', content: 'real answer', timestamp: 1001, finishReason: 'stop' },
    { role: 'assistant', content: '', thinking: '', toolRounds: [], timestamp: 1002 },  // GHOST
  ];
}
// The backend reconcile already swept the ghost → 2 msgs.
function reconciledShort() {
  return [
    { role: 'user', content: 'hi', timestamp: 1000 },
    { role: 'assistant', content: 'real answer', timestamp: 1001, finishReason: 'stop' },
  ];
}
function seed({ activeTaskId = null } = {}) {
  conversations.length = 0;
  conversations.push({
    id: 'c1', title: 'c1', messages: ghostTail(),
    _needsLoad: false,               // ← WARM: already in memory (the bypass condition)
    _serverMsgCount: 3, _cachedUpdatedAt: 2000,
    createdAt: 1000, updatedAt: 1002, activeTaskId,
  });
  return conversations[0];
}

(async () => {
  // ══ A. Warm idle re-open, local has ghost tail, server reconciled shorter ══
  {
    seed();
    SERVER_MSGS = reconciledShort();
    await loadConversationMessages('c1');
    const c = conversations.find(x => x.id === 'c1');
    const tail = c.messages[c.messages.length - 1];
    const ghostGone = c.messages.length === 2
      && !(tail.role === 'assistant' && !tail.content && !tail.finishReason);
    check('A_warm_idle_adopts_reconciled', ghostGone);
  }

  // ══ B. Lingering activeTaskId (no live stream) + ghost tail, server shorter ══
  {
    seed({ activeTaskId: 'task-finished' });
    activeStreams = new Map();  // NO live stream
    SERVER_MSGS = reconciledShort();
    await loadConversationMessages('c1');
    const c = conversations.find(x => x.id === 'c1');
    const tail = c.messages[c.messages.length - 1];
    const ghostGone = c.messages.length === 2
      && !(tail.role === 'assistant' && !tail.content && !tail.finishReason);
    check('B_merge_active_task_adopts_reconciled', ghostGone);
  }

  // ══ C. CONTROL: live stream on the conv → messages MUST NOT be truncated ══
  {
    const c = seed({ activeTaskId: 'task-live' });
    activeStreams = new Map(); activeStreams.set('c1', { controller: {}, taskId: 'task-live', assistantMsg: c.messages[2] });
    SERVER_MSGS = reconciledShort();
    await loadConversationMessages('c1');
    const cc = conversations.find(x => x.id === 'c1');
    check('C_live_stream_not_truncated', cc.messages.length === 3);
    activeStreams = new Map();
  }

  // ══ D. CONTROL: genuine fresh local activity (KEEP_LOCAL) → NOT truncated ══
  {
    // A real just-typed local tail (grew during fetch); server shorter must not win.
    conversations.length = 0;
    conversations.push({
      id: 'c1', title: 'c1',
      messages: [
        { role: 'user', content: 'hi', timestamp: 1000 },
        { role: 'assistant', content: 'real answer', timestamp: 1001, finishReason: 'stop' },
        { role: 'user', content: 'a follow-up I just typed', timestamp: 9999 },  // fresh, un-acked
      ],
      _needsLoad: true,   // force Phase-2; KEEP_LOCAL must protect the fresh tail
      _serverMsgCount: 2, _cachedUpdatedAt: 2000,
      createdAt: 1000, updatedAt: 9999, activeTaskId: null,
    });
    // Simulate the message being pushed DURING the fetch: getResponse resolves
    // AFTER we mark preFetch small. The real _hasFreshLocalActivity relies on
    // conv.messages growing during the awaited fetch; emulate by using a getter.
    let served = false;
    Api.conversations.getResponse = async () => {
      // On first (and only) call, the local tail is already present (length 3),
      // preFetch snapshot was taken at entry — but since _needsLoad path took no
      // cache, preFetchMsgCount === 3 here. To exercise KEEP_LOCAL via pending
      // sync instead, tag the fresh msg _pendingSync.
      served = true;
      return {
        status: 200, ok: true, headers: { get: () => null },
        json: async () => ({ messages: reconciledShort(), title: 'c1', updatedAt: 2000, rev: 6, settings: {} }),
        clone() { return this; },
      };
    };
    conversations[0].messages[2]._pendingSync = true;  // durable un-acked local write
    await loadConversationMessages('c1');
    const cc = conversations.find(x => x.id === 'c1');
    check('D_keep_local_fresh_not_truncated', cc.messages.length === 3 && served);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_warm_open_adopts_reconciled_harness.js')
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
def test_warm_open_adopts_reconciled_list():
    """RED until the fix lands: a warm re-open must adopt the backend's
    reconciled (shorter) message list so an orphaned empty-assistant ghost is
    dropped — while a live stream and genuine fresh local activity are never
    truncated."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    # Controls must already hold today (they guard against over-correction).
    for want in ('PASS C_live_stream_not_truncated',
                 'PASS D_keep_local_fresh_not_truncated'):
        assert want in output, 'control regressed:\n' + output
    # The two ghost-adoption checks are the RED target.
    for want in ('PASS A_warm_idle_adopts_reconciled',
                 'PASS B_merge_active_task_adopts_reconciled'):
        assert want in output, (
            'EXPECTED-RED: warm re-open does not yet adopt the reconciled list '
            '(ghost survives). This is the failing test that drives the fix.\n' + output
        )

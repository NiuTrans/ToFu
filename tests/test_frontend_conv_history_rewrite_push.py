"""tests/test_frontend_conv_history_rewrite_push.py — regression for the
server→client history_rewrite alignment handler (_onConvSyncPush /
_applyHistoryRewrite in static/js/conv_sync_push.js).

WHY
---
When the backend reconcile REWRITES a conversation's persisted history (ghost-
tail delete / superseded-error-husk collapse — routes/conversations.py::
_persist_reconcile), it emits:

    push_event('conv', <convId>, {kind:'history_rewrite', rev:<new>})

Before this handler, that verdict reached the client only on a MANUAL REFRESH
(the reported "must refresh to sync the correct state" pain). Unlike the
keep-longer `notify` path (_verifyActiveConvFromServer, which only adopts when
the server is longer and NEVER shrinks local), a reconcile SHORTENS — so this
channel does an UNCONDITIONAL adopt of the authoritative server copy, gated only
by rev monotonicity + a live-task guard.

Tests (drive the REAL shipped conv_sync_push.js under node):
  1. shorten_adopted — server returns a SHORTER list (ghost tail removed) →
     conv.messages is replaced with the shorter list, renderChat repaints the
     open conv, ConvCache.put + saveConversations fire. (The keep-longer path
     would IGNORE this — the whole reason this channel exists.)
  2. rev_gate — a second frame with rev <= the already-applied rev is a NO-OP
     (no second GET).
  3. live_task_guard — a conv with an activeTaskId is NOT touched (no GET) so a
     live stream's placeholder is never yanked.
  4. background_conv_no_repaint — a rewrite for a NON-active conv adopts but does
     NOT call renderChat (only renderConversationList).

NC (on a MUTATED copy; shipped file never modified):
  strip the live-task guard → test #3's "no GET for live conv" FAILS, proving
  the guard is load-bearing.
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
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

JS_SRC = runtime_section_path('conv_sync_push.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let getCalls = [];
let renderChatCalls = [];
let listRenderCalls = 0;
let saveCalls = [];
let cachePutCalls = [];
let serverResponse = null;

global.debugLog = () => {};
global.conversations = [];
global.activeStreams = new Map();
global.activeConvId = null;
global.renderChat = (conv) => { renderChatCalls.push(conv && conv.id); };
/* The shipped repaint seam moved from renderChat(conv) to
 * window.ConvView.replaceAll(convId) — stub it into the SAME counter so the
 * 'open conv repainted' assertion keeps its original meaning. */
global.ConvView = { replaceAll: (id) => renderChatCalls.push(id) };
global.renderConversationList = () => { listRenderCalls++; };
global.saveConversations = (id) => { saveCalls.push(id); };
global._applySettingsToConv = () => {};
global._restoreConvToolState = () => {};
global.ConvCache = { put: (c) => { cachePutCalls.push(c && c.id); } };
global.pushSubscribe = () => {};
global.Api = { conversations: { get: async (id) => { getCalls.push(id); return serverResponse; } } };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(SRC);

function reset() {
  getCalls = []; renderChatCalls = []; listRenderCalls = 0;
  saveCalls = []; cachePutCalls = []; serverResponse = null;
  global.conversations = [];
  global.activeStreams = new Map();
  global.activeConvId = null;
  global._currentUserId = undefined;
}

async function run() {
  // ── Test 1: SHORTEN adopted (the keep-longer path would ignore this) ──
  reset();
  global.conversations = [{ id: 'c1', messages: [
    { role: 'user', content: 'q1' },
    { role: 'assistant', content: 'a1', finishReason: 'stop' },
    { role: 'user', content: 'q2' },
    { role: 'assistant', content: '' },   // ghost tail
  ] }];
  global.activeConvId = 'c1';
  serverResponse = { id: 'c1', messages: [
    { role: 'user', content: 'q1' },
    { role: 'assistant', content: 'a1', finishReason: 'stop' },
    { role: 'user', content: 'q2' },
  ], rev: 5 };
  await _onConvSyncPush({ channel: 'conv', taskId: 'c1', kind: 'history_rewrite', rev: 5 });
  const c1 = global.conversations[0];
  check('shorten: GET fired', getCalls.length === 1 && getCalls[0] === 'c1');
  check('shorten: messages replaced with shorter list', c1.messages.length === 3);
  check('shorten: open conv repainted', renderChatCalls.length === 1 && renderChatCalls[0] === 'c1');
  check('shorten: saveConversations fired', saveCalls.length === 1);
  check('shorten: ConvCache.put fired', cachePutCalls.length === 1);
  check('shorten: _serverRev advanced to 5', c1._serverRev === 5);

  // ── Test 2: rev-gate — second frame with rev<=applied is a no-op ──
  reset();
  global.conversations = [{ id: 'c2', messages: [{ role: 'user', content: 'q' }] , _serverRev: 0}];
  global.activeConvId = 'c2';
  serverResponse = { id: 'c2', messages: [{ role: 'user', content: 'q' }], rev: 7 };
  await _onConvSyncPush({ taskId: 'c2', kind: 'history_rewrite', rev: 7 });
  const firstGetCount = getCalls.length;
  // A stale re-fire at rev 7 (<= applied 7) must NOT GET again.
  await _onConvSyncPush({ taskId: 'c2', kind: 'history_rewrite', rev: 7 });
  check('rev-gate: first frame fetched', firstGetCount === 1);
  check('rev-gate: stale re-fire is a no-op', getCalls.length === 1);

  // ── Test 3: live-task guard — conv with activeTaskId is never touched ──
  reset();
  global.conversations = [{ id: 'c3', activeTaskId: 'tk-live', messages: [
    { role: 'user', content: 'q' }, { role: 'assistant', content: '' },
  ] }];
  global.activeConvId = 'c3';
  serverResponse = { id: 'c3', messages: [{ role: 'user', content: 'q' }], rev: 9 };
  await _onConvSyncPush({ taskId: 'c3', kind: 'history_rewrite', rev: 9 });
  check('live-guard: NO GET for a live-task conv', getCalls.length === 0);
  check('live-guard: messages untouched', global.conversations[0].messages.length === 2);

  // ── Test 4: background conv adopts but does NOT repaint ──
  reset();
  global.conversations = [{ id: 'cbg', messages: [
    { role: 'user', content: 'q' }, { role: 'assistant', content: '' },
  ] }];
  global.activeConvId = 'other';   // cbg is NOT the open conv
  serverResponse = { id: 'cbg', messages: [{ role: 'user', content: 'q' }], rev: 3 };
  await _onConvSyncPush({ taskId: 'cbg', kind: 'history_rewrite', rev: 3 });
  check('bg: adopted shorter list', global.conversations[0].messages.length === 1);
  check('bg: NO renderChat (not open)', renderChatCalls.length === 0);
  check('bg: renderConversationList called', listRenderCalls === 1);

  // ── Test 5: non-history_rewrite frame ignored ──
  reset();
  global.conversations = [{ id: 'cx', messages: [{ role: 'user', content: 'q' }] }];
  await _onConvSyncPush({ taskId: 'cx', kind: 'something_else', rev: 1 });
  check('ignore: unrelated kind → no GET', getCalls.length === 0);

  console.log(out.join('\n'));
}
run();
"""


def _run(js_path: str) -> str:
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(_HARNESS)
        harness = f.name
    try:
        res = subprocess.run(['node', harness, js_path], capture_output=True, text=True, timeout=60)
        return res.stdout + res.stderr
    finally:
        os.unlink(harness)


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_history_rewrite_push_handler():
    out = _run(JS_SRC)
    lines = [ln for ln in out.splitlines() if ln.startswith(('PASS ', 'FAIL '))]
    assert lines, f'harness produced no results:\n{out}'
    failed = [ln for ln in lines if ln.startswith('FAIL ')]
    assert not failed, 'history_rewrite handler failures:\n' + '\n'.join(lines) + '\n\nRAW:\n' + out
    print('\n'.join(lines))


@pytest.mark.skipif(not _node_available(), reason='node not available')
def test_NC_strip_live_task_guard(tmp_path):
    """Neuter the live-task guard → the live-conv test must FAIL, proving the
    guard is load-bearing (not incidental)."""
    src = open(JS_SRC, encoding='utf-8').read()
    # Remove the guard's early-return: make the guard condition always false.
    neutered = src.replace(
        'if (conv.activeTaskId || (typeof activeStreams !== "undefined" && activeStreams.has(convId))) {\n    return;\n  }',
        'if (false) { return; }')
    assert neutered != src, 'NC substitution did not match — the guard code shape changed'
    p = tmp_path / 'conv_sync_push_neutered.js'
    p.write_text(neutered, encoding='utf-8')
    out = _run(str(p))
    lines = [ln for ln in out.splitlines() if ln.startswith(('PASS ', 'FAIL '))]
    # With the guard gone, the live-task conv gets a GET → its "NO GET" check fails.
    assert any(ln.startswith('FAIL ') and 'live-guard: NO GET' in ln for ln in lines), (
        'NC did not surface the expected failure — guard may not be load-bearing:\n'
        + '\n'.join(lines) + '\n\nRAW:\n' + out)
    print('NC OK — live-task guard is load-bearing')

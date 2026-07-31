#!/usr/bin/env python3
"""tests/test_frontend_poll_404_resync.py — poll-404 re-syncs from the server
SoT instead of minting a terminal error bubble (epic pt_f5771a2e, fix F1).

WHY
---
``_pollFallback`` (static/js/ui/sse_poll_fallback.js) used to answer a poll
404 with ``assistantMsg.error = "Task not found"`` + finishStream — a red
terminal error bubble. But a task 404 after a server restart is a
TRANSPORT-level loss (the in-memory registry was wiped before the first
checkpoint, or TTL cleanup ran), NOT a task error: the conversation DB is
the single source of truth and still holds the real turn state (an
interrupted turn with its Continue affordance, or settled content). The
error bubble then compounded: it sat in conv.messages as a local ghost tail,
and the next Continue judged "empty assistant" by that LOCAL tail and went
down the pop-and-regenerate path that appended a twin answer (the ms43foj3
incident, 2026-07-28).

The fix: on poll 404, clear activeTaskId, force ``_needsLoad`` and reload
the conversation from the server, re-render, and never stamp the error.

This harness loads the REAL shipped ``sse_poll_fallback.js`` under bare
node, makes ``Api.chat.poll`` return a 404 once, and asserts the re-sync
contract. NEUTER: drop the ``loadConversationMessages`` call → the re-sync
check FAILs.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_FILE = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_poll_fallback.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Window-scope globals _pollFallback reads ──
global.conversations = [];
global.activeConvId = 'conv-404';
global.streamBufs = new Map();
global.twUpdate = () => {};
global.twStop = () => {};
global.finishStreamCalls = [];
global.finishStream = (cid) => { global.finishStreamCalls.push(cid); };
global.saveConversations = () => {};
global.renderChat = () => {};
global.showToast = () => {};
global.debugLog = () => {};
global._reportClientError = () => {};
global._startOfflineRecoveryPolling = () => {};
global._checkServerHealth = async () => true;
global._lastHealthCheck = 0;

// The SoT re-sync seam — capture it.
global.loadCalls = [];
global.loadConversationMessages = async (cid) => {
  global.loadCalls.push(cid);
  // Mirror the real one: server truth replaces the local messages.
  const conv = conversations.find(c => c.id === cid);
  if (conv) {
    conv.messages = [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'interrupted stub', interruptedReason: 'manual' },
    ];
    conv._needsLoad = false;
  }
};
global.ConvView = { replaceAll: () => {} };

// Api.chat.poll returns a 404 once (task lost to a restart).
global.Api = { chat: { poll: async () => ({ ok: false, status: 404 }) } };

const stream = { controller: { signal: { aborted: false } } };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/sse_poll_fallback.js (real)

if (typeof _pollFallback !== 'function') {
  console.log('FAIL fn_exposed _pollFallback missing'); process.exit(0);
}
check('fn_exposed', true);

(async () => {
  const conv = { id: 'conv-404', activeTaskId: 'task-lost-1', _needsLoad: false,
                 messages: [
                   { role: 'user', content: 'q' },
                   { role: 'assistant', content: '', thinking: '', toolRounds: [] },
                 ] };
  conversations.push(conv);
  const assistantMsg = conv.messages[conv.messages.length - 1];
  await _pollFallback('conv-404', 'task-lost-1', stream, assistantMsg);

  // The re-sync fired — server truth was fetched for THIS conv.
  check('resync_called', global.loadCalls.length === 1 && global.loadCalls[0] === 'conv-404');
  // The dead task pointer is cleared.
  check('active_task_cleared', conv.activeTaskId === null);
  // THE CRUX: no terminal error bubble was minted on the assistant message.
  check('no_error_bubble', assistantMsg.error === undefined);
  // The stream was finalized (chrome teardown still happens).
  check('finish_stream_called', global.finishStreamCalls.includes('conv-404'));
  console.log(out.join('\n'));
})();
"""


def _run(tag: str, transform=None) -> str:
    with open(JS_FILE, encoding='utf-8') as f:
        src = f.read()
    if transform is not None:
        src = transform(src)
    js_copy = os.path.join(HERE, f'_poll404_src_{tag}.js')
    harness = os.path.join(HERE, f'_poll404_harness_{tag}.js')
    with open(js_copy, 'w', encoding='utf-8') as f:
        f.write(src)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, js_copy],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (js_copy, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_404_resyncs_from_server_sot():
    out = _run('real')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'poll-404 SoT re-sync failures:\n' + out


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_404_neuter_drop_resync():
    """NEUTER: remove the loadConversationMessages call → the re-sync check
    must FAIL, proving the test pins the re-sync (not just any teardown)."""
    anchor = '                await loadConversationMessages(convId);'
    with open(JS_FILE, encoding='utf-8') as f:
        shipped = f.read()
    assert anchor in shipped, 'NC anchor drifted — the re-sync seam moved'

    def _neuter(src: str) -> str:
        return src.replace(anchor, '                // NC: re-sync removed', 1)

    out = _run('neuter', _neuter)
    assert 'FAIL resync_called' in out, (
        'NC did not bite: removing the re-sync still passed resync_called:\n' + out)
    # The no-error-bubble half is independent of the re-sync call and must
    # still hold (proves the NC removed only the re-sync).
    assert 'PASS no_error_bubble' in out, out


def test_poll_404_never_mints_error_bubble_source():
    """Source complement: the old terminal-error assignment must be GONE from
    the shipped 404 branch (a re-introduction re-opens the twin-bubble path:
    the ghost error tail is what drove the next Continue into pop-and-
    regenerate)."""
    with open(JS_FILE, encoding='utf-8') as f:
        src = f.read()
    assert 'assistantMsg.error = "Task not found"' not in src, (
        'the poll-404 path re-introduced the terminal error bubble — a task '
        'lost to a restart is a TRANSPORT loss, not a task error; re-sync '
        'from the server SoT instead')

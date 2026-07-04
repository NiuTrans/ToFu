"""tests/test_frontend_poll_reconnect_hint.py — regression for the Epic C
sharded-backend affinity re-route hint being IGNORED by the poll fallback.

WHY
---
Under ``TOFU_RUNTIME_STATE_BACKEND=redis`` (multi-replica) the poll endpoint
(``routes/chat.py`` ``chat_poll``) returns ``status:'running'`` PLUS
``reconnect:true`` when the DB holds a live ``running`` checkpoint but the task
is ABSENT from THIS replica's memory — the task is (probably) alive on another
replica and the client must re-route via taskId affinity (charter Epic C §4.1:
report running+reconnect, NOT interrupted, no cross-replica probe).

``_pollFallback`` (static/js/ui/sse_poll_fallback.js) consumed ``data.status``
but NEVER ``data.reconnect``. Its terminal check is ``if (data.status !==
"running")`` — with the hint, status is ``running``, so the loop kept polling
THIS replica (which has no live task) forever → the bubble hung on "running".
(inproc default never sets ``reconnect``, so single-box was unaffected — this
was a latent defect that would activate the moment the redis backend shipped.)

THE FIX
-------
On ``data.reconnect === true && data.status === 'running'`` the poll loop now
re-opens the SSE stream via ``_trySSE`` (taskId affinity routes to the owning
replica). If SSE re-attaches it takes over and the poll yields (return); if it
still fails, polling resumes — bounded by ``_MAX_RECONNECT_ATTEMPTS`` so the
re-open path can never spin forever.

This harness loads the REAL shipped ``sse_poll_fallback.js`` under bare node,
stubs the window globals + ``_trySSE``, and exercises two scenarios:
  A. ``_trySSE`` returns true (re-attaches) → ``_pollFallback`` returns without
     a terminal finishStream driven by poll, and NO further polls happen.
  B. ``_trySSE`` returns false every time → the re-open is attempted at most
     ``_MAX_RECONNECT_ATTEMPTS`` times, then polling continues and the loop
     terminates only when the server finally returns a non-running status.

SOURCE-LEVEL DOUBLE-NEUTER (proven by hand; restored byte-identical):
  • Delete the ``if (data.reconnect === true ...)`` guard block → scenario A's
    ``sse_reopened`` / ``poll_yielded`` assertions FAIL (the loop ignores the
    hint and keeps polling), while the null-resp / other suites stay green.
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

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Window-scope globals _pollFallback reads ──
global.conversations = [];
global.activeConvId = 'conv-shard';
global.streamBufs = new Map();
global.twUpdate = () => {};
global.twStop = () => {};
global.finishStream = () => {};
global.saveConversations = () => {};
global.renderChat = () => {};
global.showToast = () => {};
global.debugLog = () => {};
global._startOfflineRecoveryPolling = () => {};
global._checkServerHealth = async () => true;
global._lastHealthCheck = 0;
global._reportClientError = () => {};
global.updateContextBar = () => {};

// Instrument _trySSE: count calls; behaviour is set per-scenario.
let trySseCalls = 0;
let trySseReturns = false;   // scenario A flips this to true
global._trySSE = async () => { trySseCalls++; return trySseReturns; };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/sse_poll_fallback.js (real)

if (typeof _pollFallback !== 'function') {
  console.log('FAIL fn_exposed _pollFallback missing'); process.exit(0);
}
check('fn_exposed', true);

// A poll response carrying the sharded affinity hint.
function reconnectResp() {
  return { ok: true, status: 200, json: async () => ({
    id: 'task-shard', status: 'running', reconnect: true,
    content: '', thinking: '',
  }) };
}
// A terminal poll response (task settled on this replica).
function doneResp() {
  return { ok: true, status: 200, json: async () => ({
    id: 'task-shard', status: 'done', content: 'final answer', thinking: '',
    finishReason: 'stop',
  }) };
}

(async () => {
  // ══ Scenario A: _trySSE re-attaches → poll yields, no more polls ══
  {
    trySseCalls = 0; trySseReturns = true;
    const conv = { id: 'conv-shard', activeTaskId: 'task-shard', messages: [] };
    conversations.length = 0; conversations.push(conv);
    const stream = { controller: { signal: { aborted: false } } };
    let pollCalls = 0;
    global.Api = { chat: { poll: async () => { pollCalls++; return reconnectResp(); } } };
    const assistantMsg = { content: '', thinking: '' };
    await _pollFallback('conv-shard', 'task-shard', stream, assistantMsg);
    check('A_polled_once', pollCalls === 1);
    check('A_sse_reopened', trySseCalls === 1);
    // Loop must have YIELDED after SSE took over — exactly one poll, no spin.
    check('A_poll_yielded', pollCalls === 1 && trySseCalls === 1);
  }

  // ══ Scenario B: _trySSE keeps failing → bounded re-opens, then polling
  //    continues and terminates on the eventual non-running status. ══
  {
    trySseCalls = 0; trySseReturns = false;
    const conv = { id: 'conv-shard', activeTaskId: 'task-shard', messages: [] };
    conversations.length = 0; conversations.push(conv);
    const stream = { controller: { signal: { aborted: false } } };
    let pollCalls = 0;
    // First 5 polls carry the hint; then the task settles (done) so the loop
    // can terminate. If the fix's bound is honoured, _trySSE is called at most
    // _MAX_RECONNECT_ATTEMPTS (3) times regardless of how many hinted polls.
    global.Api = { chat: { poll: async () => {
      pollCalls++;
      return pollCalls <= 5 ? reconnectResp() : doneResp();
    } } };
    const assistantMsg = { content: '', thinking: '' };
    await _pollFallback('conv-shard', 'task-shard', stream, assistantMsg);
    check('B_terminated', assistantMsg.content === 'final answer');
    check('B_bounded_reopen', trySseCalls <= 3);
    check('B_attempted_reopen', trySseCalls >= 1);
    check('B_kept_polling', pollCalls >= 6);
  }

  console.log('trySseCalls(final)=' + trySseCalls);
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_poll_reconnect_hint_reopens_sse():
    harness = os.path.join(HERE, '_poll_reconnect_hint_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_poll_fallback.js'),  # argv[2]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'poll reconnect-hint handling failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{output}'

"""Regression test: deleting a message/turn from the chat container is
INSTANT (optimistic) — the message leaves the model AND the DOM in the same
task as the click; the server DELETE then runs in the background, and a
failure rolls the local removal back.

WHY
---
``_execDeleteTurn`` used to ``await Api.conversations.deleteMessage(...)``
BEFORE splicing ``conv.messages`` and re-rendering. On a slow tunnel the
click showed no visible effect for the whole round-trip — the message just
sat there, inviting repeated clicks (owner directive 2026-07-31, epic
pt_0b444c0be11a4048: "every button must execute immediately; switch state
first, process the backend as fast as possible").

The contract is now:
1. SAME task as the click (zero awaits): capture the target objects (the
   message +, for a turn, the following assistant) by IDENTITY, splice them
   from ``conv.messages``, update the IndexedDB cache, re-render the chat
   container + turn-nav + sidebar, show the success toast.
2. BACKGROUND: fire the server DELETE (with the stable ``_msgId`` so the
   server resolves any index drift by identity). On ANY failure (network
   null, non-OK, throw) re-insert the captured targets at their ORIGINAL
   positions and surface an error toast.

This drives the REAL shipped ``_execDeleteTurn`` from
``static/js/ui/message_actions.js`` under node with a controllable server
promise. Skips cleanly when node isn't installed.
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


# Harness: define every global _execDeleteTurn touches, then eval the REAL
# message_actions.js so the shipped function runs against our stubs. The
# server promise is resolved MANUALLY after the instant-UI pins are checked,
# so the pins observe the state after the sync prefix but BEFORE the DELETE
# round-trip completes.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// argv[3]: 'success' — server DELETE resolves ok
//          'fail'    — server DELETE resolves non-OK → rollback expected
const scenario = process.argv[3] || 'success';

let conv = {
  id: 'conv-1', title: 'T',
  messages: [
    { role: 'user',      content: 'u1', timestamp: 1, _msgId: 'm1' },
    { role: 'assistant', content: 'a1', timestamp: 2, _msgId: 'm2' },
    { role: 'user',      content: 'u2', timestamp: 3, _msgId: 'm3' },
  ],
  _serverMsgCount: 3,
};
global.conversations = [conv];
global.activeConvId = 'conv-1';
global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
global.activeStreams = new Map();

const calls = { replaceAll: 0, turnNav: 0, convList: 0, cachePut: 0, seq: [] };
const toasts = [];

let _serverResolve, _serverReject;
global.Api = {
  conversations: {
    deleteMessage: (convId, idx, mode, opts) => {
      calls.seq.push('server:' + idx + ':' + mode + ':' + (opts && opts.msgId));
      return new Promise((res, rej) => { _serverResolve = res; _serverReject = rej; });
    },
  },
};
global.ConvCache = { put() { calls.cachePut++; }, remove() {} };
global.window.ConvView = { replaceAll() { calls.replaceAll++; calls.seq.push('ui'); } };
global.buildTurnNav = () => { calls.turnNav++; };
global.renderConversationList = () => { calls.convList++; };
global.showToast = (...a) => toasts.push(a);
global.document = {
  addEventListener() {},
  getElementById() { return null; },
  createElement() {
    return {
      className: '', style: {},
      set innerHTML(v) {}, get innerHTML() { return ''; },
      classList: { add() {} }, remove() {},
      querySelector() { return { style: {} }; },
      addEventListener() {},
    };
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // message_actions.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof _execDeleteTurn !== 'function') { console.log('FAIL fn_missing _execDeleteTurn'); return; }
  check('fn_exposed', true);

  // Delete the FIRST turn (user m1 + assistant m2).
  const p = _execDeleteTurn(0, 'turn');

  // ★ INSTANT-UI pins — observed BEFORE the server promise settles. (On the
  //   old await-first code every one of these FAILS: the splice + re-render
  //   only happened after the DELETE round-trip.)
  check('removed_instantly', conv.messages.length === 1 && conv.messages[0]._msgId === 'm3');
  check('ui_rerendered_before_server', calls.replaceAll === 1);
  check('ui_ordered_before_server', calls.seq.indexOf('ui') !== -1
        && calls.seq.indexOf('ui') < calls.seq.findIndex(s => s.startsWith('server:')));
  check('turn_nav_rebuilt', calls.turnNav >= 1);
  check('sidebar_rerendered', calls.convList >= 1);
  check('cache_updated', calls.cachePut >= 1);
  check('success_toast_early', toasts.some(a => a[0] === 'Turn deleted' && a[1] === 'success'));
  /* Flush microtasks so a regression that SUSPENDS before calling the server
   *   still reaches deleteMessage — keeps the remaining pins observable
   *   instead of crashing the harness on an unset _serverResolve. */
  for (let i = 0; i < 5 && typeof _serverResolve !== 'function'; i++) await Promise.resolve();
  check('server_called_with_identity', calls.seq.some(s => s === 'server:0:turn:m1'));

  if (scenario === 'success') {
    _serverResolve({ ok: true, json: async () => ({ ok: true, deletedIndices: [0, 1] }) });
    await p;
    // Success → the removal stands; no rollback, no error toast, no extra render.
    check('removal_stands', conv.messages.length === 1 && conv.messages[0]._msgId === 'm3');
    check('no_error_toast', !toasts.some(a => a[1] === 'error'));
    check('no_second_render', calls.replaceAll === 1);
  } else {
    _serverResolve({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
    await p;
    // ★ Rollback: both messages restored at their ORIGINAL positions, the UI
    //   re-rendered a second time, and an error toast replaces the optimism.
    check('rollback_restored_count', conv.messages.length === 3);
    check('rollback_original_order', conv.messages[0]._msgId === 'm1'
          && conv.messages[1]._msgId === 'm2' && conv.messages[2]._msgId === 'm3');
    check('rollback_identity', conv.messages[0].content === 'u1' && conv.messages[1].content === 'a1');
    check('rerendered_after_rollback', calls.replaceAll === 2);
    check('error_toast', toasts.some(a => a[0] === 'Delete failed' && a[1] === 'error'));
  }

  console.log(out.join('\n'));
})();
"""


def _run_harness(scenario: str) -> str:
    harness = os.path.join(HERE, f'_delete_turn_opt_harness_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'message_actions.js'),  # argv[2]
             scenario,                                          # argv[3]
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
    assert not fails, f'delete-turn optimistic ({scenario}) failures:\n' + output
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_turn_removes_instantly_then_persists():
    """INSTANT-UI: the turn leaves the model + chat container in the SAME task
    as the click (before the server responds); a successful background DELETE
    then leaves the removal standing with no extra render."""
    output = _run_harness('success')
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_turn_failure_rolls_back():
    """ROLLBACK: a failed background DELETE re-inserts the captured targets at
    their ORIGINAL positions (identity + order preserved), re-renders, and
    surfaces an error toast."""
    output = _run_harness('fail')
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'

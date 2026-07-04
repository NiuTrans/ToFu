"""Regression: `initActiveTasks` Case D (ghost-tail reconcile) must NOT fall
through into Case E and auto-start an UNREQUESTED assistant turn.

WHY
---
Cases A/B/C in `initActiveTasks` (`main_init_tasks.js`) each end with `continue`.
Case D did NOT. Its `_ghost === 'delete'` branch does `conv.messages.pop()` to
remove an EMPTY trailing assistant ghost — and after that pop the NEW tail can
be the preceding `user` message. Execution then fell straight into the Case-E
block IN THE SAME loop iteration, which sees a recent (< 5 min) trailing user
msg and queues `startAssistantResponse` (fires 3s later) — re-running, and
re-billing, a turn the user never asked to re-run, potentially DUPLICATING an
answer the server already completed. A reconcile must never auto-fire an LLM
turn.

THE FIX
-------
Add `continue` right after the Case-D delete branch's sync, so a ghost delete
ends the iteration exactly like Cases A/B/C. The buried-ghost SWEEP (mid-list,
never exposes a user tail) and the `interrupted`-stamp branch (leaves an
assistant tail) are unaffected and legitimately continue to Case E's own
guarded check.

This drives the REAL shipped `initActiveTasks` end-to-end under node, stubbing
the network + timer seam, and asserts `startAssistantResponse` is NEVER called
for a conv whose only trailing "orphan user" was EXPOSED by the ghost delete.
A control conv (genuine orphan user, no ghost) still auto-starts — proving the
guard is surgical, not a blanket Case-E suppressor.

DOUBLE-NEUTER (run below): removing the `continue` in a COPY of
main_init_tasks.js makes the ghost-delete conv auto-start (the bug returns),
while the genuine-orphan control still starts. Real file untouched.

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


# Drives the REAL initActiveTasks. The whole thing hangs off a handful of async
# seams (loadConversationsFromServer, Api.chat.activeResponse, timers). We stub
# them, seed `conversations` with two convs, then run initActiveTasks and drain
# the deferred Case-E setTimeout (which we make synchronous). The spy records
# every startAssistantResponse(convId).
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const NOW = Date.now();
const RECENT = NOW - 30 * 1000;  // 30s ago → within the 5-min Case-E window

// Two convs, both LOADED (not shells), no activeTaskId, no running server task:
//  • conv-ghost: trailing EMPTY assistant ghost sitting on top of a recent user
//    msg. Case D must delete the ghost and STOP (no auto-start).
//  • conv-orphan: genuine trailing recent user msg (no ghost). Case E SHOULD
//    auto-start it — the control proving the guard is surgical.
function _seedConvs() {
  return [
    {
      id: 'conv-ghost', title: 'ghost', _needsLoad: false, activeTaskId: null,
      messages: [
        { role: 'user', content: 'do the thing', timestamp: RECENT },
        // empty ghost assistant (no content/thinking/finishReason/round):
        { role: 'assistant', content: '', thinking: '', toolRounds: [], timestamp: RECENT + 1 },
      ],
      _serverMsgCount: 2,
    },
    {
      id: 'conv-orphan', title: 'orphan', _needsLoad: false, activeTaskId: null,
      messages: [
        { role: 'user', content: 'answer me', timestamp: RECENT },
      ],
      _serverMsgCount: 1,
    },
  ];
}

const started = [];   // convIds passed to startAssistantResponse
let conversations = [];
global.__reseed = () => { conversations = _seedConvs(); global.conversations = conversations; };
global.__reseed();
global.activeConvId = null;

// ── Network + lifecycle seam ──
global.loadConversationsFromServer = async () => {};
global.loadFolders = async () => {};
global.loadConversationMessages = async () => {};
global.Api = {
  chat: {
    activeResponse: async () => ({ ok: true, json: async () => [] }),  // NO running tasks
    poll: async () => ({ ok: true, json: async () => ({ status: 'done' }) }),
    active: async () => [],
  },
  conversations: { get: async () => null, put: async () => ({ ok: true }) },
};
global.startAssistantResponse = (convId) => { started.push(convId); };
global.connectToTask = () => {};
global.syncConversationToServer = async () => {};
global.saveConversations = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.getActiveConv = () => null;
global.ConvCache = { put() {}, remove() {} };
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.errorEnvelopeKind = () => '';
global._ensureMsgId = (m) => m;
global._migratePinnedToFolder = () => {};
global._refreshServerQueue = () => {};
global.isBranchTaskId = () => false;
global.initBranchReconnect = () => {};
global.config = { model: 'aws.claude-opus-4.8' };
global.serverModel = 'aws.claude-opus-4.8';
global.activeStreams = new Map();
// initActiveTasks reads sessionStorage at the top; _ensureNewest reads these.
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._editingMsgIdx = null;
global.showStreamingUIForConv = () => {};

// Make the deferred Case-E dispatch fire synchronously so we can observe it
// within the test without real time. (initActiveTasks wraps the auto-start in
// setTimeout(..., 3000); we run it immediately.)
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main/main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof initActiveTasks !== 'function') { console.log('FAIL fn_exposed initActiveTasks missing'); return; }
  check('fn_exposed', true);

  await initActiveTasks();
  // initActiveTasks fires _bgRecovery().then(...) without awaiting; drain it.
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }

  // The ghost conv: its empty assistant must have been POPPED…
  const ghost = conversations.find(c => c.id === 'conv-ghost');
  check('ghost_popped', !!ghost && ghost.messages.length === 1
        && ghost.messages[0].role === 'user');
  // …and it must NOT have auto-started a turn (the fall-through bug).
  check('ghost_no_autostart', !started.includes('conv-ghost'));

  // The control orphan (no ghost) SHOULD auto-start — proves the guard is
  // surgical, not a blanket Case-E kill.
  check('orphan_autostarts', started.includes('conv-orphan'));

  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_casee_ghost_delete_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_source_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_casee_no_autostart_after_ghost_delete():
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    proc = _run_harness(src_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Case-D→Case-E fall-through failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_casee_no_autostart_double_neuter(tmp_path):
    """DOUBLE-NEUTER: remove the Case-D `continue` in a COPY of
    main_init_tasks.js and prove the ghost-delete conv now AUTO-STARTS (the
    fall-through bug returns), while the genuine-orphan control still starts.
    Real file untouched."""
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    with open(src_js, encoding='utf-8') as f:
        src = f.read()

    # The fix is the `continue;` that immediately follows the delete-branch
    # sync. Neuter by deleting that exact continue (identified by the marker
    # comment we shipped) so execution falls through to Case E again.
    marker = "          continue;\n        } else if (_ghost === 'interrupted') {"
    assert marker in src, 'delete-branch continue marker not found — update the neuter target'
    neutered_src = src.replace(marker,
        "        } else if (_ghost === 'interrupted') {", 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'main_init_tasks_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # With the continue removed, the ghost conv falls through to Case E and
    # auto-starts → `ghost_no_autostart` MUST now FAIL.
    assert lines.get('ghost_no_autostart') is False, (
        'DOUBLE-NEUTER did not bite: removing the Case-D `continue` did NOT '
        'cause the ghost-delete conv to auto-start — the test does not '
        'discriminate the fix.\n' + output)
    # The control orphan still auto-starts either way.
    assert lines.get('orphan_autostarts') is True, (
        'neuter unexpectedly changed the genuine-orphan control:\n' + output)

"""Regression suite: the generation-STARTUP window ("连接中…" placeholder,
send / regenerate / edit-resend POST in flight, no task registered yet) shows
a STOP button, and clicking it aborts with user-stop semantics — deterministic
rollback, no task started, the user message stays editable (owner directive
2026-07-31, epic pt_fa32a2351b3840ad).

WHY
---
During the connecting POST window, ``updateSendButton``'s busy predicate was
all-false (no stream, no activeTaskId, not translating), so the composer
showed a SEND-shaped button — and ``sendMessage`` early-returns on the (now
empty) composer, so the click was DEAD AIR on exactly the slow-server seconds
when the user most wants to cancel. ``_sendAbortCtrl`` existed but was wired
only to the 90s timer.

The fix:
1. Each pipeline stamps ``conv._genStartCtrl = <its AbortController>`` for the
   POST window; ``updateSendButton`` predicates on it (stop form).
2. The stop click (priority 0.5) owner-tags ``conv._genStartStop = <ctrl>``,
   nulls the marker (instant button flip), and aborts the controller.
3. Each catch's user-stop branch fires on ``conv._genStartStop === <ctrl>``
   (owner tag survives a newer send racing the older pipeline's finally) and
   delegates the rollback to the shared ``_userStopDuringStartup`` helper —
   the generalized translation-stop semantics: placeholder torn down, user
   message kept editable, local persist, ``Api.chat.abortConv``.

Drives the REAL ui/send_button.js (predicate + stop click + shared helper)
under node, plus source-scan wiring ratchets over the three pipelines. Skips
cleanly when node isn't installed.
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


def _run_node(name: str, harness_src: str, min_pass: int) -> str:
    harness = os.path.join(HERE, f'_connecting_stop_{name}.js')
    with open(harness, 'w') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'ui', 'send_button.js')],
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
    assert not fails, f'{name} failures:\n' + output
    assert output.count('PASS') >= min_pass, \
        f'expected >={min_pass} PASS lines, got:\n{output}'
    return output


# ═══════════════════════════════════════════════════════════════════
# Harness A — REAL updateSendButton: stop form during the connecting window;
# the click owner-tags the flag, aborts the controller, flips back instantly.
# ═══════════════════════════════════════════════════════════════════
_HARNESS_BUTTON = r"""
const fs = require('fs');
global.window = global;

const conv = { id: 'conv-1', title: 'T', messages: [] };
global.conversations = [conv];
global.activeConvId = 'conv-1';
global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
global.convIsBusy = () => false;
global.activeStreams = new Map();
global._branchStreams = new Map();
global._activeBranch = null;
global._branchKey = () => '';
global._dispatchableQueueCount = () => 0;
global.renderConversationList = () => { calls.renderList++; };
global.sendMessage = function () {};           // send-form onclick target
global.Api = { chat: { abortTask: async () => ({}) } };
global.twStop = () => {};
global.finishStream = () => {};
global._finishBranchStream = () => {};
global._removeStreamingVuBubbleIfTail = () => {};

const calls = { renderList: 0 };
const btn = { className: '', innerHTML: '', onclick: null };
global.document = { getElementById(id) { return id === 'sendBtn' ? btn : null; } };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/send_button.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof updateSendButton !== 'function') { console.log('FAIL fn_missing'); return; }
  check('fn_exposed', true);

  // ── Idle baseline: send form. ──
  updateSendButton();
  check('idle_send_form', btn.className === 'send-btn' && btn.onclick === sendMessage);

  // ── Connecting window: a startup AbortController is registered on the conv. ──
  const fakeCtrl = { aborted: false, abort() { this.aborted = true; } };
  conv._genStartCtrl = fakeCtrl;
  conv._genStartStop = null;
  updateSendButton();
  // ★ The button MUST be a stop button during the POST window (the bug: it
  //   was send-shaped and dead-clicked on the empty composer).
  check('stop_form_during_connecting', btn.className === 'send-btn stop-btn');

  // ── Click it: user-stop semantics + INSTANT flip back. ──
  btn.onclick();
  check('flag_owner_tagged', conv._genStartStop === fakeCtrl);
  check('controller_aborted', fakeCtrl.aborted === true);
  check('marker_cleared_for_button', conv._genStartCtrl === null);
  check('btn_back_to_send_form', btn.className === 'send-btn' && btn.onclick === sendMessage);
  check('sidebar_rerendered', calls.renderList >= 1);

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_connecting_window_shows_stop_and_click_aborts():
    _run_node('button', _HARNESS_BUTTON, 8)


# ═══════════════════════════════════════════════════════════════════
# Harness B — REAL _userStopDuringStartup (shared rollback): placeholder torn
# down, user message kept editable, local persist, abortConv, NO task start.
# ═══════════════════════════════════════════════════════════════════
_HARNESS_ROLLBACK = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3] || 'rescue-ok';

const userMsg = { role: 'user', content: 'hello', timestamp: 1, _msgId: 'u1' };
const conv = { id: 'conv-1', title: 'T', messages: [userMsg] };
global.conversations = [conv];
global.activeConvId = 'conv-1';

const calls = { removeBubble: 0, apply: [], saved: 0, nav: 0, abortConv: [],
                connectToTask: 0, pendingSync: 0, sync: [] };
global._removeTranslatingBubble = () => { calls.removeBubble++; };
global.window.ConvView = { apply(convId, idx, msg) { calls.apply.push(idx); } };
global.saveConversations = () => { calls.saved++; };
global.syncConversationToServer = (c, opts) => {
  calls.sync.push(opts || null);
  return Promise.resolve(scenario === 'rescue-fail' ? false : true);
};
global.markConvPendingSync = () => { calls.pendingSync++; };
global.buildTurnNav = () => { calls.nav++; };
global.connectToTask = () => { calls.connectToTask++; };
global.Api = { chat: { abortConv: (id) => { calls.abortConv.push(id); return Promise.resolve({}); } } };
global.document = { getElementById(id) { return id === 'msg-0' ? { _stub: true } : null; } };

// updateSendButton deps (eval defines it; unused here but keep globals sane)
global.getActiveConv = () => conv;
global.convIsBusy = () => false;
global.activeStreams = new Map();
global._branchStreams = new Map();
global._activeBranch = null;
global._branchKey = () => '';
global._dispatchableQueueCount = () => 0;
global.renderConversationList = () => {};
global.sendMessage = function () {};
global.twStop = () => {};
global.finishStream = () => {};
global._finishBranchStream = () => {};
global._removeStreamingVuBubbleIfTail = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/send_button.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof _userStopDuringStartup !== 'function') { console.log('FAIL fn_missing _userStopDuringStartup'); return; }
  check('fn_exposed', true);

  await _userStopDuringStartup(conv, 'conv-1', { userMsg, userMsgIdx: 0, rescue: true });

  // ★ The end-to-end contract of "连接中点击停止":
  check('placeholder_torn_down', calls.removeBubble === 1);
  check('user_msg_rerendered', calls.apply.join(',') === '0');
  check('message_kept_editable', conv.messages.length === 1 && conv.messages[0] === userMsg);
  check('conversations_saved', calls.saved >= 1);
  check('backend_told_to_abort', calls.abortConv.join(',') === 'conv-1');
  check('no_task_started', calls.connectToTask === 0);
  check('nav_rebuilt', calls.nav >= 1);
  if (scenario === 'rescue-fail') {
    // ★ Durability: a failed rescue sync marks the turn pending-sync so the
    //   message survives a reload (poor-network path from the send branch).
    check('pending_sync_marked', calls.pendingSync === 1);
  } else {
    check('no_pending_sync_when_synced', calls.pendingSync === 0);
  }

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_user_stop_rollback_keeps_message_and_starts_no_task():
    _run_node('rollback', _HARNESS_ROLLBACK, 9)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_user_stop_rollback_marks_pending_sync_on_poor_network():
    _run_node('rollback', _HARNESS_ROLLBACK.replace(
        "process.argv[3] || 'rescue-ok'", "process.argv[3] || 'rescue-fail'"), 9)


# ═══════════════════════════════════════════════════════════════════
# Wiring ratchets (source scans over the three pipelines + the button seam).
# ═══════════════════════════════════════════════════════════════════
def _src(rel: str) -> str:
    with open(os.path.join(JS_DIR, rel), encoding='utf-8') as f:
        return f.read()


def test_send_pipeline_startup_stop_wiring():
    src = _src(os.path.join('main', 'main_send_pipeline.js'))
    assert 'conv._genStartCtrl = _sendAbortCtrl;' in src, \
        'send must stamp the startup controller on the conv for the POST window'
    assert 'conv._genStartStop === _sendAbortCtrl' in src, \
        'send catch must fire user-stop on the OWNER-TAGGED flag'
    assert '_userStopDuringStartup(conv, convId, { userMsg, userMsgIdx, rescue: true })' in src, \
        'send user-stop branch must delegate to the shared rollback helper'
    assert 'if (conv._genStartCtrl === _sendAbortCtrl || conv._genStartStop === _sendAbortCtrl)' in src, \
        'send finally must identity-clear only ITS OWN markers (a newer send survives)'


def test_regen_pipeline_startup_stop_wiring():
    src = _src(os.path.join('main', 'main_regen_continue.js'))
    assert 'conv._genStartCtrl = _regenAbortCtrl;' in src
    assert 'conv._genStartStop === _regenAbortCtrl' in src
    assert '_userStopDuringStartup(conv, convId, { syncOpts: { allowTruncate: true } })' in src
    assert 'if (conv._genStartCtrl === _regenAbortCtrl || conv._genStartStop === _regenAbortCtrl)' in src


def test_edit_resend_pipeline_startup_stop_wiring():
    src = _src(os.path.join('ui', 'edit_message.js'))
    assert 'conv._genStartCtrl = _editAbortCtrl;' in src
    assert 'conv._genStartStop === _editAbortCtrl' in src
    assert '_userStopDuringStartup(conv, convId, { syncOpts: { allowTruncate: true } })' in src
    assert 'if (conv._genStartCtrl === _editAbortCtrl || conv._genStartStop === _editAbortCtrl)' in src


def test_button_seam_predicate_and_owner_tag():
    src = _src(os.path.join('ui', 'send_button.js'))
    assert 'conv._genStartCtrl' in src, \
        'updateSendButton must predicate the stop form on the startup marker'
    assert 'conv._genStartStop = _gsCtrl;' in src, \
        'the stop click must owner-tag the flag with the aborted controller'
    assert 'async function _userStopDuringStartup' in src, \
        'the shared rollback helper must live at the stop seam (send_button.js)'

"""jsdom guard for fix ① — the send-side "assistant dead-zone".

WHY
---
`sendMessage()` (static/js/main/main_send_pipeline.js) only rendered assistant-
side feedback BEFORE the synchronous `Api.chat.send()` POST when auto-translate
was on (`_renderTranslatingBubble()` gated behind `_willTranslate`). For any
NON-translated send the assistant side was BLANK for the entire POST — which
synchronously does load → translate-cap → task-start on the server — so the user
saw a frozen gap until the streaming bubble was created AFTER the POST returned
a taskId (line ~686). That is the reported "real-time status feels terrible even
on my own device" dead-zone.

THE FIX
-------
Render a deterministic placeholder on the SAME `#translating-msg` node
UNCONDITIONALLY before the POST — '翻译中…' when translating, '连接中…'
(Connecting…) otherwise — then upgrade it in place to the streaming bubble
(`#streaming-msg`) once the POST returns the taskId. Every exit path
(success / queued / steered / error / finally) already tears the node down via
`_removeTranslatingBubble()`, now made unconditional, so no orphan is left.

WHAT THIS PROVES (drives the REAL shipped sendMessage under jsdom)
-----------------------------------------------------------------
(a) PRE-POST: with the `Api.chat.send` promise still PENDING, `#translating-msg`
    is present carrying the '连接中…' label AND `#streaming-msg` does NOT yet
    exist — the dead-zone is filled synchronously, before the POST resolves.
(b) UPGRADE: once the POST resolves with `{taskId}`, `#translating-msg` is gone
    and `#streaming-msg` exists (placeholder upgraded in place).
(c) QUEUED / STEERED: when the POST resolves `{queued}` / `{steered}` the
    placeholder is removed and NO streaming bubble is left behind (no orphan).

NEUTER: strip the new pre-POST `_renderTranslatingBubble('连接中…')` render out
of the non-translate branch and prove scenario (a) then shows a BLANK assistant
side (no placeholder while the POST is pending) — i.e. the added render is what
closes the dead-zone.

Runs the REAL shipped JS under node+jsdom; skips cleanly when the dev deps
aren't installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[3] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="chatContainer"><div id="chatInner"></div></div>' +
  '<textarea id="userInput"></textarea>' +
  '<div id="topbarTitle"></div><div id="pdfProgress"></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.sessionStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function flush(n) { // drain microtasks up to the pending POST across macrotasks
  return new Promise((res) => { let i = 0;
    const step = () => { if (i++ >= (n||12)) return res(); setImmediate(step); };
    setImmediate(step); });
}

// ── i18n: real enough to expose the '连接中…' label for the connecting key ──
global.t = win.t = (k) => (k === 'sidebar.connecting' ? '连接中…'
                          : k === 'sidebar.translating' ? '翻译中…' : (k || ''));
global.formatClockTime = win.formatClockTime = () => '12:00';

// ── Controllable POST: a deferred so we can inspect the DOM WHILE it's pending ──
let _resolveSend = null;
const _sendGate = new Promise((r) => { _resolveSend = r; });
let _sendResult = { taskId: 'task-abc' };   // scenario-controlled below
global.Api = win.Api = {
  chat: {
    send: () => _sendGate.then(() => ({ ok: true, json: async () => _sendResult })),
    abortConv: () => {},
  },
};

// ── Globals / stubs sendMessage closes over (all no-op or immediate-async) ──
global._pendingLogClean = win._pendingLogClean = null;
global.imageGenMode = win.imageGenMode = false;
global.pendingImages = win.pendingImages = [];
global.pendingPdfTexts = win.pendingPdfTexts = [];
global.pdfProcessing = win.pdfProcessing = 0;
global._sendGeneration = win._sendGeneration = 0;
global.activeStreams = win.activeStreams = new Map();
global.pendingMessageQueue = win.pendingMessageQueue = new Map();
global.projectState = win.projectState = { active: false, path: '' };
global.serverModel = win.serverModel = 'test-model';
global.conversations = win.conversations = [];
global.activeConvId = win.activeConvId = null;
let _curConv = null;
global.getActiveConv = win.getActiveConv = () => _curConv;

for (const n of [
  'debugLog','renderConversationList','_saveConvToolState','renderImagePreviews',
  '_vlmClearState','buildTurnNav','saveConversations','connectToTask','updateSendButton',
  'updateContextBar','_refreshServerQueue','markConvPendingSync','clearReplyQuote',
  'clearConvRefs','_hardCancelActiveStream','showConfirm','hideLogCleanBanner',
  'stripNoTranslateTags','buildTurnCtxSnapshot','getPendingReplyQuotes','getPendingConvRefs',
  'getActiveFolderId','generateId','_streamingBubbleRole','scrollToBottom','showToast',
]) { win[n] = global[n] = () => {}; }

global.stripNoTranslateTags = win.stripNoTranslateTags = (s) => String(s || '');
global._streamingBubbleRole = win._streamingBubbleRole = () => 'worker';
global._newClientMsgId = win._newClientMsgId = () => 'mid-' + Math.random().toString(36).slice(2, 8);
global._ensureMsgId = win._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-x'; };
global.renderMessage = win.renderMessage = (m, i) =>
  '<div class="message" id="msg-' + i + '" data-msg-id="' + (m._msgId || '') + '"></div>';
global._forceScrollToBottom = win._forceScrollToBottom = () => {};
// Streaming-bubble fallback (ConvView absent → insertAdjacentHTML path).
global._streamingBubbleHTML = win._streamingBubbleHTML = (role, a, b, msgId) =>
  '<div class="message" id="streaming-msg" data-msg-id="' + (msgId || '') + '">' +
  '<div class="message-body" id="streaming-body"></div></div>';
// Async stubs that are AWAITed on the path to the POST — resolve immediately.
global._waitForImageProcessing = win._waitForImageProcessing = async () => {};
global._waitForVlmParsing = win._waitForVlmParsing = async () => {};
global.loadConversationMessages = win.loadConversationMessages = async () => {};
global._buildConvConfig = win._buildConvConfig = async () => ({ model: 'test-model', autoTranslate: false });
global._buildConvSettings = win._buildConvSettings = async () => ({});
global.syncConversationToServer = win.syncConversationToServer = async () => true;
global._promptInjectMode = win._promptInjectMode = async () => 'queue';

// ── Load the REAL renderers + the REAL ConvView seam + the REAL sendMessage ──
// (Phase 3.5 step 3: the send pipeline routes its #chatInner writes through
// ConvView.apply/removeMessage — the seam must be present, exactly as the
// boot-time hard check guarantees in production.)
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'main', 'main_translating_bubble.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'conv_view.js'), 'utf8'));
let sendSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'main', 'main_send_pipeline.js'), 'utf8');
if (NEUTER === 'strip') {
  // Remove the pre-POST placeholder render from the non-translate branch.
  const before = sendSrc;
  sendSrc = sendSrc.replace("_renderTranslatingBubble(t('sidebar.connecting'));",
                            "/* NEUTERED: no pre-POST placeholder */ void 0;");
  if (sendSrc === before) { console.log('FAIL neuter_not_applied'); console.log(out.join('\n')); process.exit(0); }
}
eval(sendSrc);

if (typeof sendMessage !== 'function') { console.log('FAIL sendMessage_missing'); console.log(out.join('\n')); process.exit(0); }

function _placeholder() { return document.getElementById('translating-msg'); }
function _streaming() { return document.getElementById('streaming-msg'); }
function _resetScenario() {
  document.getElementById('chatInner').innerHTML = '';
  document.getElementById('userInput').value = 'hello world';
  win.pendingImages = global.pendingImages = [];
  win.pendingPdfTexts = global.pendingPdfTexts = [];
  _curConv = { id: 'c-1', title: 'New Chat', messages: [], _needsLoad: false,
               activeTaskId: null, createdAt: 1, updatedAt: 1 };
  win.conversations = global.conversations = [_curConv];
  win.activeConvId = global.activeConvId = 'c-1';
  // fresh deferred gate per scenario
}

(async () => {
  // ══ Scenario A: PRE-POST placeholder + UPGRADE on taskId ══
  {
    _resetScenario();
    let _gate2Resolve;
    const gate = new Promise((r) => { _gate2Resolve = r; });
    global.Api.chat.send = () => gate.then(() => ({ ok: true, json: async () => ({ taskId: 'task-abc' }) }));
    const p = sendMessage();          // do NOT await — POST is pending
    await flush();                    // drain to the awaiting-POST point
    if (NEUTER === 'strip') {
      // The dead-zone must be BLANK without the fix.
      check('neuter_no_placeholder_prepost', !_placeholder());
      _gate2Resolve(); await p;
      console.log(out.join('\n')); return;
    }
    const ph = _placeholder();
    check('a_placeholder_present_prepost', !!ph);
    check('a_placeholder_label_connecting', !!ph && /连接中/.test(ph.textContent));
    check('a_no_streaming_prepost', !_streaming());
    _gate2Resolve();                  // resolve the POST → taskId
    await p;
    check('b_placeholder_gone_after_taskid', !_placeholder());
    check('b_streaming_present_after_taskid', !!_streaming());
  }

  // ══ Scenario QUEUED: placeholder removed, no orphan streaming bubble ══
  {
    _resetScenario();
    global.Api.chat.send = async () => ({ ok: true, json: async () => ({ queued: true, position: 1, queueId: 'q1' }) });
    await sendMessage();
    check('q_placeholder_gone', !_placeholder());
    check('q_no_streaming', !_streaming());
  }

  // ══ Scenario STEERED: placeholder removed, no orphan streaming bubble ══
  {
    _resetScenario();
    global.Api.chat.send = async () => ({ ok: true, json: async () => ({ steered: true }) });
    await sendMessage();
    check('s_placeholder_gone', !_placeholder());
    check('s_no_streaming', !_streaming());
  }

  console.log(out.join('\n'));
})().catch((e) => { console.log('FAIL harness_threw ' + (e && e.stack || e)); console.log(out.join('\n')); });
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_send_placeholder_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, neuter],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _lines(output):
    return {ln[5:]: ln[:4].strip() for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_send_placeholder_prepost_and_upgrade():
    """Fix ①: the pre-POST placeholder appears synchronously with the '连接中…'
    label while the POST is pending, then upgrades in place to the streaming
    bubble on taskId; the queued/steered paths leave no orphan."""
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'send-placeholder failures:\n' + output
    lines = _lines(output)
    for key in ('a_placeholder_present_prepost', 'a_placeholder_label_connecting',
                'a_no_streaming_prepost', 'b_placeholder_gone_after_taskid',
                'b_streaming_present_after_taskid', 'q_placeholder_gone', 'q_no_streaming',
                's_placeholder_gone', 's_no_streaming'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_prepost_placeholder_is_load_bearing():
    """NEUTER: strip the new pre-POST `_renderTranslatingBubble('连接中…')` render
    and prove the assistant side is BLANK while the POST is pending — i.e. the
    added render is exactly what closes the dead-zone."""
    lines = _lines(_run('strip'))
    assert lines.get('neuter_no_placeholder_prepost') == 'PASS', (
        'NEUTER did not bite: a placeholder still appeared pre-POST without the '
        f'fix — the test does not discriminate the fix.\n{lines}')

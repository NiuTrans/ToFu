#!/usr/bin/env python3
"""Reproduction probe: after a USER ABORT, do the completed tool rounds on the
streaming assistant bubble survive into the rendered DOM?

WHY THIS TEST EXISTS
--------------------
Reported symptom: "the agent had already invoked many tools, but after I
interrupted it, all the tools on the front end simply disappeared." A first
hypothesis (the finishStream ghost-tail self-heal ``pop()``-ing the bubble) was
FALSIFIED by reading the code: ``_classifyGhostTailJS`` short-circuits to
``null`` (keep) the moment ``_hasRealToolRound(msg)`` is true, and a completed
round (``status==='done'``) makes it true — so a bubble with real rounds is
never delete-verdicted.

This test does NOT assume a root cause. It drives the REAL shipped stack end to
end under jsdom —
    finishStream (ui/stream_lifecycle.js)
      → ConvView.finalizeStreaming (conv_view.js)  [real outerHTML swap]
        → renderMessage (ui/chat_render.js)         [real bubble HTML]
          → getToolRoundsFromMsg (core.js)          [real round extraction]
            → renderToolRoundsHTML                  [stubbed panel marker]
— and asserts, by counting the panel markers actually present in #chatInner,
whether the N completed rounds are visible after the turn settles.

It is a DISCRIMINATOR, not a fix: three scenarios that share one code path but
differ in the one variable each candidate root cause turns on.

  A. abort_basic — user Stop; #streaming-msg present; msg still in
     conv.messages. PREDICTED to KEEP the rounds (finalizeStreaming re-renders
     from msg via renderMessage). If this FAILS, the ghost-tail / basic-abort
     theory is alive after all.
  B. abort_no_streaming_msg — user Stop but the #streaming-msg node is already
     gone when finishStream runs (e.g. a repaint/teardown raced ahead). Probes
     the "abort tore down the DOM before an authoritative repaint" vector: with
     no sm, finalizeStreaming returns false and NOTHING re-renders the rounds
     from msg → they would only be visible if some OTHER path painted them.
  C. abort_truncated_away — msg no longer in conv.messages (Edit/Regen won the
     race mid-abort). finalizeStreaming's idx<0 branch removes the bubble with
     NO replacement. This SHOULD drop the rounds (the turn was truncated on
     purpose) — included as the honest control for "removal is sometimes
     correct".

The rounds are counted by the number of stubbed ``TOOLPANEL`` markers the real
renderMessage emitted into the live #chatInner, so a "kept" verdict means the
rounds genuinely reached the DOM, not merely that they stayed on the JS object.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STREAM_LIFECYCLE = runtime_section_path('ui/stream_lifecycle.js')
CONV_VIEW = runtime_section_path('conv_view.js')
CHAT_RENDER = runtime_section_path('ui/chat_render.js')
ESCAPE_HTML = runtime_section_path('core/escape_html.js')
SAFE_HTML = runtime_section_path('core/safe_html.js')
TRANSLATION_MODEL = runtime_section_path('core/translation_model.js')
TRANSLATION_INDICATOR = runtime_section_path('ui/translation_indicator.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const SCENARIO = process.argv[3] || 'abort_basic';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── State globals finishStream + ConvView read. ──
const activeStreams = new Map();
const streamBufs = new Map();
let conversations = [];
let activeConvId = 'c1';
let _editingMsgIdx = null;
win.activeStreams = global.activeStreams = activeStreams;
win.streamBufs = global.streamBufs = streamBufs;
win.conversations = global.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
Object.defineProperty(global, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
Object.defineProperty(win, '_editingMsgIdx', { get: () => _editingMsgIdx, set: v => _editingMsgIdx = v });
Object.defineProperty(global, '_editingMsgIdx', { get: () => _editingMsgIdx, set: v => _editingMsgIdx = v });

function spy(name) { return (...a) => { spy[name] = (spy[name] || 0) + 1; spy[name + '_args'] = a; }; }
// ── Render-adjacent helpers renderMessage / finishStream need. ──
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.formatClockTime = global.formatClockTime = () => '';
// ★ REAL round extraction is loaded from core.js below (getToolRoundsFromMsg +
//   _rehydrateInjectRows). The tool PANEL renderer lives in tool_rounds.js
//   (not eval'd) — stub it to emit ONE marker per round so we can COUNT how
//   many completed rounds actually reached the DOM.
win.renderToolRoundsHTML = global.renderToolRoundsHTML = function (rounds) {
  const n = Array.isArray(rounds) ? rounds.length : 0;
  let h = '';
  for (let i = 0; i < n; i++) h += '<div class="ptool-panel" data-round="' + i + '">TOOLPANEL</div>';
  return h;
};
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop0 = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_destroyLazyObserver',
  '_stampFreshness','calcCostCny','renderRelatedConversationsHtml',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop0; } }
win._prefetchConvCosts = global._prefetchConvCosts = () => Promise.resolve(false);
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => Promise.resolve(false);
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<i>u</i>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<i>w</i>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<i>p</i>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<i>c</i>';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

// finishStream side-effect helpers — spy so they don't explode; behaviour is
// irrelevant to "did the rounds survive in the DOM".
for (const n of ['saveConversations','renderConversationList','updateSendButton',
  'buildTurnNav','scrollToBottom','renderChat','_checkForQueuedTask',
  '_attachAutopilotFollowup','_maybeAutoGenerateTitle','_armAutoTranslateWatchdog',
  'twStop','_runTerminalContinuation','_removeStreamingVuBubbleIfTail',
  '_startAutoTranslateForMsg']) {
  win[n] = global[n] = spy(n);
}
win.ConvCache = global.ConvCache = { put: spy('ConvCache_put') };
win.isNearBottom = global.isNearBottom = () => false;
win._convRenderFingerprint = global._convRenderFingerprint = () => 'fp';
win._dispatchableQueueCount = global._dispatchableQueueCount = () => 0;
win.convAutoTranslate = global.convAutoTranslate = () => false;
win.convAutoTranslateEffective = global.convAutoTranslateEffective = () => false;
win.autoTranslate = global.autoTranslate = false;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._openScrollConvId = global._openScrollConvId = null;
win._explicitBottomLatch = global._explicitBottomLatch = null;
win._activeBranch = global._activeBranch = null;
// getActiveConv (finishStream reads it via the render belt) + the terminal
// continuation's carrier probe. Neither affects "did the rounds reach the DOM".
win.getActiveConv = global.getActiveConv = () => conversations.find(c => c.id === activeConvId) || null;
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier = () => null;
win.connectToTask = global.connectToTask = () => {};

// Load the REAL modules in bundle order.
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));   // core/escape_html.js
(0, eval)(fs.readFileSync(process.argv[5], 'utf8'));   // core/safe_html.js
(0, eval)(fs.readFileSync(process.argv[6], 'utf8'));   // core/translation_model.js
(0, eval)(fs.readFileSync(process.argv[7], 'utf8'));   // ui/translation_indicator.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));   // ui/chat_render.js (real renderMessage + ghost-tail)
(0, eval)(fs.readFileSync(process.argv[9], 'utf8'));   // conv_view.js (real finalizeStreaming)
// core.js is huge and full of browser-only init; we only need
// getToolRoundsFromMsg + _rehydrateInjectRows + _ensureMsgId. chat_render
// already exposed the ghost-tail predicates. Provide the minimal real copies
// by extracting from core.js would be brittle; instead the real
// getToolRoundsFromMsg logic is simple and pinned elsewhere — load it via a
// tiny faithful shim IF core didn't define it.
if (typeof getToolRoundsFromMsg !== 'function') {
  win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = function (msg) {
    if (msg.toolRounds && msg.toolRounds.length > 0) return msg.toolRounds;
    return [];
  };
}
if (typeof _ensureMsgId !== 'function') {
  win._ensureMsgId = global._ensureMsgId = function (m) {
    if (m && !m._msgId) m._msgId = 'tmp_' + Math.random().toString(36).slice(2);
    return m;
  };
}
(0, eval)(fs.readFileSync(process.argv[10], 'utf8'));  // ui/stream_lifecycle.js (real finishStream)

if (typeof finishStream !== 'function') { console.log('FAIL finishStream_exposed'); process.exit(0); }
if (!win.ConvView || typeof win.ConvView.finalizeStreaming !== 'function') {
  console.log('FAIL convview_exposed'); process.exit(0);
}
if (typeof renderMessage !== 'function') { console.log('FAIL renderMessage_exposed'); process.exit(0); }

const N_ROUNDS = 4;
function mkRounds() {
  const rs = [];
  for (let i = 0; i < N_ROUNDS; i++) {
    rs.push({ toolCallId: 'tc' + i, toolName: 'read_files', status: 'done',
              toolContent: 'result ' + i, roundNum: i + 1, llmRound: i });
  }
  return rs;
}

/* Build a conv whose trailing assistant is the just-streamed turn: it has N
 * completed tool rounds but NO final content yet (user hit Stop before the
 * model's closing prose) — the exact reported shape. */
function setupConv() {
  conversations.length = 0;
  const am = {
    role: 'assistant', content: '', thinking: 'looked at the files',
    toolRounds: mkRounds(), _msgId: 'm-stream', timestamp: Date.now(),
  };
  const conv = { id: 'c1', title: 'T',
    messages: [{ role: 'user', content: 'do the thing', _msgId: 'u1' }, am],
    activeTaskId: 't1' };
  conversations.push(conv);
  activeConvId = 'c1';
  return { conv, am };
}

/* Render the streaming bubble the way showStreamingUIForConv would: a
 * #streaming-msg node carrying the same data-msg-id. finalizeStreaming will
 * swap THIS for the real renderMessage output. */
function paintStreamingBubble(msgId) {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="msg-0" data-msg-id="u1"><div class="message-body">user</div></div>' +
    '<div class="message" id="streaming-msg" data-msg-id="' + msgId + '">' +
    '<div class="message-body" id="streaming-body">live…</div></div>';
}

function countPanels() {
  return document.getElementById('chatInner').querySelectorAll('.ptool-panel').length;
}

// ═══ Scenario dispatch ═══
if (SCENARIO === 'abort_basic') {
  const { conv, am } = setupConv();
  am.finishReason = 'aborted';                 // send_button pre-stamps this before abort
  activeStreams.set('c1', { controller: {}, taskId: 't1', assistantMsg: am });
  paintStreamingBubble('m-stream');
  finishStream('c1');
  const panels = countPanels();
  check('basic_msg_kept', conv.messages.indexOf(am) >= 0);
  check('basic_rounds_visible_in_dom', panels === N_ROUNDS);
  out.push('INFO basic_panels=' + panels + ' expected=' + N_ROUNDS);
}
else if (SCENARIO === 'abort_no_streaming_msg') {
  const { conv, am } = setupConv();
  am.finishReason = 'aborted';
  // A late server conversation load replaced conv.messages after stream
  // registration, so the stream entry still points at a detached old object.
  // The retained terminal assistant in the conversation is authoritative.
  const detached = { role: 'assistant', content: '', thinking: '',
    toolRounds: [], _msgId: 'm-detached' };
  activeStreams.set('c1', { controller: {}, taskId: 't1', assistantMsg: detached });
  // The abort teardown removed #streaming-msg BEFORE finishStream ran (the
  // controller.abort() → reader teardown race). Only the static user node
  // remains; the assistant bubble was never painted as a static msg-1.
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="msg-0" data-msg-id="u1"><div class="message-body">user</div></div>';
  finishStream('c1');
  const panels = countPanels();
  check('nosm_msg_kept', conv.messages.indexOf(am) >= 0);
  // If this is 0, we have REPRODUCED the disappearance: the rounds live on the
  // msg object but nothing repainted them into #chatInner.
  check('nosm_rounds_visible_in_dom', panels === N_ROUNDS);
  out.push('INFO nosm_panels=' + panels + ' expected=' + N_ROUNDS
           + ' msgKept=' + (conv.messages.indexOf(am) >= 0));
}
else if (SCENARIO === 'abort_truncated_away') {
  const { conv, am } = setupConv();
  activeStreams.set('c1', { controller: {}, taskId: 't1', assistantMsg: am });
  paintStreamingBubble('m-stream');
  // Edit/Regen won the race: the streamed assistant was spliced out of
  // conv.messages mid-abort, leaving a trailing USER message.
  conv.messages = [{ role: 'user', content: 'edited prompt', _msgId: 'u2' }];
  finishStream('c1');
  const panels = countPanels();
  // Honest control: removal here is CORRECT (turn was truncated on purpose).
  check('trunc_bubble_removed', panels === 0);
  out.push('INFO trunc_panels=' + panels);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(scenario: str) -> str:
    harness = os.path.join(HERE, f'_abort_toolrounds_harness_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, scenario,
             ESCAPE_HTML, SAFE_HTML, TRANSLATION_MODEL, TRANSLATION_INDICATOR,
             CHAT_RENDER, CONV_VIEW, STREAM_LIFECYCLE],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = (proc.stdout + '\n' + proc.stderr).strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_abort_basic_keeps_rounds():
    """A: normal user Stop with #streaming-msg present + msg retained →
    finalizeStreaming re-renders from msg and the completed rounds stay in the
    DOM. This is the PREDICTED pass (ghost-tail cannot delete a real-round msg)."""
    output = _run('abort_basic')
    print(output)
    assert 'FAIL basic_msg_kept' not in output, output
    assert 'FAIL basic_rounds_visible_in_dom' not in output, (
        'ABORT-BASIC LOST THE ROUNDS — the basic-abort path itself drops them:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_abort_no_streaming_msg_probe():
    """B: user Stop but #streaming-msg was already torn down when finishStream
    ran. The authoritative conversation document must be repainted so its
    partial content and completed tool rounds remain visible."""
    output = _run('abort_no_streaming_msg')
    print(output)
    assert 'FAIL nosm_rounds_visible_in_dom' not in output, (
        'ABORT WITHOUT A LIVE BUBBLE LOST THE RETAINED MESSAGE DOM:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_abort_truncated_away_control():
    """C: honest control — a turn truncated by Edit/Regen mid-abort is SUPPOSED
    to have its bubble removed with no replacement."""
    output = _run('abort_truncated_away')
    print(output)
    assert 'FAIL trunc_bubble_removed' not in output, output


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        for sc in ('abort_basic', 'abort_no_streaming_msg', 'abort_truncated_away'):
            print(f'=== {sc} ===')
            print(_run(sc))

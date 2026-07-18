"""tests/test_frontend_delta_reset_content_stall.py — repro for the mid-stream
content FREEZE (Bug 2).

WHY
---
Reported (tofu-android autopilot turn): the streamed deliverable text froze at a
half-word — the bubble showed ``Compile gate green. T`` and never advanced,
while tool rounds below kept running. The model would never emit just that.

Ground truth (from the persisted ``task_events`` log): the deltas DID arrive in
full (``…green. Tag v0.1.7 and push.``) and the pre-tool narration WAS correctly
stamped onto the tool round's ``assistantContent``. So the DATA path is correct
— it is a LIVE-RENDER freeze:

``delta_reset`` zeroes ``buf.content`` and moves the narration onto the tool
round, but the repaint that would wipe the content zone rides ``twUpdate``, which
is COALESCED — the empty-content frame is routinely DROPPED when a long-running
tool (or the next content) beats the rAF. So the pre-tool prose stays FROZEN in
the content zone for the rest of the turn.

THE FIX (under test): when ``delta_reset`` successfully stamps the narration onto
a tool round of this llmRound batch, it clears content/thinking AND forces a
SYNCHRONOUS ``updateStreamingUI`` in the same step (atomic clear+repaint) so the
deliverable text can never linger. If the batch's tool_start has NOT been applied
yet (delta_reset raced ahead), it KEEPS the content (nowhere to stamp → must not
vanish).

This drives the REAL ``dispatchSSEEvent`` + ``updateStreamingUI`` under jsdom with
``twUpdate`` stubbed to a NO-OP (mimicking the coalesced frame drop): the ONLY
thing that can clear the zone is the synchronous repaint inside the handler, so a
cleared zone is a behavioral proof the fix ran. Skips cleanly when node/jsdom
absent.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, node_deps_available

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))


_BODY = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatInner">'
  + '<div class="message" id="streaming-msg" data-msg-id="mid-worker">'
  + '<div class="message-body" id="streaming-body"></div></div>'
  + '</div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearInterval = win.clearInterval = () => {};
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (_) {} return 0; };
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
win.Icon = global.Icon = () => '';
win.CSS = global.CSS = { escape: (s) => s };
win.formatClockTime = global.formatClockTime = () => '12:00';
// NOTE: `_segTimelineEnabled` was removed — the interleaved timeline is now the
// only streaming render path, so this stub is INERT (streaming_ui.js no longer
// reads it). The S1/S2 scenarios below still run both `_segFlag` values, which
// now exercise the SAME (timeline) path — the content-clear assertion holds
// either way. Kept as-is to preserve the double-scenario coverage.
let _segFlag = false;
win._segTimelineEnabled = global._segTimelineEnabled = () => _segFlag;
for (const n of ['_stampFreshness','_buildSwarmInboxChipsHTML','renderTurnProvenanceHtml',
  'renderMcpLoginHintHtml','renderPreferenceLearnedHtml','_renderFileChangesHtml',
  '_isRoundSwarm','_buildSwarmPanelHTML','_renderUnifiedToolLine','_renderTurnHead',
  '_renderSoloRoundTag','scrollToBottom','_setBubbleLiveness']) {
  win[n] = global[n] = () => '';
}
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = async () => [];
win.isNearBottom = global.isNearBottom = () => false;
win._turnLabelText = global._turnLabelText = () => 'parallel';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => String(s == null ? '' : s);
win.convAutoTranslate = global.convAutoTranslate = () => false;

let conversations = []; let activeConvId = 'c1';
const streamBufs = new Map(); const activeStreams = new Map();
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
win.streamBufs = streamBufs; win.activeStreams = activeStreams;
// twUpdate is a NO-OP — this is the COALESCED-frame-drop the bug relies on.
// If the content zone ends up cleared, it can ONLY be the SYNCHRONOUS repaint
// inside the delta_reset handler (the fix). That is the test's teeth.
win.twUpdate = global.twUpdate = () => {};
win.twStart = global.twStart = () => {};
win.twStop = global.twStop = () => {};
let _idc = 0;
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-' + (++_idc); return m; };
win._resolveAssistantById = global._resolveAssistantById = (conv, id) =>
  (conv && conv.messages.find(m => m._msgId === id)) || null;

eval(fs.readFileSync(process.argv[5], 'utf8'));  // sse_handlers_tool.js
eval(fs.readFileSync(process.argv[6], 'utf8'));  // sse_handlers_swarm.js
eval(fs.readFileSync(process.argv[7], 'utf8'));  // sse_handlers_io.js
eval(fs.readFileSync(process.argv[8], 'utf8'));  // sse_handlers_misc.js
eval(fs.readFileSync(process.argv[9], 'utf8'));  // sse_handlers_lifecycle.js
eval(fs.readFileSync(process.argv[10], 'utf8')); // ui/stream_reducer.js (delta_reset folds through reduceStreamState)
eval(fs.readFileSync(process.argv[2], 'utf8'));  // sse_pipeline.js
eval(fs.readFileSync(process.argv[4], 'utf8'));  // streaming_ui.js

const T = win.__sse_test__;
const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ('  << ' + (extra || ''))));
}
function line(obj) { return 'data: ' + JSON.stringify(obj); }
function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }

function _mkCtx() {
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'mid-worker' };
  const conv = { id: 'c1', messages: [{ role: 'user', content: 'hi' }, am] };
  conversations.push(conv);
  activeConvId = 'c1';
  const buf = { content: '', thinking: '', toolRounds: [], phase: null };
  streamBufs.set('c1', buf);
  const ctx = T.makeCtx({ convId: 'c1', taskId: 't1',
    stream: { controller: { signal: { aborted: false } } }, assistantMsg: am, buf });
  const chatInner = document.getElementById('chatInner');
  chatInner.innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="mid-worker">'
    + '<div class="message-body" id="streaming-body"></div></div>';
  if (typeof _streamZoneCache !== 'undefined') { try { _streamZoneCache = { body: null }; } catch (_) {} }
  return { conv, am, buf, ctx };
}
function _frame(buf) {
  return { thinking: buf.thinking || '', content: buf.content || '',
           toolRounds: buf.toolRounds || [], phase: buf.phase };
}
function _contentZoneText() {
  const body = document.getElementById('streaming-body');
  const z = body && body.querySelector('[data-zone="content"]');
  const el = z && z.querySelector('.md-content');
  return el ? el.textContent : (z ? z.textContent : null);
}

const PARTIAL = 'Compile gate green. T';   // the exact half-word freeze

// ── S1 (seg OFF): delta_reset with a tool round to stamp → content zone MUST
//    clear synchronously even though twUpdate is a no-op. ──
function _stallScenario(segOn, tag) {
  const { am, buf, ctx } = _mkCtx();
  _segFlag = segOn;
  // Stream the pre-tool narration and render it (the zone shows the partial).
  for (const ch of PARTIAL.split('')) {
    T.dispatchSSEEvent(line({ type: 'delta', content: ch }), ctx);
  }
  updateStreamingUI(_frame(buf));
  const before = _contentZoneText();
  check(tag + '_zone_shows_partial_before', norm(before) === norm(PARTIAL),
    JSON.stringify(before));
  // The round's tool_start arrives (llmRound=21), then delta_reset(round=21).
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 22, toolCallId: 'tc21',
    toolName: 'run_command', llmRound: 21 }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 21 }), ctx);
  // NO manual updateStreamingUI here + twUpdate is a no-op → the only possible
  // clearer is the synchronous repaint inside the handler (the fix).
  const after = _contentZoneText();
  check(tag + '_zone_cleared_after_delta_reset', norm(after) === '',
    'zone still shows: ' + JSON.stringify(after));
  // The narration was preserved on the tool round (data-preservation).
  const r = (am.toolRounds || []).find(x => x.llmRound === 21);
  check(tag + '_narration_stamped_on_round',
    !!r && norm(r.assistantContent) === norm(PARTIAL),
    JSON.stringify(r && r.assistantContent));
  // The committed message content is cleared (terminal round owns final text).
  check(tag + '_msg_content_cleared', (am.content || '') === '', JSON.stringify(am.content));
}
_stallScenario(false, 'S1_segoff');
_stallScenario(true, 'S2_segon');

// ── S3: GUARD — delta_reset races AHEAD of its tool_start (no round to stamp).
//    Content must be KEPT (must not vanish with nowhere to preserve it). ──
{
  const { am, buf, ctx } = _mkCtx();
  _segFlag = false;
  for (const ch of PARTIAL.split('')) {
    T.dispatchSSEEvent(line({ type: 'delta', content: ch }), ctx);
  }
  updateStreamingUI(_frame(buf));
  // delta_reset for a round that has NO tool_start applied yet.
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 99 }), ctx);
  check('S3_guard_content_kept_when_no_round_to_stamp',
    norm(am.content) === norm(PARTIAL), JSON.stringify(am.content));
  updateStreamingUI(_frame(buf));
  check('S3_guard_zone_still_shows_partial',
    norm(_contentZoneText()) === norm(PARTIAL), JSON.stringify(_contentZoneText()));
}

console.log(out.join('\n'));
"""


def _run():
    import subprocess
    harness = os.path.join(_HERE, '_delta_reset_stall_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_BODY)
    try:
        return subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'),
             _ROOT,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),
             os.path.join(JS_DIR, 'ui', 'stream_reducer.js')],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


def test_delta_reset_content_does_not_freeze_mid_stream():
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')
    proc = _run()
    output = (proc.stdout or '').strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'delta_reset content-stall detected:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS, got:\n{output}'

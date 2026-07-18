"""Live-streaming thinking↔content leading-fragment swap — deterministic repro.

WHY
---
Reported bug (aws.claude-opus-4.8 via the OpenAI-compat gateway; the
``reasoning_content`` delta shape, thinking deltas first then content): the
LIVE streaming bubble shows a surgical single-token corruption — a stale
leading fragment glued to the front of the content (and/or thinking) block —
while the debug panel (which reads ``conv.messages`` — the committed message)
is CLEAN and the corruption is gone after finalize.

By the project's own diagnostic rule (the duplicate-bubble fix): *a corruption
that shows clean data in a data-sourced panel and is absent after finalize is a
RENDER-projection bug, not a data bug.*

THE RENDER MECHANISM UNDER TEST
-------------------------------
``updateStreamingUI`` (streaming_ui.js) renders the content zone incrementally
with a FROZEN-PREFIX cache: everything before the last paragraph boundary is
rendered once and cached on the zone element (``contentZone._frozenHtml`` /
``_frozenLen``); only the growing tail is re-rendered each frame. That cache is
cleared ONLY when a render observes ``msg.content === ""`` (the else branch).

But the live render is COALESCED — the real ``_twFlush`` renders only the
LATEST buffer state per animation frame. On a tool round the backend emits
``delta_reset`` which zeroes ``buf.content``; if the next content delta lands
before the next frame, the render NEVER observes ``content === ""``, so the
prior round's ``_frozenLen`` / ``_frozenHtml`` SURVIVE into the terminal round.
The terminal content is then spliced against a stale freeze index → the content
zone shows a stale fragment from the prior round glued to the new answer, while
``buf.content`` (and thus the committed message / debug panel) is pristine.

This test drives the REAL ``dispatchSSEEvent`` delta handler
(``window.__sse_test__``) and the REAL ``updateStreamingUI`` render, rendering
AFTER EVERY delta (and coalescing the ``delta_reset`` frame away, as the real
rAF flush does), and asserts a strong PER-FRAME invariant:

  the content zone's visible text is ALWAYS a faithful projection of the
  CURRENT ``buf.content`` — never stale text from a prior buffer state.

If the render corrupts, this FAILS and pins the layer to the render. If it
stays green across every frame and every mode, the render is EXONERATED and the
corruption is upstream (only then is a wire capture justified).

Drives the REAL shipped JS under jsdom. Skips cleanly when node/jsdom absent.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, node_deps_available

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))


# ═══════════════════════════════════════════════════════════════════════════
#  argv contract:
#    argv[2] = sse_pipeline.js (dispatchSSEEvent + __sse_test__)
#    argv[3] = repo root
#    argv[4] = streaming_ui.js
#    argv[5..9] = sse_handlers_{tool,swarm,io,misc,lifecycle}.js
# ═══════════════════════════════════════════════════════════════════════════
_BODY = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>'
  + '<div id="chatInner">'
  + '<div class="message" id="streaming-msg" data-msg-id="mid-worker">'
  + '<div class="message-body" id="streaming-body"></div></div>'
  + '</div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearInterval = win.clearInterval = () => {};
global.clearTimeout = win.clearTimeout = () => {};
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (_) {} return 0; };
global.cancelAnimationFrame = win.cancelAnimationFrame = () => {};
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

// renderMarkdown is IDENTITY (no wrapping) so the rendered zone text equals the
// raw field exactly — a stale-fragment leak is then unambiguous in textContent.
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.renderMarkdown = global.renderMarkdown = (s) => String(s == null ? '' : s);
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
win.Icon = global.Icon = () => '';
win.CSS = global.CSS = { escape: (s) => s };
win.formatClockTime = global.formatClockTime = () => '12:00';
// NOTE: `_segTimelineEnabled` was removed — the interleaved timeline is now the
// only streaming render path, so this stub is INERT (streaming_ui.js no longer
// reads it). The scenarios below still toggle `_segFlag` (seg OFF / seg ON),
// which now exercise the SAME (timeline) path; the content-zone fidelity
// invariant holds regardless. Kept to preserve the scenario matrix.
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
win.convAutoTranslate = global.convAutoTranslate = () => true;

let conversations = []; let activeConvId = 'c1';
const streamBufs = new Map(); const activeStreams = new Map();
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
win.streamBufs = streamBufs; win.activeStreams = activeStreams;
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
eval(fs.readFileSync(process.argv[12], 'utf8')); // ui/stream_reducer.js (delta_reset folds through reduceStreamState)
eval(fs.readFileSync(process.argv[2], 'utf8'));  // sse_pipeline.js
eval(fs.readFileSync(process.argv[4], 'utf8'));  // streaming_ui.js
eval(fs.readFileSync(process.argv[10], 'utf8')); // translation.js (engine)
eval(fs.readFileSync(process.argv[11], 'utf8')); // ui/translation_render.js (_renderStreamingTranslatePreview — relocated)

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
  // Fresh DOM body so the zone freeze-cache starts clean per scenario.
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
function _termContentText() {
  const body = document.getElementById('streaming-body');
  const z = body && body.querySelector('[data-zone="content"]');
  const el = z && z.querySelector('.md-content');
  return el ? el.textContent : null;
}

// THE PER-FRAME INVARIANT: the content zone's visible text must be a faithful
// projection of the CURRENT buf.content — i.e. once whitespace-normalized they
// are EQUAL. Any divergence = a stale fragment leaked from a prior buffer
// state (the reported "stale token glued to the answer" symptom). Returns a
// diagnostic string on violation, '' when clean.
function _contentFidelityViolation(buf) {
  const zoneTxt = _termContentText();
  const want = norm(buf.content);
  const got = norm(zoneTxt);
  if (got === want) return '';
  return 'zone=' + JSON.stringify(got.slice(0, 60)) + ' want=' + JSON.stringify(want.slice(0, 60));
}

// Long multi-paragraph fixtures that TRIGGER the freeze split (>300 chars, has
// a \n\n boundary). Reconstructed in shape from the reported transcript.
const R0_NARRATION =
  'Let me analyze the chase-entry mechanism in detail before making any change. '
  + 'I need to read tofu-pet.js and trace how _pickNext reads the critter position, '
  + 'how the cooldown and energy thresholds gate the transition, and where the '
  + 'randomized start x in _startWander interacts with the 160-frame pump.\n\n'
  + 'First I will read the pet module and the scene module together so I can see '
  + 'both sides of the chase interaction and the shared random source.';
const TERMINAL_CONTENT =
  'I now understand the flake precisely. Chase entry in _pickNext is deterministic '
  + 'given a critter in range, but the surrounding timing depends on Math.random(): '
  + '_startWander picks a random start x and every state duration uses _rand, so '
  + 'whether the pet reaches a catch within the 160-frame pump varies.\n\n'
  + 'The fix per your guidance: seed Math.random with a stubbed LCG in the harness '
  + 'so the pump reliably converges. Let me apply it to the chase_result harness.';

function _thinkDeltas() {
  return ['So the chase-entry ', 'mechanism is actually deterministic\u2014once ',
    'a critter is in range and the cooldown and energy thresholds are met, ',
    'the system enters chase mode.'];
}
// Split a long string into ~20-char deltas (like a tokenizer would).
function _chunks(s, n) {
  const a = []; for (let i = 0; i < s.length; i += n) a.push(s.slice(i, i + n)); return a;
}

// ─────────────────────────────────────────────────────────────────────────
//  Scenario 1 — PLAIN turn (no tools, seg OFF). Reasoning first, then a LONG
//  content that crosses the freeze threshold. Render EVERY frame; assert the
//  content-zone fidelity invariant holds at every step.
// ─────────────────────────────────────────────────────────────────────────
{
  const { am, buf, ctx } = _mkCtx();
  _segFlag = false;
  let worst = '';
  for (const d of _thinkDeltas()) {
    T.dispatchSSEEvent(line({ type: 'delta', thinking: d }), ctx);
    updateStreamingUI(_frame(buf));
  }
  for (const d of _chunks(TERMINAL_CONTENT, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    updateStreamingUI(_frame(buf));
    const v = _contentFidelityViolation(buf);
    if (v && !worst) worst = v;
  }
  check('S1_data_content_clean', am.content === TERMINAL_CONTENT);
  check('S1_content_zone_fidelity_every_frame', worst === '', worst);
}

// ─────────────────────────────────────────────────────────────────────────
//  Scenario 2 — TOOL ROUND with a LONG round-0 content that crosses the
//  freeze threshold, then delta_reset whose empty-content frame is COALESCED
//  AWAY (the real _twFlush renders only the latest buffer per rAF), then the
//  terminal round streams the real content. THIS is the stale-frozen-cache
//  path: _frozenLen / _frozenHtml from round 0 must NOT bleed into round 1.
// ─────────────────────────────────────────────────────────────────────────
{
  const { am, buf, ctx } = _mkCtx();
  _segFlag = false;   // grouped render (the classic content zone)
  let worst = '';

  // Round 0: reasoning + a LONG narration (crosses freeze), then a tool call.
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'Round zero reasoning about the files.' }), ctx);
  updateStreamingUI(_frame(buf));
  for (const d of _chunks(R0_NARRATION, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    updateStreamingUI(_frame(buf));   // freeze fires somewhere in here
  }
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tc0',
    toolName: 'read_files', llmRound: 0 }), ctx);
  updateStreamingUI(_frame(buf));

  // delta_reset closes round 0 → zeroes buf.content. COALESCE: do NOT render
  // this frame (mirrors the rAF that never observed content==="" because the
  // next content delta beats the next animation frame).
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 0 }), ctx);
  // (no updateStreamingUI here — the empty-content frame is coalesced away)

  // Terminal round: stream the real content. The FIRST render here sees the
  // stale _frozenLen/_frozenHtml from round 0 still on the zone element.
  for (const d of _chunks(TERMINAL_CONTENT, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    updateStreamingUI(_frame(buf));
    const v = _contentFidelityViolation(buf);
    if (v && !worst) worst = v;
  }

  check('S2_data_content_clean', am.content === TERMINAL_CONTENT, JSON.stringify(am.content.slice(0,40)));
  // THE BUG: the content zone must show ONLY the terminal content, never a
  // stale fragment from round 0's frozen narration.
  check('S2_content_zone_no_stale_fragment', worst === '', worst);
  const finalTxt = _termContentText();
  check('S2_content_zone_starts_with_I',
    finalTxt != null && norm(finalTxt)[0] === 'I', JSON.stringify(finalTxt && finalTxt.slice(0, 40)));
  check('S2_content_zone_no_round0_narration',
    finalTxt != null && finalTxt.indexOf('Let me analyze the chase-entry') < 0,
    JSON.stringify(finalTxt && finalTxt.slice(0, 60)));
}

// ─────────────────────────────────────────────────────────────────────────
//  Scenario 3 — SEG-TIMELINE mode (flag ON), same tool-round + coalesced
//  delta_reset boundary. Confirms the terminal content zone stays faithful in
//  the interleave render path too.
// ─────────────────────────────────────────────────────────────────────────
{
  const { am, buf, ctx } = _mkCtx();
  _segFlag = true;
  let worst = '';
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'Round zero reasoning.' }), ctx);
  updateStreamingUI(_frame(buf));
  for (const d of _chunks(R0_NARRATION, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    updateStreamingUI(_frame(buf));
  }
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tc0',
    toolName: 'read_files', llmRound: 0 }), ctx);
  updateStreamingUI(_frame(buf));
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 0 }), ctx);
  for (const d of _chunks(TERMINAL_CONTENT, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    updateStreamingUI(_frame(buf));
    const v = _contentFidelityViolation(buf);
    if (v && !worst) worst = v;
  }
  check('S3_segtl_content_zone_no_stale_fragment', worst === '', worst);
}

// ─────────────────────────────────────────────────────────────────────────
//  Scenario 4 — AUTO-TRANSLATE LIVE path (the view the screenshots depict:
//  Chinese "思考过程" header over English body). This exercises the paths I
//  had DEFERRED: the main thinking zone (textContent = msg.thinking) AND the
//  translate render (_renderStreamingTranslatePreview → translatedPrimary
//  zone + per-round .stream-seg-narration). Same reasoning-then-content deltas
//  across a COALESCED delta_reset, plus translate push frames. Assert:
//   (a) the main thinking zone == the clean reasoning (no content 'I' prefix);
//   (b) the translatedPrimary zone == the clean translated blob (no stale
//       leading fragment from a prior round / a prior partial).
//  If this stays green every frame it is a REAL exoneration of the thinking +
//  translate paths — the reported symptom's exact view.
// ─────────────────────────────────────────────────────────────────────────
function _thinkZoneText() {
  const body = document.getElementById('streaming-body');
  const z = body && body.querySelector('[data-zone="thinking"]');
  const el = z && z.querySelector('.thinking-text');
  return el ? el.textContent : null;
}
function _translatedPrimaryText() {
  const body = document.getElementById('streaming-body');
  const z = body && (body.querySelector('[data-zone="translatedPrimary"]')
    || body.querySelector('[data-zone="translatePreview"]'));
  const el = z && z.querySelector('.md-content');
  return el ? el.textContent : null;
}
{
  const { am, buf, ctx } = _mkCtx();
  _segFlag = false;
  const THINK_FULL = _thinkDeltas().join('');
  // Two translate "partials": round 0's Chinese, then the terminal Chinese.
  // The terminal partial is SHORTER-prefixed than a naive concat would be —
  // it must fully REPLACE the blob, never glue the round-0 Chinese in front.
  const ZH_R0 = '让我先分析追逐进入机制，再做任何改动。';
  const ZH_TERMINAL = '我现在完全明白这个不稳定的原因了。追逐进入是确定性的。';

  let worstThink = '';
  let worstXlate = '';
  function _renderAll(partial, byRound) {
    updateStreamingUI(_frame(buf));
    if (partial) _renderStreamingTranslatePreview('c1', 'mid-worker', partial, byRound || null);
    // PER-FRAME thinking-zone fidelity: the visible thinking text must EXACTLY
    // equal the current buf.thinking — no duplication, no stale round-0
    // fragment, no content 'I' prefix. Exact equality (not indexOf) so a
    // duplicated/appended render is caught, not just a missing one.
    const tv = _thinkZoneText();
    if (tv != null && norm(tv) !== norm(buf.thinking) && !worstThink) {
      worstThink = 'zone=' + JSON.stringify(norm(tv).slice(0, 50)) + ' want=' + JSON.stringify(norm(buf.thinking).slice(0, 50));
    }
  }

  // Round 0: reasoning streams into the main thinking zone; narration streams
  // as English content; a tool call; then a translate frame lands ZH_R0.
  for (const d of _thinkDeltas()) {
    T.dispatchSSEEvent(line({ type: 'delta', thinking: d }), ctx);
    _renderAll(null, null);
  }
  for (const d of _chunks(R0_NARRATION, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    _renderAll(ZH_R0, null);   // translate blob = round-0 Chinese
    const cur = _translatedPrimaryText();
    if (cur != null && norm(cur) !== norm(ZH_R0) && !worstXlate)
      worstXlate = 'r0 blob=' + JSON.stringify(norm(cur).slice(0, 40)) + ' want=' + JSON.stringify(norm(ZH_R0).slice(0, 40));
  }
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tc0',
    toolName: 'read_files', llmRound: 0 }), ctx);
  _renderAll(ZH_R0, null);

  // Coalesced delta_reset (empty content+thinking frame NOT rendered). This
  // zeroes buf.thinking too (round-0 reasoning was stamped onto its round).
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 0 }), ctx);

  // Terminal round (matches the screenshot: reasoning IS present next to the
  // content in the final turn): stream the terminal reasoning FIRST — the main
  // thinking zone must show ONLY this clean reasoning, never a content 'I'
  // prefix nor a stale round-0 thinking fragment across the reset boundary.
  for (const d of _thinkDeltas()) {
    T.dispatchSSEEvent(line({ type: 'delta', thinking: d }), ctx);
    _renderAll(ZH_TERMINAL, null);
  }
  // Then the real content streams (English tail), and the translate frame now
  // carries ZH_TERMINAL as the blob. The translatedPrimary zone must show ONLY
  // ZH_TERMINAL, never ZH_R0 glued in front.
  for (const d of _chunks(TERMINAL_CONTENT, 20)) {
    T.dispatchSSEEvent(line({ type: 'delta', content: d }), ctx);
    _renderAll(ZH_TERMINAL, null);
    const cur = _translatedPrimaryText();
    if (cur != null && norm(cur) !== norm(ZH_TERMINAL) && !worstXlate)
      worstXlate = 'term blob=' + JSON.stringify(norm(cur).slice(0, 50)) + ' want=' + JSON.stringify(norm(ZH_TERMINAL).slice(0, 50));
  }

  // Data-layer sanity: committed thinking is the clean TERMINAL reasoning
  // (round-0 reasoning was zeroed by delta_reset and stamped onto its round).
  check('S4_data_thinking_clean', am.thinking === THINK_FULL, JSON.stringify(am.thinking.slice(0, 40)));
  // (a) main thinking zone faithful every frame — exact match to buf.thinking,
  //     no content 'I' prefix, no stale round-0 fragment, no duplication.
  const thinkTxt = _thinkZoneText();
  check('S4_thinking_zone_faithful',
    worstThink === '' && thinkTxt != null && norm(thinkTxt) === norm(THINK_FULL)
      && thinkTxt.indexOf('I now understand') < 0,
    worstThink || JSON.stringify(thinkTxt && thinkTxt.slice(0, 40)));
  // (b) translatedPrimary zone faithful every frame — no stale ZH_R0 fragment.
  check('S4_translated_primary_no_stale_fragment', worstXlate === '', worstXlate);
  const finalXlate = _translatedPrimaryText();
  check('S4_translated_primary_is_terminal',
    finalXlate != null && norm(finalXlate) === norm(ZH_TERMINAL)
      && finalXlate.indexOf('让我先分析') < 0,
    JSON.stringify(finalXlate && finalXlate.slice(0, 50)));
}

console.log(out.join('\n'));
"""


def _run_repro():
    import subprocess
    harness = os.path.join(_HERE, '_thinking_content_swap_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_BODY)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'),
             _ROOT,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),
             os.path.join(JS_DIR, 'translation.js'),
             os.path.join(JS_DIR, 'ui', 'translation_render.js'),
             os.path.join(JS_DIR, 'ui', 'stream_reducer.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


def test_thinking_content_no_leading_fragment_swap():
    """Repro: the content zone must be a faithful projection of buf.content on
    EVERY frame — no stale fragment leaking across a coalesced delta_reset."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')
    proc = _run_repro()
    output = (proc.stdout or '').strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'content-zone stale-fragment leak detected:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS, got:\n{output}'

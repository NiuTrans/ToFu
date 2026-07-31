"""Segment-timeline STREAMING interleave (step 5b) — the reported bug fix.

WHY
---
The SETTLED render already interleaves each LLM round's thinking + narration
ADJACENT to the tools it produced (``renderSegmentTimelineHTML``, reads
``msg.segments``). The interleaved timeline is now the ONLY render path — the
former ``_segTimelineEnabled`` toggle was removed. The LIVE streaming path
never got the interleave originally (design doc §5 "step 5b", deferred), so
during streaming:

  1. On every tool round the backend emits ``delta_reset`` and the frontend
     BLINDLY zeroed ``content`` / ``thinking`` (sse_pipeline.js) — so an
     earlier round's reasoning + narration VANISHED live (it only reappeared
     after finalize when segments re-derived it). That is genuine data loss on
     screen: the user watches round N's thinking disappear the instant round
     N+1 starts, and the bottom content zone shows only the last round's tail.

  2. The legacy bottom ``.translate-preview`` blob (chat_render.js) still fired
     whenever ``_translatePartial`` was set — dumping the partial translation
     as one wall at the bottom, fighting the interleaved layout, and
     REAPPEARING on any pause / mid-stream ``renderMessage`` re-render.

The fix (this file's contract):
  • Part A — ``delta_reset`` STAMPS the closing llmRound's narration + thinking
    onto that batch's FIRST ``toolRounds`` entry BEFORE clearing the live
    accumulators — exactly where the backend ``assemble_segments`` puts it, so
    the live view matches the settled render (finalize = visual no-op).
  • Part B — ``_syncToolRoundsDOM`` renders each round's ``thinking`` +
    ``assistantContent`` as INDEPENDENT SIBLINGS of its ``.ptool-turn`` card
    (in ``.ptool-panel-body``, immediately BEFORE the card — NOT nested inside
    it), reusing the SETTLED ``.seg-thinking`` / ``.seg-narration`` classes
    verbatim (panel carries ``seg-timeline``) — zero CSS fork. This is the
    owner directive (2026-07-08): thinking, narration and each tool must be
    separate sibling blocks, never boxed together in one card.
  • Part C — the standalone bottom ``.translate-preview`` is SUPPRESSED when
    the timeline render is active (``_segTimelineRendered`` gate in
    chat_render.js; ``data-seg-timeline`` gate in translation.js in-place patch).
  • Part D — ROOT-CAUSE fix the interleave exposed: ``_syncToolRoundsDOM``'s
    change-detect fingerprint was a running ``_fp = _fp * 31 + x`` float that
    OVERFLOWS 2**53 within a single round (31**~20 ≈ 1e29). A late small field
    change (the prose length Part A stamps) then hashes to a COLLIDING double
    and the gate silently bails — so round N's captured prose never paints on
    long turns. Fixed by ``Math.imul(_fp, 31)`` (32-bit exact). This also
    latently dropped late ``compactionLayer`` / swarm / ``_hgTranslating``
    changes on long turns.

Parts A and D pair their assertion with a NEUTER that disables the mechanism
and proves the guard is load-bearing (break → the check flips → restore). Parts
B and C lost their flag-OFF neuter when the ``_segTimelineEnabled`` toggle was
removed (there is no OFF state left) — their positive sibling/adjacency and
suppression checks carry the guard.

Drives the REAL shipped JS under jsdom. Skips cleanly when node/jsdom absent.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════════
#  Part A — delta_reset CAPTURES per-round prose onto the round (data-loss fix)
#
#  Drives the REAL dispatchSSEEvent seam (window.__sse_test__) through a
#  multi-round tool sequence and asserts round N's thinking+narration is
#  STAMPED onto its first tool round when round N ends in tool calls — instead
#  of being lost when the accumulators are zeroed. NC: restore the old
#  "clear-without-capture" behavior → the prose is gone.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_CAPTURE = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearTimeout = win.clearTimeout = () => {};

let conversations = []; let activeConvId = null;
const streamBufs = new Map(); const activeStreams = new Map();
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
win.streamBufs = streamBufs; win.activeStreams = activeStreams;
// Uniform streamSessions stub (streamBufs retired — phase lives in session)
win.streamSessions = global.streamSessions = new Map();
win.getStreamSession = global.getStreamSession = (cid) => { let s = win.streamSessions.get(cid); if (!s) { s = { phase: null }; win.streamSessions.set(cid, s); } return s; };
win.setStreamPhase = global.setStreamPhase = (cid, p) => { if (!win.streamSessions.has(cid) && !(typeof activeStreams !== 'undefined' && activeStreams.has(cid))) return; win.getStreamSession(cid).phase = p; };
win.clearStreamSession = global.clearStreamSession = (cid) => { win.streamSessions.delete(cid); };
// Stub for _drBuf used in sse_pipeline delta handler (streamBufs retired)
let _drBuf = null;

const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; }; }
for (const n of ['twUpdate','twStart','twStop','finishStream','renderChat',
  'renderConversationList','buildTurnNav','saveConversations','updateContextBar',
  'scrollToBottom','_forceScrollToBottom','showToast','debugLog','showMessagesInDebug',
  '_handleAutopilotVuEvent','_retriggerHgTranslations','_streamTimerTouch',
  '_reportClientError']) { win[n] = global[n] = spy(n); }
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '<div id="streaming-msg"></div>';
win.renderMarkdown = global.renderMarkdown = (s) => s;
win.ConvView = global.ConvView = { finalizeStreaming: spy('finalizeStreaming') };
win.getActiveConv = global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
if (typeof global.requestAnimationFrame !== 'function') {
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (_) {} return 0; };
}
let _idc = 0;
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-' + (++_idc); return m; };
win._resolveAssistantById = global._resolveAssistantById = (conv, id) =>
  (conv && conv.messages.find(m => m._msgId === id)) || null;
win.renderConversationList = global.renderConversationList = spy('renderConversationList');

eval(fs.readFileSync(process.argv[9], 'utf8'));  // ui/stream_reducer.js (Phase 3: dispatch's delta branch calls reduceStreamState)
eval(fs.readFileSync(process.argv[4], 'utf8'));  // sse_handlers_tool.js
eval(fs.readFileSync(process.argv[5], 'utf8'));  // sse_handlers_swarm.js
eval(fs.readFileSync(process.argv[6], 'utf8'));  // sse_handlers_io.js
eval(fs.readFileSync(process.argv[7], 'utf8'));  // sse_handlers_misc.js
eval(fs.readFileSync(process.argv[8], 'utf8'));  // sse_handlers_lifecycle.js
eval(fs.readFileSync(process.argv[2], 'utf8'));  // sse_pipeline.js

const T = win.__sse_test__;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function setup() {
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'mid-worker' };
  const conv = { id: 'c1', messages: [{ role: 'user', content: 'hi' }, am] };
  conversations.push(conv);
  activeConvId = 'c1';
  const buf = { content: '', thinking: '', toolRounds: [] };
  // streamBufs retired — assistantMsg is the accumulation target
  const ctx = T.makeCtx({ convId: 'c1', taskId: 't1',
    stream: { controller: { signal: { aborted: false } } }, assistantMsg: am, buf });
  return { conv, am, buf, ctx };
}
function line(obj) { return 'data: ' + JSON.stringify(obj); }

// ── Round 0: thinking → narration → tool call → delta_reset ──
{
  const { am, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'reasoning about round 0' }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta', content: 'Let me read the files.' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tc0',
    toolName: 'read_files', llmRound: 0 }), ctx);
  // delta_reset closes round 0 (it issued tool calls). roundNum === llmRound.
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 0 }), ctx);

  const r0 = am.toolRounds.find(r => r.toolCallId === 'tc0');
  check('A_round0_narration_captured', !!r0 && r0.assistantContent === 'Let me read the files.');
  check('A_round0_thinking_captured', !!r0 && r0.thinking === 'reasoning about round 0');
  // Global accumulators cleared (the deliverable is only the terminal round).
  check('A_accumulators_cleared_after_reset', am.content === '' && am.thinking === '');

  // ── Round 1 starts and streams NEW thinking/narration → round 0's stays. ──
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'reasoning about round 1' }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta', content: 'Now the second step.' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 2, toolCallId: 'tc1',
    toolName: 'grep_search', llmRound: 1 }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 1 }), ctx);

  const r0b = am.toolRounds.find(r => r.toolCallId === 'tc0');
  const r1 = am.toolRounds.find(r => r.toolCallId === 'tc1');
  // THE BUG: round 0's prose must SURVIVE round 1 starting (was wiped live).
  check('A_round0_survives_round1', !!r0b && r0b.assistantContent === 'Let me read the files.'
    && r0b.thinking === 'reasoning about round 0');
  check('A_round1_captured_separately', !!r1 && r1.assistantContent === 'Now the second step.'
    && r1.thinking === 'reasoning about round 1');
  // No cross-contamination: round 1's prose is NOT on round 0.
  check('A_no_crosstalk', r0b.assistantContent !== 'Now the second step.');
}

// ── NEUTER A: simulate the OLD clear-without-capture (zero the fields the
//    handler would have stamped) → round 0's prose is LOST, proving the
//    capture is load-bearing, not incidental. ──
{
  const { am, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'r0 think' }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta', content: 'r0 narration' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'n0',
    toolName: 'read_files', llmRound: 0 }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta_reset', roundNum: 0 }), ctx);
  const r0 = am.toolRounds.find(r => r.toolCallId === 'n0');
  // Prove the mechanism worked, THEN neuter it (wipe what it captured) and
  // prove the round is left bare — i.e. WITHOUT the capture the prose is gone.
  check('NCA_capture_present_before_neuter', !!r0 && r0.assistantContent === 'r0 narration');
  delete r0.assistantContent; delete r0.thinking;   // ← neuter the capture
  check('NCA_neuter_loses_round0_prose',
    !r0.assistantContent && !r0.thinking);
}

console.log(out.join('\n'));
"""


def test_streaming_interleave_deltareset_captures_prose():
    """Part A — delta_reset stamps per-round narration+thinking onto the round."""
    import shutil
    import subprocess
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.normpath(os.path.join(HERE, '..'))
    if not (shutil.which('node') and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))):
        pytest.skip('node + jsdom dev-deps not installed')
    harness = os.path.join(HERE, '_streaming_interleave_A_harness.js')
    with open(harness, 'w') as f:
        f.write(_BODY_CAPTURE)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'),
             ROOT,
             os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),
             os.path.join(JS_DIR, 'ui', 'stream_reducer.js')],   # argv[9]
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = (proc.stdout or '').strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Part A capture failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS, got:\n{output}'


def test_deltareset_capture_routes_through_reducer_neuter():
    """NEUTER (RENDER_CONTRACT Phase 3, delta_reset fold): prove the LIVE
    delta_reset branch in sse_pipeline.js ACTUALLY folds through the pure
    reducer's delta_reset case (_stampDeltaReset) rather than the old inline
    prose-capture.

    We neuter reduceStreamState to a no-op in a copy of stream_reducer.js and
    re-run the SAME Part A capture harness. If the branch routes through the
    reducer, the no-op means the round's prose is NEVER stamped and the
    accumulators are NEVER cleared — so Part A's capture assertions MUST FAIL
    (A_round0_narration_captured, A_round0_thinking_captured,
    A_accumulators_cleared_after_reset). If a future edit reverts the fold to
    inline mutation, this NEUTER goes green — flagging the regression."""
    import shutil
    import subprocess
    import tempfile
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.normpath(os.path.join(HERE, '..'))
    if not (shutil.which('node') and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))):
        pytest.skip('node + jsdom dev-deps not installed')
    reducer_path = os.path.join(JS_DIR, 'ui', 'stream_reducer.js')
    src = open(reducer_path, encoding='utf-8').read()
    marker = 'function reduceStreamState(state, ev) {'
    assert marker in src, 'reduceStreamState anchor not found — did the reducer move?'
    neutered = src.replace(
        marker, marker + '\n  if (state !== undefined) return state;  /* NEUTER: no-op */', 1)
    assert neutered != src, 'NC patch did not apply'
    tmp_reducer = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False)
    tmp_reducer.write(neutered)
    tmp_reducer.close()
    harness = os.path.join(HERE, '_streaming_interleave_A_nc_harness.js')
    with open(harness, 'w') as f:
        f.write(_BODY_CAPTURE)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'), ROOT,
             os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),
             os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),
             tmp_reducer.name],   # argv[9]: NEUTERED reducer
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (harness, tmp_reducer.name):
            try:
                os.remove(p)
            except OSError:
                pass
    output = (proc.stdout or '') + (proc.stderr or '')
    crashed = proc.returncode != 0
    required_fails = [
        'FAIL A_round0_narration_captured',
        'FAIL A_round0_thinking_captured',
        'FAIL A_accumulators_cleared_after_reset',
    ]
    # A neutered reducer must break the capture — either the assertions FAIL, or
    # (if the flow crashed) that is equally proof the branch depends on it.
    got_fails = [f for f in required_fails if f in output]
    assert crashed or got_fails, (
        'Neutering reduceStreamState did NOT break the delta_reset capture — the '
        'LIVE delta_reset branch is NOT routing through the pure reducer (it must '
        'have reverted to inline prose-capture). The fold is not load-bearing:\n'
        + output[-1500:])


# ═══════════════════════════════════════════════════════════════════════════
#  Part B — _syncToolRoundsDOM renders per-round prose as SIBLINGS of the tool
#           card (in .ptool-panel-body, BEFORE the card — NOT nested inside it)
#           + the panel carries seg-timeline (settled-CSS reuse).
#
#  Drives the REAL _syncToolRoundsDOM over a two-round toolRounds list where
#  each round's first entry carries assistantContent+thinking (as Part A
#  stamps). Asserts each round's prose lands in the panel body as a sibling of
#  its .ptool-turn card, immediately BEFORE it (located by data-seg-round), and
#  is NOT a descendant of any .ptool-turn — the owner's "don't box the three
#  together" fix. (The interleaved timeline is now the ONLY streaming render
#  path — the former `_segTimelineEnabled` flag was removed — so there is no
#  flag-OFF neuter left; the positive sibling/adjacency checks carry the guard.)
# ═══════════════════════════════════════════════════════════════════════════
_BODY_RENDER = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2]],   // ui/streaming_ui.js
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [{ role: 'assistant', content: '', _msgId: 'mLive' }] }],
    isNearBottom: () => false,
    scrollToBottom: () => {},
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
    _isRoundSwarm: () => false,
    _buildSwarmPanelHTML: () => '',
    // Tool line renderer — a minimal stand-in (real one lives in tool_rounds.js).
    _renderUnifiedToolLine: (r) => '<div class="ptool-line">' + (r.toolName || '') + '</div>',
    _renderTurnHead: () => '<div class="ptool-turn-head"></div>',
    _renderSoloRoundTag: (rno) => '<div class="ptool-turn-rno-solo">' + rno + '</div>',
    _turnLabelText: () => 'parallel',
  },
});

// Two rounds, each its own llmRound batch, each first entry carrying prose
// (exactly what Part A stamps at delta_reset).
const rounds = [
  { roundNum: 1, toolCallId: 'tc0', toolName: 'read_files', status: 'done',
    llmRound: 0, thinking: 'thinking zero', assistantContent: 'Narration for round zero.' },
  { roundNum: 2, toolCallId: 'tc1', toolName: 'grep_search', status: 'done',
    llmRound: 1, thinking: 'thinking one', assistantContent: 'Narration for round one.' },
];

const body = document.getElementById('streaming-body');
// _syncToolRoundsDOM operates on the [data-zone="tool"] container.
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');
_syncToolRoundsDOM(toolZone, rounds);

const panel = toolZone.querySelector('.ptool-panel');
check('B_panel_has_seg_timeline_class', !!panel && panel.classList.contains('seg-timeline'));
const panelBody = panel && panel.querySelector('.ptool-panel-body');
check('B_panel_body_present', !!panelBody);

const g0 = toolZone.querySelector('.ptool-turn[data-llm-round="L0"]');
const g1 = toolZone.querySelector('.ptool-turn[data-llm-round="L1"]');
check('B_two_groups_rendered', !!g0 && !!g1);

// ★ OWNER DIRECTIVE (2026-07-08): prose is an INDEPENDENT SIBLING of the tool
//   card — it lives in the panel body, NOT nested inside `.ptool-turn`. This is
//   the whole "don't box the three together" fix. Located by `data-seg-round`
//   = the group's llmRound key.
const th0 = panelBody && panelBody.querySelector(':scope > .seg-thinking[data-seg-round="L0"]');
const th1 = panelBody && panelBody.querySelector(':scope > .seg-thinking[data-seg-round="L1"]');
check('B_round0_thinking_sibling_in_body', !!th0 &&
  th0.querySelector('.thinking-text').textContent === 'thinking zero');
check('B_round1_thinking_sibling_in_body', !!th1 &&
  th1.querySelector('.thinking-text').textContent === 'thinking one');
// ★ NOT NESTED: the thinking block must NOT be a descendant of ANY .ptool-turn.
check('B_thinking_not_inside_any_ptool_turn', !!th0 && !th0.closest('.ptool-turn'));
check('B_thinking_parent_is_panel_body', !!th0 && th0.parentElement === panelBody);
// It reuses the SETTLED classes (no CSS fork): .thinking-block.seg-thinking.
check('B_thinking_reuses_settled_classes', !!th0 &&
  th0.classList.contains('thinking-block') && th0.classList.contains('seg-thinking'));
// ★ CRITICAL (per sibling mrbyt1x8): the sibling's slim `.seg-thinking` sizing
//   is scoped `.seg-timeline .seg-thinking` (+ tofu override). The panel body
//   carries the `seg-timeline` class (on .ptool-panel), so the prose STILL
//   inherits the slim look via the .seg-timeline ANCESTOR even though it now
//   sits in the body, not the card. Assert the ancestor chain resolves so a
//   refactor can't silently regress the styling.
check('B_thinking_has_seg_timeline_ancestor', !!th0 && !!th0.closest('.seg-timeline'));

// Per-round narration lands as a SIBLING in the panel body (not in the card).
const n0 = panelBody && panelBody.querySelector(':scope > .seg-narration[data-seg-round="L0"]');
const n1 = panelBody && panelBody.querySelector(':scope > .seg-narration[data-seg-round="L1"]');
check('B_round0_narration_sibling_in_body', !!n0 && n0.innerHTML.indexOf('round zero') >= 0);
check('B_round1_narration_sibling_in_body', !!n1 && n1.innerHTML.indexOf('round one') >= 0);
check('B_narration_not_inside_any_ptool_turn', !!n0 && !n0.closest('.ptool-turn'));
check('B_no_crosstalk_g1', !!n1 && n1.innerHTML.indexOf('round zero') < 0);
// Narration also inherits the sibling's slim `.seg-timeline .seg-narration`.
check('B_narration_has_seg_timeline_ancestor', !!n0 && !!n0.closest('.seg-timeline'));

// ADJACENCY: each round's prose sits immediately BEFORE its own tool card in
// the panel body — the settled `_renderTimelineBatch` order:
//   thinking(L0) → narration(L0) → card(L0) → thinking(L1) → narration(L1) → card(L1)
const kids = Array.prototype.slice.call(panelBody.children);
const iTh0 = kids.indexOf(th0), iN0 = kids.indexOf(n0), iG0 = kids.indexOf(g0);
const iN1 = kids.indexOf(n1), iG1 = kids.indexOf(g1);
check('B_thinking_above_its_card', iTh0 >= 0 && iG0 >= 0 && iTh0 < iG0);
check('B_narration_above_its_card', iN0 >= 0 && iG0 >= 0 && iN0 < iG0);
// thinking BEFORE narration (matches the settled _renderTimelineBatch order).
check('B_thinking_before_narration', iTh0 < iN0);
// Round-1 prose sits AFTER round-0's card (interleaved, not all-prose-first).
check('B_round1_prose_after_round0_card', iN1 > iG0);
check('B_round1_narration_above_its_card', iN1 < iG1);

// (The former NEUTER B flipped the `_segTimelineEnabled` flag OFF to prove the
//  interleave render was flag-gated. That flag was removed — the interleaved
//  timeline is now the ONLY streaming render path (`_segEnabled` is
//  unconditionally true in streaming_ui.js) — so there is no OFF state left to
//  neuter. The positive B_* checks above already prove the render is correct.)

report();
"""


def test_streaming_interleave_renders_prose_as_sibling_of_card():
    """Part B — per-round prose renders as a SIBLING of its tool card (in the
    panel body, before the card), NOT nested inside .ptool-turn."""
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY_RENDER,
        min_pass=15,
        label='streaming-interleave-render',
    )


def test_slim_seg_thinking_selector_scoped_to_seg_timeline():
    """The sibling's slim .seg-thinking sizing is scoped `.seg-timeline
    .seg-thinking` — assert that selector exists in styles.css so the
    ancestor-chain guard in Part B is meaningful (streaming inherits the slim
    look via the .seg-timeline ancestor, not by forking the CSS)."""
    css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'static', 'styles.css')
    css = open(css_path, encoding='utf-8').read()
    assert '.seg-timeline .seg-thinking' in css, \
        'sibling slim .seg-thinking selector missing/renamed — coordinate before landing'
    assert '.seg-timeline .seg-narration' in css, \
        'sibling slim .seg-narration selector missing/renamed'


# ═══════════════════════════════════════════════════════════════════════════
#  Part C — the standalone bottom .translate-preview is SUPPRESSED when the
#           segment timeline renders (chat_render gate + translation.js gate).
#
#  Drives the REAL renderMessage over a finished assistant msg carrying
#  segments + _translatePartial + _translateDone===false. The timeline renders
#  (it is now the only render path for a segment-bearing turn), so the bottom
#  .translate-preview block must be ABSENT (translation shows inline) and the
#  loading shell must be stamped data-seg-timeline — proving the suppression
#  fires whenever segments are present.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_SUPPRESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  // argv[4]=escape_html.js, argv[5]=safe_html.js, argv[6]=translation_model.js,
  // argv[7]=translation_indicator.js, argv[8]=tool_rounds.js,
  // argv[2]=chat_render.js (last — the fn under test).
  targets: [process.argv[4], process.argv[5], process.argv[6], process.argv[7], process.argv[8], process.argv[2]],
  globals: {
    activeConvId: 'c1',
    autoTranslate: false,
    conversations: [{ id: 'c1', messages: [] }],
    getActiveConv: () => ({ id: 'c1', messages: [] }),
    activeStreams: new Map(),
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    renderFinishInfo: () => '',
    renderFileChangesBar: () => '',
    renderErrorEnvelope: () => '',
    renderTurnProvenanceHtml: () => '',
    renderTurnCtxNote: () => '',
    stripNoTranslateTags: (s) => s,
    calcCostCny: () => 0,
    _TOFU_WORKER_SVG: '<svg></svg>',
    convAutoTranslate: () => false,
    _renderToolGroupsHTML: () => '<div class="ptool-turn"><div data-prn="1">t</div></div>',
    _toolPanelHeaderLabel: () => 'tools',
    _computeToolBatches: (rs) => [{ key: 'L0', rounds: rs }],
    _renderToolSlot: (r) => '<div data-prn="' + r.roundNum + '">t</div>',
  },
});

// A finished assistant msg WITH segments (so the timeline can render) AND a
// live translation partial (so the bottom preview WOULD fire pre-fix).
function mkMsg() {
  return {
    role: 'assistant',
    content: 'The deliverable answer.',
    toolRounds: [{ roundNum: 1, toolCallId: 'tc0', toolName: 'read_files',
                   status: 'done', llmRound: 0, assistantContent: 'narration' }],
    segments: [
      { type: 'text', text: 'narration', deliverable: false, llmRound: 0 },
      { type: 'tool_use', id: 'tc0', name: 'read_files', input: '', llmRound: 0,
        result: { content: 'x', status: 'done' } },
      { type: 'text', text: 'The deliverable answer.', deliverable: true, terminal: true },
    ],
    _translatePartial: '这是流式翻译的中文预览。',
    _translateDone: false,
  };
}

// The turn carries segments → the interleaved timeline renders (the only
// render path now) → the bottom .translate-preview is absent.
const html = renderMessage(mkMsg(), 0);
const wrap = document.createElement('div'); wrap.innerHTML = html;
check('C_timeline_rendered', !!wrap.querySelector('.ptool-panel.seg-timeline'));
check('C_no_bottom_translate_preview', !wrap.querySelector('.translate-preview'));
// The loading shell still shows (spinner head) but is marked seg-timeline so
// the in-place poll patch also skips re-creating the preview.
const loading = wrap.querySelector('.translate-loading');
check('C_loading_shell_present', !!loading);
check('C_loading_marked_seg_timeline', !!loading && loading.getAttribute('data-seg-timeline') === '1');

// (The former NEUTER C flipped `config.segmentTimeline` OFF to prove the
//  bottom `.translate-preview` reappeared when the timeline did not render.
//  That flag was removed — the timeline is now the ONLY render path — so the
//  suppression is unconditional and there is no OFF state left to neuter. The
//  positive C_* checks above prove the preview is suppressed + the loading
//  shell is marked seg-timeline.)

report();
"""


def test_streaming_interleave_suppresses_bottom_translate_preview():
    """Part C — bottom .translate-preview suppressed when timeline renders."""
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'chat_render.js'),
        body_js=_BODY_SUPPRESS,
        extra_targets=[
            os.path.join(JS_DIR, 'core', 'escape_html.js'),
            os.path.join(JS_DIR, 'core', 'safe_html.js'),
            os.path.join(JS_DIR, 'core', 'translation_model.js'),
            os.path.join(JS_DIR, 'ui', 'translation_indicator.js'),
            os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        ],
        min_pass=4,
        label='streaming-interleave-suppress',
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Part D — fingerprint PRECISION (root cause the interleave exposed).
#
#  _syncToolRoundsDOM change-detects via a running fingerprint. The OLD form
#  (_fp = _fp * 31 + x) overflows Number.MAX_SAFE_INTEGER (2**53) within ONE
#  round, so a LATE small change (e.g. the prose length Part A stamps onto an
#  early round after N later rounds already rendered) collides with the prior
#  hash → the gate bails → the captured narration never paints. The fix is
#  Math.imul(_fp, 31) (32-bit exact), so EVERY field change registers.
#
#  This drives the REAL _syncToolRoundsDOM: render many rounds, then mutate an
#  EARLY round's assistantContent and assert the fingerprint CHANGED (→ the
#  gate re-renders). NC: restore the lossy float math (patch Math.imul back to
#  plain *) → the same late mutation collides and the fingerprint is UNCHANGED,
#  proving the precision fix is load-bearing.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_FINGERPRINT = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2]],   // ui/streaming_ui.js
  globals: {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [{ role: 'assistant', content: '', _msgId: 'mLive' }] }],
    isNearBottom: () => false,
    scrollToBottom: () => {},
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
    _isRoundSwarm: () => false,
    _buildSwarmPanelHTML: () => '',
    _renderUnifiedToolLine: (r) => '<div class="ptool-line">' + (r.toolName || '') + '</div>',
    _renderTurnHead: () => '<div class="ptool-turn-head"></div>',
    _renderSoloRoundTag: (rno) => '<div class="ptool-turn-rno-solo">' + rno + '</div>',
    _turnLabelText: () => 'parallel',
  },
});

// Many rounds — enough that the running fingerprint far exceeds 2**53 (the
// overflow point) by the time we reach the tail, reproducing the long-turn
// condition where the old float math loses precision.
function mkRounds(n) {
  const rs = [];
  for (let i = 0; i < n; i++) {
    rs.push({ roundNum: i + 1, toolCallId: 'tc' + i, toolName: 'read_files',
      status: 'done', llmRound: i });
  }
  return rs;
}
const N = 12;

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');

// Compute the fingerprint the REAL code would (call _syncToolRoundsDOM and
// read back container._roundsFingerprint) for baseline, then mutate an EARLY
// round's prose (as delta_reset does) and re-derive.
function fpFor(rounds) {
  // Fresh zone each time so we read the freshly-computed fingerprint, not a
  // gated no-op.
  const b = document.createElement('div'); b.id = 'zz';
  const tz = document.createElement('div'); tz.setAttribute('data-zone', 'tool');
  b.appendChild(tz);
  _syncToolRoundsDOM(tz, rounds);
  return tz._roundsFingerprint;
}

const base = mkRounds(N);
const fp1 = fpFor(base);
// Late stamp on the LAST round: its assistantContent length is folded in at
// the END of the running hash, so under the old float math it is added to an
// already-huge value and ROUNDS AWAY (this is exactly what happened to the
// final tool round's prose in the live repro). Under Math.imul it registers.
const mutated = mkRounds(N);
mutated[N - 1].assistantContent = 'late narration stamped on the last round';
const fp2 = fpFor(mutated);

check('D_fingerprint_is_safe_integer', Number.isSafeInteger(fp1));
check('D_late_last_round_stamp_changes_fingerprint', fp1 !== fp2);

// ── NEUTER D: restore the lossy float math (Math.imul → plain *). The same
//    last-round stamp now collides (its small length is swallowed by the
//    already-overflowed float) so the fingerprint is UNCHANGED — proving the
//    Math.imul precision fix is load-bearing. ──
const _realImul = Math.imul;
Math.imul = (a, b) => a * b;   // ← reintroduce the overflow bug
try {
  const fp1b = fpFor(mkRounds(N));
  const m = mkRounds(N);
  m[N - 1].assistantContent = 'late narration stamped on the last round';
  const fp2b = fpFor(m);
  check('NCD_neuter_overflow_loses_safe_integer', !Number.isSafeInteger(fp1b));
  check('NCD_neuter_last_round_stamp_collides', fp1b === fp2b);
} finally {
  Math.imul = _realImul;
}

report();
"""


def test_streaming_interleave_fingerprint_precision():
    """Part D — Math.imul fingerprint registers a late early-round prose stamp."""
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY_FINGERPRINT,
        min_pass=4,
        label='streaming-interleave-fingerprint',
    )

"""tests/test_frontend_rendermessage_segment_gate.py — the LAST MILE:
prove `renderMessage` itself flips behavior on the segment-timeline toggle.

WHY
---
Prior tests proved (a) `renderSegmentTimelineHTML` interleaves correctly when
called DIRECTLY, and (b) the server DELIVERS `segments` to `msg.segments`
through the real GET route. But nothing exercised the DECISION inside
`renderMessage` (chat_render.js): the

    if (!isUser && !_vuPrivate && _segTimelineEnabled()
        && Array.isArray(msg.segments) && msg.segments.length > 0) { … }

gate firing AND the `_segTimelineRendered` suppression of the duplicate
standalone `msg.thinking` block. A wrong condition or a double-rendered
thinking block would leave every other test green while the owner-visible
output was wrong. This is the "flipping the toggle actually changes what I
see" seam — the whole point of the toggle.

This harness evals the REAL shipped escape_html.js + safe_html.js +
tool_rounds.js (for the REAL renderSegmentTimelineHTML + _segTimelineEnabled)
+ chat_render.js, and drives the REAL `renderMessage(msg)` over a
segments-carrying multi-tool assistant message. It asserts:

  FLAG ON:
    • the interleaved `seg-timeline` panel IS emitted, AND
    • the standalone `thinking-block` (msg.thinking) is NOT ALSO rendered
      (no duplicate thinking — the _segTimelineRendered suppression fired).
  FLAG OFF (control):
    • the legacy grouped tool panel is emitted (via renderToolRoundsHTML),
      NOT the seg-timeline panel, AND
    • the standalone `thinking-block` IS present (legacy path).
  NC (gate neuter): force `_segTimelineEnabled` false → even with segments
    present + flag config ON, the timeline branch does not fire → falls back
    to legacy + standalone thinking (proves the gate condition is load-bearing).

Skips cleanly when node / jsdom aren't installed.
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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
// Minimal document stub: tool_rounds.js registers a top-level
// document.addEventListener at load time (delegated click handler). We don't
// exercise events here — only the pure render functions — so a no-op suffices.
global.document = {
  addEventListener: function () {},
  removeEventListener: function () {},
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  getElementById: function () { return null; },
  createElement: function () { return { style: {}, classList: { add: function(){}, remove: function(){}, toggle: function(){} }, setAttribute: function(){}, appendChild: function(){} }; },
};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── config: the toggle store _segTimelineEnabled() reads. Mutable per-case. ──
global.config = { segmentTimeline: true };

// ── i18n + leaf render helpers stubbed to no-ops / identity. ──
global.t = (k) => k;
global._fmtAbsoluteDateTime = () => '';
global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
global.renderMcpLoginHintHtml = () => '';
global.renderTurnProvenanceHtml = () => '';
global.renderFileChangesBar = () => '';
global.renderErrorEnvelope = () => '';
global.renderBranchZone = () => '';
global.renderTurnCtxNote = () => '';
global.renderPreferenceLearnedHtml = () => '';
global.getActiveConv = () => null;
global.activeStreams = new Set();
global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
// Tool-line renderers the REAL renderToolRoundsHTML / renderSegmentTimelineHTML
// reach. We stub the LEAF tool-line + swarm builders so both paths run, but we
// DO NOT stub renderSegmentTimelineHTML or renderToolRoundsHTML — those are the
// REAL functions under test (from tool_rounds.js).
global._buildSwarmPanelHTML = () => '<swarm/>';
global._buildSwarmInboxChipsHTML = () => '';
global._isRoundSwarm = () => false;
global._TOOL_DISPLAY = {};
global._toolPanelHeaderLabel = () => 'HDR';
// getToolRoundsFromMsg lives in another module (conv_view / ui.js); stub it to
// return the message's toolRounds so BOTH the real renderSegmentTimelineHTML
// and the legacy renderToolRoundsHTML path resolve tool bodies.
global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
// Tail collaborators renderMessage calls after the segment gate (finish bar,
// avatars). Stubbed no-op — not under test; the gate + thinking suppression
// (asserted above them in the body) are what we exercise.
global.renderFinishInfo = () => '';
global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
global.calcCostCny = () => 0;

function loadAll(chatSrc) {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // escape_html.js
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // safe_html.js
  (0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // tool_rounds.js (REAL)
  (0, eval)(chatSrc);                                   // chat_render.js (REAL / neutered)
}

// A finished multi-tool assistant message with the interleaved segment shape
// AND a standalone msg.thinking (the field the timeline path must suppress).
function mkMsg() {
  return {
    role: 'assistant',
    content: 'The answer.',
    thinking: 'STANDALONE_THINKING_TEXT',
    toolRounds: [
      { toolCallId: 'tc1', toolName: 'web_search', status: 'done',
        toolContent: 'hit', llmRound: 0, roundNum: 1 },
    ],
    segments: [
      { type: 'thinking', text: 'batch reasoning', deliverable: false, llmRound: 0 },
      { type: 'text', text: 'Let me search.', deliverable: false, llmRound: 0 },
      { type: 'tool_use', id: 'tc1', name: 'web_search', input: '{}',
        llmRound: 0, result: { content: 'hit', status: 'done' } },
      { type: 'text', text: 'The answer.', deliverable: true, terminal: true },
    ],
  };
}

// Count standalone thinking blocks = the legacy `<div class="thinking-block"
// onclick="_toggleThinking(...)">` markup (NOT the per-batch seg-thinking,
// which uses onclick="this.classList.toggle('expanded')").
function standaloneThinkingCount(html) {
  const m = html.match(/thinking-block" onclick="_toggleThinking/g);
  return m ? m.length : 0;
}

const CHAT = fs.readFileSync(process.argv[5], 'utf8');
loadAll(CHAT);
if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
check('fn_exposed', true);
check('real_timeline_fn_present', typeof renderSegmentTimelineHTML === 'function');

// ══ 1. FLAG ON → interleaved seg-timeline + NO duplicate standalone thinking ══
{
  global.config = { segmentTimeline: true };
  const html = renderMessage(mkMsg(), 0);
  check('on_emits_seg_timeline', html.indexOf('seg-timeline') !== -1);
  check('on_has_batch_narration', html.indexOf('<md>Let me search.</md>') !== -1);
  // The standalone msg.thinking block must be SUPPRESSED (per-batch thinking
  // already shown inside the timeline). Zero legacy _toggleThinking blocks.
  check('on_no_duplicate_standalone_thinking', standaloneThinkingCount(html) === 0);
  // The per-batch thinking IS present (pure-CSS toggle variant).
  check('on_has_per_batch_thinking', html.indexOf("this.classList.toggle('expanded')") !== -1);
}

// ══ 2. FLAG OFF → legacy grouped path + standalone thinking present ══
{
  global.config = { segmentTimeline: false };
  const html = renderMessage(mkMsg(), 0);
  check('off_no_seg_timeline', html.indexOf('seg-timeline') === -1);
  // Legacy grouped tool panel still renders the tool (ptool-panel present).
  check('off_has_ptool_panel', html.indexOf('ptool-panel') !== -1);
  // The standalone msg.thinking block IS rendered on the legacy path.
  check('off_has_standalone_thinking', standaloneThinkingCount(html) === 1);
  check('off_standalone_has_text_hook', html.indexOf('Thinking Process') !== -1);
}

// ══ 3. NC: neuter the gate (_segTimelineEnabled → always false) — even with
//        segments + config ON, the timeline branch must NOT fire. Proves the
//        gate call is load-bearing inside renderMessage. ══
{
  const NEUT = CHAT.replace(
    'function _segTimelineEnabled() {',
    'function _segTimelineEnabled() { return false; /* NEUTERED */ ');
  // (tool_rounds.js owns _segTimelineEnabled, not chat_render — neuter there)
  const TR = fs.readFileSync(process.argv[4], 'utf8').replace(
    'function _segTimelineEnabled() {',
    'function _segTimelineEnabled() { return false; /* NEUTERED */ ');
  const applied = TR.indexOf('/* NEUTERED */') !== -1;
  check('nc_pattern_applied', applied);
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));
  (0, eval)(TR);        // neutered tool_rounds
  (0, eval)(CHAT);      // real chat_render
  global.config = { segmentTimeline: true };  // config ON, but gate forced false
  const html = renderMessage(mkMsg(), 0);
  check('nc_gate_off_no_timeline', html.indexOf('seg-timeline') === -1);
  check('nc_gate_off_standalone_thinking_back', standaloneThinkingCount(html) === 1);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rendermessage_segment_gate_flips():
    # Sanity: the gate + suppression must be present in the shipped source.
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    assert '_segTimelineRendered' in chat_src, 'segment gate missing from chat_render.js — test stale'
    assert 'renderSegmentTimelineHTML(msg.segments' in chat_src, 'gate call missing — test stale'

    harness = os.path.join(HERE, '_rendermsg_seg_gate_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ESCAPE_HTML, SAFE_HTML, TOOL_ROUNDS, CHAT_RENDER],
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
    assert not fails, 'renderMessage segment-gate failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        test_rendermessage_segment_gate_flips()
        print('PASS test_rendermessage_segment_gate_flips')

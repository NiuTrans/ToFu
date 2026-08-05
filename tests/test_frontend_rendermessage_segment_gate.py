"""tests/test_frontend_rendermessage_segment_gate.py — the LAST MILE:
prove `renderMessage` itself interleaves whenever a turn carries `segments`.

WHY
---
Prior tests proved (a) `renderSegmentTimelineHTML` interleaves correctly when
called DIRECTLY, and (b) the server DELIVERS `segments` to `msg.segments`
through the real GET route. But nothing exercised the DECISION inside
`renderMessage` (chat_render.js): the

    if (_segTimelineAllowed
        && Array.isArray(msg.segments) && msg.segments.length > 0) { … }

branch firing AND the `_segTimelineRendered` suppression of the duplicate
standalone `msg.thinking` block. A wrong condition or a double-rendered
thinking block would leave every other test green while the owner-visible
output was wrong.

The interleaved timeline is now the ONLY user-facing render path (the former
`_segTimelineEnabled` toggle was removed) — so the decision reduces to "does
this turn carry segments?". This harness evals the REAL shipped escape_html.js
+ safe_html.js + tool_rounds.js (for the REAL renderSegmentTimelineHTML) +
chat_render.js, and drives the REAL `renderMessage(msg)`. It asserts:

  SEGMENTS PRESENT → the interleaved timeline is the render:
    • the interleaved `seg-timeline` panel IS emitted (with the per-batch
      thinking inline), AND
    • the standalone `thinking-block` (msg.thinking) is rendered EXACTLY ONCE.
      msg.thinking is the TERMINAL round's reasoning; the timeline
      DELIBERATELY skips every terminal segment (tool_rounds.js:
      `if (s.terminal) continue`), so the standalone block is its ONLY render
      path. Zero occurrences = the 752927bd drop regression ("reasoning_content
      missing"); two+ = a real duplicate. The block hydrates its text LAZILY
      on toggle (`.thinking-text` is emitted empty), so its identity is
      pinned via the label's char-meta (= msg.thinking.length) and the
      per-batch string via a single inline occurrence — the precise modern
      form of this suite's original "no duplicate thinking" intent.
  SEGMENTS ABSENT (control / the sole remaining fallback):
    • the legacy grouped tool panel is emitted (via renderToolRoundsHTML),
      NOT the seg-timeline panel, AND
    • the standalone `thinking-block` IS present (legacy path).
      This proves `msg.segments` presence is what drives the timeline — the
      grouped renderer survives only as the automatic segment-less fallback.

HISTORY (test-drift lesson): the pre-752927bd version of this suite asserted
the standalone block count was ZERO when the timeline rendered — the old
`!_segTimelineRendered` suppression contract. 752927bd removed that gate as a
root fix (terminal reasoning was silently dropped on every multi-tool turn)
and left this suite red. The suppression contract's guard is
test_frontend_terminal_thinking_render.py (NC: re-adding the gate drops the
block); this suite now pins the same contract from the renderMessage side.

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
  (0, eval)(fs.readFileSync(process.argv[2].replace('escape_html.js', 'translation_model.js'), 'utf8'));  // core/translation_model.js (chat_render dep)
  (0, eval)(fs.readFileSync(process.argv[2].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));  // ui/translation_indicator.js (chat_render dep)
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

// ══ 1. SEGMENTS PRESENT → interleaved seg-timeline + terminal thinking ONCE ══
{
  const html = renderMessage(mkMsg(), 0);
  check('on_emits_seg_timeline', html.indexOf('seg-timeline') !== -1);
  check('on_has_batch_narration', html.indexOf('<md>Let me search.</md>') !== -1);
  // 752927bd contract: msg.thinking is the TERMINAL reasoning — the timeline
  // deliberately skips terminal segments, so the standalone block is its ONLY
  // render path. EXACTLY ONCE: zero = dropped (the 752927bd "reasoning_content
  // missing" regression), two+ = a real duplicate. (The pre-752927bd version
  // of this check demanded ZERO — the suppression contract that root-fix
  // removed; see the module docstring.)
  check('on_terminal_thinking_exactly_once', standaloneThinkingCount(html) === 1);
  // The block's identity: the label's char-meta is msg.thinking.length, so the
  // block is provably the TERMINAL field's lazy surface (its text hydrates on
  // toggle — chat_render.js emits `.thinking-text` EMPTY by design — never
  // inline; test_frontend_terminal_thinking_render.py pins the same hook).
  check('on_terminal_block_is_msg_thinking',
        html.indexOf('stream.thinking.done (24 chars)') !== -1);
  // The per-batch thinking IS present (pure-CSS toggle variant)…
  check('on_has_per_batch_thinking', html.indexOf("this.classList.toggle('expanded')") !== -1);
  // …and it is NOT duplicated: the per-batch reasoning lives only inside the
  // timeline, exactly once. "No duplicate thinking" in its precise modern
  // form (the terminal string has no inline occurrence to count — see above).
  check('on_per_batch_text_once', html.split('batch reasoning').length - 1 === 1);
}

// ══ 2. SEGMENTS ABSENT (the sole remaining fallback) → legacy grouped path +
//        standalone thinking present. Proves `msg.segments` presence is what
//        drives the timeline; the grouped renderer survives only as the
//        automatic segment-less fallback. ══
{
  const noSeg = mkMsg();
  delete noSeg.segments;
  const html = renderMessage(noSeg, 0);
  check('off_no_seg_timeline', html.indexOf('seg-timeline') === -1);
  // Legacy grouped tool panel still renders the tool (ptool-panel present).
  check('off_has_ptool_panel', html.indexOf('ptool-panel') !== -1);
  // The standalone msg.thinking block IS rendered on the legacy path.
  check('off_has_standalone_thinking', standaloneThinkingCount(html) === 1);
  check('off_standalone_has_text_hook', html.indexOf('stream.thinking.done') !== -1);
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
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        test_rendermessage_segment_gate_flips()
        print('PASS test_rendermessage_segment_gate_flips')

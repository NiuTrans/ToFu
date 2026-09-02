#!/usr/bin/env python3
"""Grouped-panel per-round narration render-in-place (translatedText fix).

The interleaved SEGMENT TIMELINE (renderSegmentTimelineHTML) already renders
each round's narration inline. But when the segment-timeline toggle is OFF, or
the timeline path falls back to the legacy GROUPED panel (renderToolRoundsHTML
→ _renderUnifiedGroup), the grouped panel historically rendered NO per-round
narration at all — so a translated turn's per-round Chinese had nowhere to go
in the settled view and clumped into the whole-message bottom block (the
reported "everything clumps at the tail after finalize" bug).

The fix threads the message's `segments` into the grouped panel: each round's
non-deliverable narration is rendered as a `.md-content.seg-narration` block
IMMEDIATELY BEFORE that round's `.ptool-turn` — byte-identical to the settled
timeline (_renderTimelineBatch) and the streaming preview (.stream-seg-narration
in translation.js). When a narration segment carries `translatedText` it renders
the Chinese (in place); else the English.

This test extracts the REAL grouped-panel functions from tool_rounds.js and
evals them in node with the tool renderer + markdown stubbed to ORDER MARKERS,
asserting:
  1. Each round's narration renders BEFORE its .ptool-turn (in place).
  2. A narration segment with translatedText shows the Chinese, NOT the English.
  3. A narration segment without translatedText falls back to English.
  4. No `segments` arg → byte-identical to the pre-fix grouped render (no
     narration injected) — the branch/paper-reader/upload callers are safe.
  5. NC: neuter the narration prepend in _renderToolGroupsHTML → the per-round
     narration disappears from the grouped panel → the "renders in place"
     assertions fail, proving the prepend is load-bearing (this reproduces the
     tail-clump bug for the grouped path).

Skips cleanly when node isn't installed.
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
TR_JS = runtime_section_path('ui/tool_rounds.js')


def _extract_grouped_fns() -> str:
    """Extract the contiguous grouped-panel helper block from tool_rounds.js:
    from `function _computeToolBatches(` through the end of
    `_renderToolGroupsHTML` (just before `function renderMcpLoginHintHtml`-style
    unrelated code). We stub _renderToolSlot, so we cut right after
    _renderToolGroupsHTML."""
    src = open(TR_JS, encoding='utf-8').read()
    start = src.index('function _computeToolBatches(')
    # End at the start of _renderUnifiedGroup (block above it is the group
    # renderer + narration helpers we need).
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = src[start:end]
    for needed in ('_narrationByRound', '_renderSegNarrationHTML',
                   '_renderToolGroupsHTML', '_computeToolBatches'):
        assert needed in chunk, f'extraction missed {needed}'
    return chunk


_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
function t(k){ return k; }
function stripNoTranslateTags(text){ return text; }
// The extracted block contains the REAL _renderToolSlot, which delegates to
// these collaborators. Stub them: non-swarm rounds emit an ORDER MARKER so we
// can assert the narration sits BEFORE the round's tool card. The real
// _renderToolGroupsHTML still wraps each batch in the `.ptool-turn` div
// (carrying data-llm-round — the key the narration is matched against).
function _isRoundSwarm(r){ return false; }
function _buildSwarmPanelHTML(r, allRounds){ return '<SWARM>'; }
function _renderUnifiedToolLine(r, searching){ return '<TOOL name=' + (r.toolName||'') + '>'; }
"""

_HARNESS = _STUBS + r"""
const src = process.env.TR_SRC;
eval(src);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Two batches: round 0 (narration WITH translatedText + 1 tool), round 1
// (narration WITHOUT translatedText + 1 tool).
const rounds = [
  { roundNum: 1, toolName: 'grep_search', status: 'done', llmRound: 0 },
  { roundNum: 2, toolName: 'apply_diff', status: 'done', llmRound: 1 },
];
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narrate0-EN', deliverable: false, llmRound: 0, translatedText: '第零轮译文' },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'text', text: 'narrate1-EN', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', llmRound: 1 },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];

// Drive the real grouped renderer with selector-projected segments.
const narrByRound = _narrationByRound(segments);
const html = _renderToolGroupsHTML(rounds, rounds, narrByRound);

function idx(n){ return html.indexOf(n); }
const iNarr0 = idx('<md>第零轮译文</md>');
const iTurn0 = idx('data-llm-round="L0"');
const iGrep  = idx('<TOOL name=grep_search>');
const iNarr1 = idx('<md>narrate1-EN</md>');
const iTurn1 = idx('data-llm-round="L1"');
const iDiff  = idx('<TOOL name=apply_diff>');

// 1. Round-0 narration renders IN PLACE — before round-0's .ptool-turn.
check('round0_narration_present', iNarr0 >= 0);
check('round0_narration_before_its_turn', iNarr0 >= 0 && iNarr0 < iTurn0);
check('round0_narration_before_tool', iNarr0 >= 0 && iNarr0 < iGrep);

// 2. translatedText wins over English for round 0.
check('round0_shows_chinese', iNarr0 >= 0);
check('round0_english_not_shown', idx('<md>narrate0-EN</md>') === -1);

// 3. Round-1 narration (no translatedText) falls back to English, in place.
check('round1_narration_english', iNarr1 >= 0);
check('round1_narration_before_its_turn', iNarr1 >= 0 && iNarr1 < iTurn1);
check('round1_narration_after_round0_tool', iNarr1 > iGrep);

// 4. Deliverable/terminal is NOT rendered in the tool panel.
check('deliverable_excluded', idx('THE ANSWER') === -1);

// 5. No-segments call is byte-identical to the pre-fix grouped render (no
//    narration blocks injected) — protects branch/paper-reader/upload callers.
const htmlNoSeg = _renderToolGroupsHTML(rounds, rounds);
check('no_segments_no_narration', htmlNoSeg.indexOf('seg-narration') === -1);
check('no_segments_still_renders_tools',
      htmlNoSeg.indexOf('<TOOL name=grep_search>') >= 0 &&
      htmlNoSeg.indexOf('<TOOL name=apply_diff>') >= 0);

console.log(out.join('\n'));
"""


def _run_node(harness: str, src: str) -> str:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    env = dict(os.environ, TR_SRC=src)
    p = subprocess.run([node, '-e', harness], capture_output=True, text=True,
                       env=env, timeout=30)
    if p.returncode != 0:
        raise AssertionError(f'node eval failed:\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}')
    return p.stdout


def test_grouped_panel_renders_narration_in_place():
    src = _extract_grouped_fns()
    out = _run_node(_HARNESS, src)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'grouped-panel narration checks failed:\n{out}'
    # Sanity: all expected checks ran.
    assert out.count('PASS') >= 11, f'expected ≥11 PASS lines, got:\n{out}'


def test_neuter_removes_grouped_narration_prepend():
    """NC: neuter the narration prepend in _renderToolGroupsHTML (return the
    .ptool-turn WITHOUT the leading `narr`), so the grouped panel drops the
    per-round narration entirely → the in-place assertions fail. Proves the
    prepend is the load-bearing line that fixes the tail-clump for the grouped
    path (not merely present-but-inert)."""
    src = _extract_grouped_fns()
    # Turn `return narr + \`<div class="ptool-turn"...` into `return \`<div ...`
    target = ('return narr + `<div class="ptool-turn'
              '${programs.length ? " ptool-program-turn" : ""}"')
    neutered = src.replace(
        target, 'return ' + target.removeprefix('return narr + '))
    assert neutered != src, 'neuter target string not found — extraction drifted'
    out = _run_node(_HARNESS, neutered)
    # With the prepend gone, the Chinese narration disappears from the panel →
    # the "renders in place" checks must FAIL.
    assert 'FAIL round0_narration_present' in out or \
           'FAIL round0_narration_before_its_turn' in out, \
        f'NEUTER did not bite — narration still rendered:\n{out}'
    # The no-segments control assertions still pass (they never depended on the
    # prepend), confirming the neuter is surgical.
    assert 'PASS no_segments_still_renders_tools' in out, out


if __name__ == '__main__':
    print(_run_node(_HARNESS, _extract_grouped_fns()))

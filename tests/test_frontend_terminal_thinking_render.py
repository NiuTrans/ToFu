#!/usr/bin/env python3
"""tests/test_frontend_terminal_thinking_render.py — the TERMINAL thinking
(reasoning_content) of a finished assistant turn must RENDER even when the
interleaved segment timeline rendered.

WHY (recurring "content / reasoning_content missing" bug)
---------------------------------------------------------
A finished multi-tool assistant turn is rendered by the segment timeline
(renderSegmentTimelineHTML), which shows each tool's PRECEDING per-round
thinking + narration inline. The timeline DELIBERATELY skips every ``terminal``
segment — both the deliverable answer text AND the terminal thinking (the
reasoning the model produced right before its final answer, == ``msg.thinking``,
which is task['thinking'] reset each round).

The deliverable answer is rendered SEPARATELY by chat_render.js's
``else if (msg.content)`` branch (NOT gated on the timeline) — so it survives.
But the terminal thinking block used to be gated on ``!_segTimelineRendered``
under the wrong assumption that "the timeline already includes the thinking".
It includes only the PER-ROUND thinking; the terminal thinking has NO batch
after it, so it is never inline — and the standalone block was suppressed →
the terminal reasoning was SILENTLY DROPPED whenever the timeline rendered
(i.e. every normal multi-tool turn). This is the root cause the prior
``superseded``-badge fixes never touched, and the existing
test_frontend_segment_timeline.py never caught because its fixtures set
``msg.thinking`` to a PER-ROUND (llmRound:0) thinking, never a distinct
terminal one.

This test drives the REAL shipped ``renderMessage`` (chat_render.js) with a
message that carries BOTH:
  • per-round thinking segments (llmRound 0/1 — rendered inline by the timeline)
  • a DISTINCT terminal thinking string on ``msg.thinking`` (the reasoning right
    before the final answer)
and asserts the terminal thinking block IS present in the rendered bubble, and
its text is distinct from the inline per-round thinking (no duplication).

NEUTER CONTROL
  • NC-1: re-add the old ``&& !_segTimelineRendered`` gate on the terminal
    thinking block → the block disappears when the timeline rendered → the
    "terminal thinking present" assertion FAILS. Proves the fix is load-bearing.

Skips cleanly when node + jsdom aren't installed.
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
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const NC = process.argv[6] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const JS = path.join(ROOT, 'static', 'js');
function load(rel){ (0, eval)(fs.readFileSync(path.join(JS, rel), 'utf8')); }

const _conv = { id: 'c1', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c1';
win.getActiveConv = global.getActiveConv = () => _conv;
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<MD>' + String(s == null ? '' : s).replace(/\n/g, ' ') + '</MD>';
win.Icon = global.Icon = () => '';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win._USER_AVATAR_SVG = win._TOFU_WORKER_SVG = win._TOFU_PLANNER_SVG = win._TOFU_CRITIC_SVG = '';
global._USER_AVATAR_SVG = global._TOFU_WORKER_SVG = global._TOFU_PLANNER_SVG = global._TOFU_CRITIC_SVG = '';
const _noop = () => '';
for (const n of ['renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote','renderPreferenceLearnedHtml',
  'renderFinishInfo','_buildSwarmInboxChipsHTML','_injectAnchoredBranches','_prefetchConvCosts',
  '_prefetchConvFileChanges','_stampFreshness','buildTurnNav','calcCostCny','buildCompactionCardHtml']) {
  win[n] = global[n] = _noop;
}
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg =
  (m) => (m && m.toolRounds && m.toolRounds.length) ? m.toolRounds : [];

load('core/escape_html.js');
load('core/safe_html.js');
load('core/translation_model.js');
load('ui/translation_indicator.js');
load('ui/tool_rounds.js');   // REAL renderSegmentTimelineHTML + renderToolRoundsHTML

// chat_render.js — shipped or NEUTERED.
let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
if (NC === 'nc_gate') {
  // NC-1: re-introduce the old over-suppression gate on the terminal thinking
  // block. The shipped fix is `if (msg.thinking) {`; regress it back to
  // `if (msg.thinking && !_segTimelineRendered) {`.
  const before = chatSrc;
  chatSrc = chatSrc.replace(
    'if (msg.thinking) {\n    const thinkLen = msg.thinking.length;',
    'if (msg.thinking && !_segTimelineRendered) {\n    const thinkLen = msg.thinking.length;');
  if (chatSrc === before) { console.log('FAIL nc_pattern_applied'); process.exit(0); }
}
(0, eval)(chatSrc);
console.log('PASS nc_pattern_applied');

if (typeof renderMessage !== 'function') { console.log('FAIL renderMessage_exposed'); process.exit(0); }

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A finished 2-batch assistant turn:
//   batch0: per-round thinking 'PERROUND_REASON_0' + a tool
//   batch1: a tool
//   terminal: distinct thinking 'TERMINAL_REASON_XYZ' + deliverable 'THE FINAL ANSWER'
function mkMsg() {
  return {
    role: 'assistant',
    _msgId: 'a1',
    content: 'THE FINAL ANSWER',
    thinking: 'TERMINAL_REASON_XYZ',   // the terminal (last-round) reasoning
    segments: [
      { type: 'thinking', text: 'PERROUND_REASON_0', deliverable: false, llmRound: 0 },
      { type: 'text', text: 'let me look', deliverable: false, llmRound: 0 },
      { type: 'tool_use', id: 'tc0', name: 'read_files', input: '{}', llmRound: 0,
        result: { content: 'ok', status: 'done' } },
      { type: 'tool_use', id: 'tc1', name: 'grep_search', input: '{}', llmRound: 1,
        result: { content: 'ok', status: 'done' } },
      { type: 'thinking', text: 'TERMINAL_REASON_XYZ', deliverable: false, terminal: true },
      { type: 'text', text: 'THE FINAL ANSWER', deliverable: true, terminal: true },
    ],
    toolRounds: [
      { toolCallId: 'tc0', toolName: 'read_files', status: 'done', toolContent: 'ok', llmRound: 0, roundNum: 1 },
      { toolCallId: 'tc1', toolName: 'grep_search', status: 'done', toolContent: 'ok', llmRound: 1, roundNum: 2 },
    ],
  };
}

const html = renderMessage(mkMsg(), 0);
const frag = win.document.createElement('div');
frag.innerHTML = html;

// The segment timeline rendered (proves we're on the path that used to drop it).
check('timeline_rendered', html.indexOf('seg-timeline') !== -1);
// The deliverable answer renders (regression guard — was never gated, must stay).
check('deliverable_content_rendered', html.indexOf('THE FINAL ANSWER') !== -1);
// The per-round thinking rendered INLINE in the timeline (as a seg-thinking).
check('perround_thinking_inline', html.indexOf('PERROUND_REASON_0') !== -1);

// ★ THE FIX: a STANDALONE terminal thinking-block is present (not seg-thinking,
//   not inside the ptool-panel) — the block that lazy-loads msg.thinking.
const standaloneThink = Array.prototype.filter.call(
  frag.querySelectorAll('.thinking-block'),
  (el) => !el.className.includes('seg-thinking') && !el.closest('.ptool-panel'));
check('terminal_thinking_block_present', standaloneThink.length >= 1);
// The standalone thinking block uses the lazy-load hook that reads msg.thinking
// (that is HOW the terminal reasoning text reaches the DOM on toggle).
check('terminal_thinking_lazyload_hook',
      html.indexOf('thinking-block" onclick="_toggleThinking') !== -1);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_terminal_thinking_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,   # argv[2]
             '',            # argv[3] (unused)
             '',            # argv[4] (unused)
             ROOT,          # argv[5]
             nc,            # argv[6]
             ],
            capture_output=True, text=True, timeout=90,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_terminal_thinking_renders_with_timeline():
    """The terminal reasoning_content renders even when the segment timeline
    rendered (the recurring "reasoning_content missing" bug)."""
    # Sanity: the shipped guard must be the un-gated form.
    src = open(CHAT_RENDER, encoding='utf-8').read()
    assert 'if (msg.thinking) {' in src, \
        'terminal thinking block is not the un-gated form — fix reverted?'
    assert 'if (msg.thinking && !_segTimelineRendered) {' not in src, \
        'terminal thinking block still carries the over-suppression gate'

    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'terminal-thinking render failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_reintroducing_gate_drops_terminal_thinking():
    """NC-1: re-adding `&& !_segTimelineRendered` must drop the terminal
    thinking block, failing the presence assertion — proves it's load-bearing."""
    output = _run('nc_gate')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL terminal_thinking_block_present' in output, (
        'Re-introducing the timeline gate did NOT drop the terminal thinking '
        f'block — the fix is not load-bearing:\n{output}')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_terminal_thinking_renders_with_timeline()
        test_nc_reintroducing_gate_drops_terminal_thinking()
        print('PASS test_frontend_terminal_thinking_render')

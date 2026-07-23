"""tests/test_frontend_autopilot_vu_timeline.py — an autopilot VU turn renders
the IDENTICAL agent INLINE per-tool segment timeline (not the legacy grouped
tool panel).

WHY
---
Owner report: "Autopilot rendering does not use our inline tool timeline — it
should reuse the exact same rendering code as the Agent." Root cause: the
settled render gate in ``chat_render.js`` gated the interleaved segment timeline
on ``!isUser``, but a VU (autopilot) turn is ``role=user`` (``_isVirtualUser``),
so it fell through to the grouped ``renderToolRoundsHTML`` panel. Fix: widen the
gate to ``(!isUser || msg._isVirtualUser)`` AND propagate a ``segments`` list
onto the VU message (backend ``run_virtual_user`` / ``_append_vu_message_to_conv``).

This test drives the REAL shipped ``renderMessage`` (jsdom) with the timeline
flag ON and a stub ``renderSegmentTimelineHTML`` that emits a recognizable
marker, and asserts:

  (a) a VU turn WITH segments renders the INLINE timeline (marker present) and
      NOT the grouped panel;
  (b) the widened gate did not regress the ASSISTANT path (still timelines);
  (c) the widened gate did not OVER-broaden — a plain human USER turn (no
      _isVirtualUser) never renders the timeline;
  (d) the translate-loading block is stamped ``data-seg-timeline="1"`` (the
      preview-dup skip). The standalone TERMINAL thinking block is NOT
      suppressed — the timeline excludes terminal segments, so ``msg.thinking``
      (the terminal reasoning) still renders below the panel (guarded in full
      by test_frontend_terminal_thinking_render.py).

NEUTER CONTROL
  • NC-gate: revert the gate to ``!isUser`` (drop the ``|| msg._isVirtualUser``)
    in a COPY of chat_render.js → the VU turn no longer takes the timeline path
    (grouped panel present, timeline marker absent, thinking block reappears) →
    the VU timeline assertions FAIL. Proves the widening is load-bearing.

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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Idle conv so renderMessage's canDelete/live-tail guards resolve.
const _conv = { id: 'c-tl', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-tl';
win.getActiveConv = global.getActiveConv = () => _conv;

win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
// Distinct markers so we can tell WHICH render path a message took:
//   • grouped legacy panel → GROUPED-PANEL
//   • inline segment timeline → SEG-TIMELINE
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">GROUPED-PANEL</div>';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML =
  (segs) => (Array.isArray(segs) && segs.length ? '<div class="ptool-panel seg-timeline">SEG-TIMELINE</div>' : '');

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny','_apRunReportAffordance',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

const CHAT = fs.readFileSync(process.argv[2], 'utf8');
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_gate') {
  // Revert the gate widening: drop the VU carve-out so a role=user VU turn no
  // longer qualifies for the inline timeline (the pre-fix behaviour).
  chatSrc = CHAT.replace(
    'const _segTimelineAllowed = !isUser || msg._isVirtualUser;',
    'const _segTimelineAllowed = !isUser;');
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));  // core/translation_model.js (chat_render dep)
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));  // ui/translation_indicator.js (chat_render dep)
(0, eval)(chatSrc);                                   // chat_render.js (real / neutered)

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
check('fn_exposed', true);

const SEGS = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'Let me check the files.', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'tc1', name: 'read_files', input: '{}', llmRound: 0,
    result: { content: 'ok', status: 'done' } },
  { type: 'text', text: 'Keep going.', deliverable: true, terminal: true },
];
const ROUNDS = [
  { toolCallId: 'tc1', toolName: 'read_files', status: 'done',
    toolContent: 'ok', llmRound: 0, roundNum: 1 },
];

function mkVu(extra) {
  return Object.assign({
    role: 'user', _isVirtualUser: true, _msgId: 'vu1', _autopilotRunId: 'RX',
    content: 'Keep going.', thinking: 'STANDALONE_VU_THINKING',
    toolRounds: ROUNDS, segments: SEGS,
  }, extra || {});
}
function mkAsst() {
  return { role: 'assistant', _msgId: 'a1', content: 'Done.',
           thinking: 'ASST_THINK', toolRounds: ROUNDS, segments: SEGS };
}
function mkUser() {
  // A plain human user turn that (defensively) carries segments — it must
  // NEVER render the timeline (the gate must not over-broaden to all users).
  // No toolRounds: a human turn never has them, and the legacy grouped-panel
  // render (chat_render.js: `!_segTimelineRendered && rounds.length>0`) is not
  // isUser-gated, so injecting rounds here would test pre-existing behaviour,
  // not the gate.
  return { role: 'user', _msgId: 'u1', content: 'hi', segments: SEGS };
}

// ══ (a) VU turn → inline segment timeline, NOT grouped panel ══
{
  const html = renderMessage(mkVu(), 3);
  check('a_vu_timeline_rendered', html.indexOf('SEG-TIMELINE') !== -1);
  check('a_vu_no_grouped_panel', html.indexOf('GROUPED-PANEL') === -1);
  // The TERMINAL thinking (msg.thinking) is NOT suppressed: the timeline
  // excludes terminal segments, so the standalone block must still render it
  // (the recurring "reasoning_content missing" root-fix). The stubbed
  // renderSegmentTimelineHTML emits no thinking, so this block is the ONLY
  // path for the terminal reasoning here.
  check('a_vu_terminal_thinking_rendered',
        html.indexOf('thinking-block" onclick="_toggleThinking') !== -1);
  // Still a VU bubble (avatar/label unchanged — the only identity signal).
  const frag = win.document.createElement('div'); frag.innerHTML = html;
  const roleEl = frag.querySelector('.message-role');
  check('a_vu_label_autopilot', roleEl && roleEl.textContent.trim() === 'Autopilot');
}

// ══ (a2) VU + in-flight translate → translate-loading stamped data-seg-timeline ══
{
  const html = renderMessage(mkVu({ _translateDone: false, _translatePartial: 'partial zh' }), 4);
  check('a2_timeline_still_rendered', html.indexOf('SEG-TIMELINE') !== -1);
  // The preview-dup skip: with the timeline active, the standalone translate
  // preview blob is suppressed and the loading head carries the marker attr.
  check('a2_translate_seg_marker', html.indexOf('data-seg-timeline="1"') !== -1);
  check('a2_no_dup_preview', html.indexOf('translate-preview') === -1);
}

// ══ (b) assistant still timelines (no regression) ══
{
  const html = renderMessage(mkAsst(), 5);
  check('b_asst_timeline_rendered', html.indexOf('SEG-TIMELINE') !== -1);
  check('b_asst_no_grouped_panel', html.indexOf('GROUPED-PANEL') === -1);
}

// ══ (c) plain human user turn NEVER timelines (no over-broadening) ══
{
  const html = renderMessage(mkUser(), 6);
  check('c_user_no_timeline', html.indexOf('SEG-TIMELINE') === -1);
  // A human user turn doesn't render a tool panel at all in the body path.
  check('c_user_no_grouped_panel', html.indexOf('GROUPED-PANEL') === -1);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_vu_timeline_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,   # argv[2]
             ESCAPE_HTML,   # argv[3]
             SAFE_HTML,     # argv[4]
             ROOT,          # argv[5]
             nc,            # argv[6]
             ],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_vu_takes_inline_timeline():
    # The shipped gate must carry the VU carve-out.
    chat_src = open(CHAT_RENDER, encoding='utf-8').read()
    assert 'const _segTimelineAllowed = !isUser || msg._isVirtualUser;' in chat_src, \
        'segment-timeline gate lost the _isVirtualUser carve-out'
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'VU timeline render failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_gate_revert_drops_vu_timeline():
    """NC: reverting the gate to `!isUser` must break the VU timeline checks."""
    output = _run('nc_gate')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert ('FAIL a_vu_timeline_rendered' in output
            or 'FAIL a_vu_no_grouped_panel' in output), (
        'Reverting the gate did NOT fail the VU-timeline assertions — '
        f'the widening is not load-bearing:\n{output}')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_vu_takes_inline_timeline()
        test_nc_gate_revert_drops_vu_timeline()
        print('PASS test_frontend_autopilot_vu_timeline')

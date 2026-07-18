"""tests/test_frontend_failed_turn_actions_reveal.py — a FAILED or INTERRUPTED
assistant turn must reveal its bottom action bar (Continue / Retry) WITHOUT
hover, so the affordance is discoverable at the exact moment it is needed.

WHY
---
Owner directive (the driving scenario): a conversation that errored or was
interrupted. The user scrolls to the very bottom to see it failed and needs
Continue. The base `.message-actions` is a hover-reveal (`opacity:0` → `1` only
on `.message:hover`), and the bar now sits at the BOTTOM of the bubble (moved
out of the old absolute top-right slot). A coarse pointer can never fire
`:hover`, and even on desktop the user must hover-hunt at the bottom of a long
errored turn. So on precisely the turn that needs Continue, the button was
invisible.

The fix has two halves this suite pins:
  (a) renderMessage stamps `.message.turn-failed` on a non-user turn carrying
      an error envelope (`msg.error`) OR `finishReason === 'interrupted'`; a
      settled/successful turn does NOT get the class.
  (b) styles.css reveals `.message.turn-failed .message-actions{opacity:1}` so
      the bottom bar (with the Continue button on the last assistant turn) is
      visible without hover.

NEUTER CONTROLS
  • NC-1 (JS): drop the `turn-failed` stamp → a failed turn no longer carries
    the class → the "failed turn is tagged" assertion FAILS. Proves the stamp
    is load-bearing.
  • NC-2 (CSS): strip the `.message.turn-failed .message-actions{opacity:1}`
    rule → the reveal assertion FAILS. Proves the CSS reveal is load-bearing.

Skips cleanly when node + jsdom aren't installed (JS half); the CSS half is a
pure-Python string assertion and always runs.
"""

from __future__ import annotations

import os
import re
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
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ── CSS half (always runs) ────────────────────────────────────────────────

def _strip_comments(css: str) -> str:
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


def test_css_reveals_failed_turn_action_bar():
    """The base stylesheet must pin `.message.turn-failed .message-actions`
    visible (opacity:1) — that's what surfaces Continue on a failed turn."""
    css = _strip_comments(open(CSS, encoding='utf-8').read())
    m = re.search(r'\.message\.turn-failed\s+\.message-actions\{([^}]*)\}', css)
    assert m, '.message.turn-failed .message-actions rule not found in styles.css'
    body = m.group(1)
    assert 'opacity:1' in body.replace(' ', ''), (
        'failed-turn action bar is not revealed (opacity:1 missing) — '
        f'got: {body!r}')


def test_css_base_action_bar_is_bottom_hover_reveal():
    """Precondition anchor: the base `.message-actions` is a BOTTOM in-flow
    hover-reveal (opacity:0, margin-top, no absolute top/right). Guards that
    the failed-turn reveal is layered on the bottom-bar design, not the old
    absolute top-right slot."""
    css = _strip_comments(open(CSS, encoding='utf-8').read())
    m = re.search(r'(?<![.\w-])\.message-actions\{([^}]*)\}', css)
    assert m, 'base .message-actions rule not found'
    body = m.group(1).replace(' ', '')
    assert 'opacity:0' in body, f'base bar is no longer a hover-reveal: {body!r}'
    assert 'position:absolute' not in body, (
        'base bar is still absolutely positioned — bottom-bar move regressed: '
        f'{body!r}')
    assert 'margin-top:' in body, (
        'base bar lost its in-flow bottom offset (margin-top): ' f'{body!r}')


# ── JS half (jsdom) ───────────────────────────────────────────────────────

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

// idle conv so canDelete / continue (no active stream) resolve TRUE and the
// LAST assistant turn emits the Continue button.
const _conv = { id: 'c-fail', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-fail';
win.getActiveConv = global.getActiveConv = () => _conv;

win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
// renderErrorEnvelope must emit *something* for a msg.error so the body is
// non-empty (mirrors production: the error bar renders above the action bar).
win.renderErrorEnvelope = global.renderErrorEnvelope = (e) =>
  '<div class="error-envelope">ERR</div>';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderBranchZone','renderTurnCtxNote','renderPreferenceLearnedHtml',
  'renderFinishInfo','_buildSwarmInboxChipsHTML','_injectAnchoredBranches',
  '_prefetchConvCosts','_prefetchConvFileChanges','_stampFreshness',
  'buildTurnNav','calcCostCny','renderTranslateIndicator',
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
if (NC === 'nc_stamp') {
  // NC-1: drop the turn-failed stamp — force _failedCls to always be empty.
  chatSrc = CHAT.replace(
    "const _failedCls = _turnFailed ? ' turn-failed' : '';",
    "const _failedCls = '';");
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));
(0, eval)(chatSrc);

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
check('fn_exposed', true);

function mkAssistant(extra) {
  return Object.assign({ role: 'assistant', _msgId: 'a1', content: 'partial answer' }, extra || {});
}

// ══ (1) an ERRORED last assistant turn → .turn-failed + Continue present ══
{
  const msg = mkAssistant({ error: { kind: 'stream_error', message: 'boom' } });
  _conv.messages = [{ role: 'user', content: 'go' }, msg];
  const html = renderMessage(msg, 1);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  check('err_turn_failed_class', !!frag.querySelector('.message.turn-failed'));
  // Continue is emitted (last assistant, no active stream) and lives in the
  // bottom action bar.
  const cont = frag.querySelector('.msg-continue-btn');
  check('err_continue_present', !!cont);
  check('err_continue_in_actions',
        cont && !!cont.closest('.message-actions'));
}

// ══ (2) an INTERRUPTED last assistant turn → .turn-failed ══
{
  const msg = mkAssistant({ finishReason: 'interrupted' });
  _conv.messages = [{ role: 'user', content: 'go' }, msg];
  const html = renderMessage(msg, 1);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  check('intr_turn_failed_class', !!frag.querySelector('.message.turn-failed'));
}

// ══ (3) a SETTLED successful last assistant turn → NO .turn-failed ══
{
  const msg = mkAssistant({ finishReason: 'stop', usage: { total: 10 } });
  _conv.messages = [{ role: 'user', content: 'go' }, msg];
  const html = renderMessage(msg, 1);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  check('ok_no_turn_failed_class', !frag.querySelector('.message.turn-failed'));
}

// ══ (4) a USER turn is never tagged failed (assistant-lane only) ══
{
  const msg = { role: 'user', _msgId: 'u1', content: 'hi', error: { kind: 'x' } };
  _conv.messages = [msg];
  const html = renderMessage(msg, 0);
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  check('user_no_turn_failed_class', !frag.querySelector('.message.turn-failed'));
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_failed_turn_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, ROOT, nc],
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
def test_failed_turn_reveals_action_bar():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'failed-turn action-bar reveal failures:\n' + output
    for needed in ('PASS err_turn_failed_class', 'PASS err_continue_present',
                   'PASS err_continue_in_actions', 'PASS intr_turn_failed_class',
                   'PASS ok_no_turn_failed_class', 'PASS user_no_turn_failed_class'):
        assert needed in output, f'missing {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_stamp_regression_is_caught():
    """NC-1: dropping the turn-failed stamp must break the tagging assertions."""
    output = _run('nc_stamp')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL err_turn_failed_class' in output, (
        'Dropping the turn-failed stamp did NOT fail the tagging assertion — '
        f'the stamp is not load-bearing:\n{output}')


def test_nc_css_reveal_regression_is_caught():
    """NC-2 (pure Python): if the CSS reveal rule is removed, the CSS assertion
    must fail. Simulate by stripping the rule from an in-memory copy."""
    css = _strip_comments(open(CSS, encoding='utf-8').read())
    neutered = re.sub(
        r'\.message\.turn-failed\s+\.message-actions\{[^}]*\}', '', css)
    assert not re.search(
        r'\.message\.turn-failed\s+\.message-actions\{[^}]*opacity:1', neutered), (
        'stripping the reveal rule left it detectable — the NC is not '
        'exercising the real selector')


if __name__ == '__main__':
    test_css_reveals_failed_turn_action_bar()
    test_css_base_action_bar_is_bottom_hover_reveal()
    test_nc_css_reveal_regression_is_caught()
    if not _node_deps_available():
        print('SKIP JS half — node + jsdom not available')
    else:
        test_failed_turn_reveals_action_bar()
        test_nc_stamp_regression_is_caught()
        print('PASS test_frontend_failed_turn_actions_reveal')

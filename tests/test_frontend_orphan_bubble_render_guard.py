"""tests/test_frontend_orphan_bubble_render_guard.py — RED test for #3 of the
empty-"Agent" air-bubble root fix: the render-time BELT.

WHY (the last line of defense)
------------------------------
#1 (warm-open adopts the reconciled list) and #2 (settle-time reconcile at
task-end) kill the orphan at the source. The belt guarantees that even a ghost
that somehow escapes both — a transient in-memory empty-assistant with no live
stream — never RENDERS as a blank "Agent" bubble.

`renderMessage` (static/js/ui/chat_render.js) currently renders every message
unconditionally, and `renderBranchZone` appends the 分支 button to any non-user
message → an empty `{role:'assistant', content:''}` = a blank bubble + branch
button. The belt adds an early return: an assistant message that is an ORPHAN
placeholder produces NO DOM (renderMessage returns '').

ORPHAN predicate (owner-specified — all clauses must hold):
  no content  AND  no thinking  AND  no toolRounds  AND  no error  AND
  no finishReason  AND  no LIVE STREAM bound to THIS message.
It must NOT key on merely-empty content, or a legitimate "Preparing…" pre-first-
token bubble (empty but with a live stream bound) would blank.

The "live stream bound" check is IDENTITY-based (an activeStreams entry whose
`assistantMsg` IS this message object, or whose `taskId === msg._taskId`) —
consistent with resolving stream slots by _taskId, never by array position.

CHECKS (RED until the belt lands)
  A. empty + LIVE STREAM bound (assistantMsg===msg) → renderMessage SHOWS a bubble
  B. empty + NO stream (orphan)                     → renderMessage returns '' (HIDDEN)
  Controls (each independently forces SHOW — proves not keyed on empty content):
  C. empty + finishReason                           → SHOWS
  D. empty content but has thinking                 → SHOWS
  E. empty content but has toolRounds               → SHOWS
  F. empty content but has error                    → SHOWS
  G. empty + stream bound by _taskId (not object)   → SHOWS (identity via taskId)
  H. a normal assistant with content                → SHOWS (sanity)

NEUTER CONTROLS
  • nc_guard_off: force the orphan guard to never fire → B (orphan HIDDEN) FAILS.
  • nc_ignore_stream: drop the live-stream clause → A (streaming SHOWS) FAILS.
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
global.setTimeout = win.setTimeout = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const _conv = { id: 'c-belt', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c-belt';
win.getActiveConv = global.getActiveConv = () => _conv;

win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">TOOLS</div>';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
const CHAT = chatSrc;
const NC = process.argv[6] || '';
if (NC === 'nc_guard_off') {
  // Force the orphan predicate to always return false → guard never fires.
  chatSrc = CHAT.replace(
    'function _isOrphanEmptyAssistant(msg) {',
    'function _isOrphanEmptyAssistant(msg) {\n  return false; // NC nc_guard_off');
} else if (NC === 'nc_ignore_stream') {
  // Drop the live-stream clause → an empty+streaming msg is treated as orphan.
  chatSrc = CHAT.replace('if (_streamBoundToMsg(msg)) return false;',
                         'if (false && _streamBoundToMsg(msg)) return false; // NC nc_ignore_stream');
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

function empty(extra) { return Object.assign({ role: 'assistant', content: '' }, extra || {}); }
function shows(html) { return typeof html === 'string' && html.trim().length > 0; }

// ══ A. empty + LIVE STREAM bound by object identity → SHOWS ══
{
  const msg = empty();
  activeStreams.set('c-belt', { controller: {}, taskId: 'T1', assistantMsg: msg });
  const html = renderMessage(msg, 0);
  check('A_streaming_bubble_shows', shows(html));
  activeStreams.clear();
}

// ══ B. empty + NO stream (orphan) → HIDDEN ('') ══
{
  const html = renderMessage(empty(), 0);
  check('B_orphan_hidden', html === '');
}

// ══ Controls: each single non-empty signal forces SHOW ══
{
  check('C_finishReason_shows', shows(renderMessage(empty({ finishReason: 'stop' }), 0)));
  check('D_thinking_shows', shows(renderMessage(empty({ thinking: 'reasoning...' }), 0)));
  check('E_toolRounds_shows', shows(renderMessage(
    empty({ toolRounds: [{ toolCallId: 't', toolName: 'read_files', status: 'done' }] }), 0)));
  check('F_error_shows', shows(renderMessage(empty({ error: { message: 'boom' } }), 0)));
}

// ══ G. empty + stream bound by _taskId (not object identity) → SHOWS ══
{
  const msg = empty({ _taskId: 'T7' });
  activeStreams.set('c-belt', { controller: {}, taskId: 'T7', assistantMsg: {} });
  check('G_streaming_by_taskid_shows', shows(renderMessage(msg, 0)));
  activeStreams.clear();
}

// ══ H. a normal assistant with content → SHOWS (sanity) ══
{
  check('H_normal_shows', shows(renderMessage({ role: 'assistant', content: 'hello' }, 0)));
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_orphan_guard_harness_{nc or "main"}.js')
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
def test_orphan_bubble_hidden_streaming_shows():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'orphan-bubble render-guard failures:\n' + output
    for want in ('PASS A_streaming_bubble_shows', 'PASS B_orphan_hidden',
                 'PASS C_finishReason_shows', 'PASS D_thinking_shows',
                 'PASS E_toolRounds_shows', 'PASS F_error_shows',
                 'PASS G_streaming_by_taskid_shows', 'PASS H_normal_shows'):
        assert want in output, 'EXPECTED-RED until the belt lands:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_guard_off_regression_is_caught():
    """Disabling the orphan guard must make the orphan render (B fails)."""
    output = _run('nc_guard_off')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL B_orphan_hidden' in output, (
        'Disabling the guard did NOT fail B — the guard is not load-bearing:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_ignore_stream_regression_is_caught():
    """Dropping the live-stream clause must blank a streaming bubble (A fails)."""
    output = _run('nc_ignore_stream')
    assert 'PASS nc_pattern_applied' in output, f'NC did not apply:\n{output}'
    assert 'FAIL A_streaming_bubble_shows' in output, (
        'Dropping the live-stream clause did NOT fail A — the clause is not '
        'load-bearing:\n' + output)


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_orphan_bubble_hidden_streaming_shows()
        test_nc_guard_off_regression_is_caught()
        test_nc_ignore_stream_regression_is_caught()
        print('PASS test_frontend_orphan_bubble_render_guard')

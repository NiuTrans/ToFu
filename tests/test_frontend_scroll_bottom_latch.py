"""tests/test_frontend_scroll_bottom_latch.py — an explicit click on
"scroll to latest" is a COMMAND that must survive every render still to come
during the conversation open; unsolicited repaints must never outrank it.

WHY (the second door of "pushed back to the middle by newly loaded content")
-----------------------------------------------------------------------------
Door 1 (fixed in c1ab6358): the bottom-sentinel drip-feed after the bounded
window evicted the tail. Door 2 (THIS suite): with slow sync, the Phase-2
server reconcile lands SECONDS after the user opened the conversation — well
within the window where the user has already clicked "scroll to latest".
That click ran a plain `_forceScrollToBottom` with no persistent intent, so
when Phase-2 then triggered a full render during the open, renderChat's
open-scroll coalescing (`_openAlreadyPositioned`) captured a scroll ANCHOR —
a heuristic built for UNSOLICITED repaints — and re-pinned the reader to
wherever they happened to be, dragging them off the bottom again.

FIX
  `_explicitBottomLatch` (streaming_render.js): set ONLY by scrollChatToBottom;
  while it names the active conv, renderChat skips the anchor capture and
  re-pins to the TRUE bottom in BOTH the full-render and surgical
  background-repaint paths. Cleared by: manual scroll-up input (wheel-up /
  touch drag-down, core.js listeners), an explicit scrollToTurn navigation,
  a conversation switch, and open end (`delete c._initialSwitchLoad`).
  Deliberately NOT cleared on reaching the bottom — mid-open that position is
  transient and later same-open renders must keep following the command.

NEUTER CONTROL
  NC: drop the `_explicitBottomLatch !== conv.id` term from the full-render
  anchor condition → the Phase-2 render captures an anchor again and the
  reader is dragged off the bottom (the bug returns), while earlier steps
  stay green — proving the term, and only it, is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ══════════════════════════════════════════════════════════════════════════
# streaming_render.js (the REAL lazy-window machinery + latch declaration),
# chat_render.js (the REAL renderChat with both scroll heuristics), and the
# REAL scrollChatToBottom + latch-clear listeners extracted from core.js are
# eval'd in ONE scope — exactly how the bundler concatenates them. The latch
# and window bounds are `let`-declared in streaming_render.js and read/written
# by the other two; splitting the eval would hand each file different bindings
# and the test would prove nothing.
# ══════════════════════════════════════════════════════════════════════════

_DRIVER = r"""
;(function () {
  const out = global.__out;
  const inner = document.getElementById('chatInner');
  const container = document.getElementById('chatContainer');
  function check(name, cond, extra) {
    out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : ''));
  }
  const conv = conversations[0];
  const BOTTOM = () => container.scrollHeight - 800;

  // ── Step 1: first paint during the open — no auto-scroll (owner directive),
  //    the open-scroll latch engages. View sits at the TOP of the tail window.
  conv._initialSwitchLoad = true;
  _lastRenderedFingerprint = '';
  renderChat(conv, false);
  check('open_first_paint_no_scroll',
        container.scrollTop === 0 && _openScrollConvId === 'c1',
        'scrollTop=' + container.scrollTop + ' openLatch=' + _openScrollConvId);

  // ── Step 2: the user clicks "scroll to latest" (H=100 → bottom = 1200).
  scrollChatToBottom();
  check('click_reaches_bottom', container.scrollTop === 1200,
        'scrollTop=' + container.scrollTop);
  check('click_sets_latch', _explicitBottomLatch === 'c1',
        'latch=' + _explicitBottomLatch);

  // ── Step 3: Phase-2 lands — every bubble triples in height (H=300) and a
  //    full render runs while the open is still in flight. With the latch the
  //    reader must stay pinned to the TRUE bottom (6000-800 = 5200); without
  //    it (NC) the anchor heuristic re-pins to 1200 — the reported yank.
  __setH(300);
  _lastRenderedFingerprint = '';
  renderChat(conv, false);
  check('latch_survives_phase2', container.scrollTop === 5200,
        'scrollTop=' + container.scrollTop + ' bottom=' + BOTTOM());

  // ── Step 4: a mid-open BACKGROUND repaint (cost/file-change data) drifts
  //    the reader — the latch must re-pin in the surgical path too.
  container.scrollTop = 1000;
  conv._bgRepaint = true;
  _lastRenderedFingerprint = '';
  renderChat(conv, false);
  delete conv._bgRepaint;
  check('latch_repins_bg_repaint', container.scrollTop === BOTTOM(),
        'scrollTop=' + container.scrollTop);

  // ── Step 5: open end (mirrors main_conv_lifecycle.js) — the latch releases
  //    and the scroll-preservation heuristics resume for unsolicited paints.
  _explicitBottomLatch = null;
  delete conv._initialSwitchLoad;
  container.scrollTop = 1000;
  _lastRenderedFingerprint = '';
  renderChat(conv, false);
  check('open_end_restores_heuristic', container.scrollTop === 1000,
        'scrollTop=' + container.scrollTop);

  // ── Step 6: manual scroll-up input clears the latch (wheel-up).
  scrollChatToBottom();
  const cleared = (() => {
    const e = new window.Event('wheel');
    e.deltaY = -120;
    container.dispatchEvent(e);
    return _explicitBottomLatch === null;
  })();
  check('wheel_up_clears_latch', cleared, 'latch=' + _explicitBottomLatch);

  console.log(out.join('\n'));
})();
"""

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const JS = path.join(ROOT, 'static', 'js');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

// ── Deterministic geometry: every bubble H px (mutable — Phase-2 triples it),
//    viewport 800px. Mirrors a real browser: heights change when the sync
//    lands richer content (thinking / tool rounds / hydrated images).
const CLIENT_H = 800;
let H = 100;
global.__setH = (v) => { H = v; };
let _scrollTop = 0;
function _idxs() {
  const els = document.getElementById('chatInner').querySelectorAll('[id^="msg-"]');
  const out = [];
  els.forEach((el) => { const m = el.id.match(/^msg-(\d+)$/); if (m) out.push(parseInt(m[1], 10)); });
  return out.sort((a, b) => a - b);
}
const container = document.getElementById('chatContainer');
Object.defineProperty(container, 'clientHeight', { get: () => CLIENT_H, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => _idxs().length * H, configurable: true });
Object.defineProperty(container, 'scrollTop', {
  get: () => _scrollTop,
  set: (v) => { const max = Math.max(0, _idxs().length * H - CLIENT_H); _scrollTop = Math.max(0, Math.min(v, max)); },
  configurable: true,
});
win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: CLIENT_H, height: CLIENT_H, left: 0, right: 0, width: 0 };
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) {
    const order = _idxs().indexOf(parseInt(m[1], 10));
    if (order < 0) return { top: 0, bottom: 0, height: H, left: 0, right: 0, width: 0 };
    const top = order * H - _scrollTop;
    return { top, bottom: top + H, height: H, left: 0, right: 0, width: 0 };
  }
  return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 };
};
win.Element.prototype.scrollIntoView = function () {};

global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.IntersectionObserver = global.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {}
};

// ── Stubs (same surface the chat_render gate harness uses; the lazy-window
//    primitives are NOT stubbed — the real ones come from streaming_render.js).
const conv = { id: 'c1', title: 't', messages: [] };
for (let i = 0; i < 100; i++) {
  conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'msg ' + i, _msgId: 'm' + i,
                       finishReason: i % 2 ? 'stop' : undefined });
}
win.conversations = global.conversations = [conv];
win.activeConvId = global.activeConvId = 'c1';
win.getActiveConv = global.getActiveConv = () => conv;
win.activeStreams = global.activeStreams = new Map();
win._getChatContainer = global._getChatContainer = () => container;
win._updateScrollToBottomBtn = global._updateScrollToBottomBtn = () => {};
win.isNearBottom = global.isNearBottom = (thr) =>
  container.scrollHeight - container.scrollTop - container.clientHeight < (thr || 150);
win.t = global.t = (k) => k;
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
const _noop = () => '';
for (const n of ['renderToolRoundsHTML','renderSegmentTimelineHTML','renderErrorEnvelope',
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar','renderBranchZone',
  'renderTurnCtxNote','renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','calcCostCny','renderTranslateIndicator',
  '_welcomePillsHtml','assertChatInnerOrder','buildTurnNav']) {
  if (typeof win[n] === 'undefined') { win[n] = global[n] = _noop; }
}
win._apSummariesFp = global._apSummariesFp = () => '0';
win._prefetchConvCosts = global._prefetchConvCosts = () => Promise.resolve(false);
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => Promise.resolve(false);
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<i>u</i>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<i>w</i>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<i>c</i>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<i>p</i>';
win.BASE_PATH = global.BASE_PATH = '';
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win.chatInnerInsert = global.chatInnerInsert = undefined;
win.chatInnerHeadAnchor = global.chatInnerHeadAnchor = undefined;
global.__out = [];

const CORE_SRC = fs.readFileSync(path.join(JS, 'core.js'), 'utf8');
const FP_FN = CORE_SRC.match(/function _convRenderFingerprint[\s\S]*?\n\}/)[0];
const AP_FP = CORE_SRC.match(/function _apSummariesFp[\s\S]*?\n\}/)[0];
const ARM_FN = CORE_SRC.match(/let _stbLatchListenersArmed[\s\S]*?\n\}/)[0];
const STB_FN = CORE_SRC.match(/function scrollChatToBottom\(\) \{[\s\S]*?\n\}/)[0];

let CR = fs.readFileSync(path.join(JS, 'ui', 'chat_render.js'), 'utf8');
if (NC === 'nc_latch') {
  // NC: drop the latch term from the full-render anchor condition → Phase-2
  // captures an anchor again and the reader is dragged off the bottom.
  const before = CR;
  CR = CR.replace('      && _explicitBottomLatch !== conv.id\n', '');
  if (CR === before) { console.log('FAIL nc_pattern_applied'); process.exit(0); }
}
console.log('PASS nc_pattern_applied');

const SR = fs.readFileSync(path.join(JS, 'ui', 'streaming_render.js'), 'utf8');
/* Same support files the chat_render gate harness evals — renderMessage reads
 * displayContent (translation_model.js) and the escape/safe-html helpers. */
const PRE = ['core', 'escape_html.js', 'core', 'safe_html.js', 'core', 'translation_model.js',
             'ui', 'translation_indicator.js', 'core', 'turn_settlement.js']
  .reduce((acc, _, i, arr) => (i % 2 ? acc : acc + fs.readFileSync(path.join(JS, arr[i], arr[i + 1]), 'utf8') + '\n'), '');
const DRIVER = __DRIVER__;
eval(PRE + AP_FP + '\n' + FP_FN + '\n' + SR + '\n' + CR + '\n' + ARM_FN + '\n' + STB_FN + '\n' + DRIVER);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_bottomlatch_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS.replace('__DRIVER__', json.dumps(_DRIVER)))
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, nc],
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


def _verdicts(output: str) -> dict:
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_explicit_bottom_latch_survives_open_renders():
    """After clicking "scroll to latest", the Phase-2 reconcile and background
    repaints landing mid-open must keep the reader pinned to the TRUE bottom;
    after the open ends or the user scrolls up, the heuristics resume."""
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'latch failures:\n' + output
    for needed in ('open_first_paint_no_scroll', 'click_reaches_bottom',
                   'click_sets_latch', 'latch_survives_phase2',
                   'latch_repins_bg_repaint', 'open_end_restores_heuristic',
                   'wheel_up_clears_latch'):
        assert f'PASS {needed}' in output, f'missing PASS {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_without_latch_term_phase2_drags_reader_off_bottom():
    """NC: drop the latch term from the anchor condition → the Phase-2 render
    re-pins to the anchor (1200) instead of the bottom (5200): the yank
    returns. Earlier steps stay green, proving the NC is surgical."""
    v = _verdicts(_run('nc_latch'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    assert v.get('latch_survives_phase2') == 'FAIL', (
        'Removing the latch term did NOT reintroduce the Phase-2 yank — the '
        f'term is not load-bearing: {v}')
    assert v.get('open_first_paint_no_scroll') == 'PASS', v
    assert v.get('click_reaches_bottom') == 'PASS', v

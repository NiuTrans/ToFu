"""Opening a conversation must position the view EXACTLY ONCE (no re-snaps).

WHY
---
Symptom (owner): opening a historical conversation "jumps around the page,
even several times in a row." Traced root cause: a single conversation OPEN
drives SEVERAL full renders — the Phase-1 IndexedDB cache paint, the Phase-2
server-reconcile paint, and the loadConversation() `.then()` fallback — and
each used to run `_forceScrollToBottom`. After the FIRST render lands the reader
at the bottom, `isNearBottom()` is true, so every SUBSEQUENT same-open full
render force-scrolled AGAIN → a visible re-snap per phase.

THE FIX (static/js/ui/chat_render.js + streaming_render.js)
-----------------------------------------------------------
Open-scroll coalescing: the first full render during an open
(`conv._initialSwitchLoad` set) force-scrolls to the bottom AND latches
`_openScrollConvId = conv.id`. Every LATER render of that same open takes the
anchor-preserve branch instead of re-snapping — even when the reader is at the
bottom — because `_openAlreadyPositioned` widens the capture guard.
`loadConversation` resets `_openScrollConvId` at the start of each open so the
next open positions exactly once.

This harness installs a deterministic layout model (like
test_frontend_full_render_scroll_anchor.py) and asserts:
  1. render #1 of an open (switch) force-scrolls exactly once;
  2. render #2 of the SAME open (Phase-2 reconcile, _initialSwitchLoad still
     set, reader now at bottom) does NOT force-scroll again;
  3. a fresh open (latch reset, _lazyConvId already this conv) force-scrolls
     again — proving the latch is per-open, not permanent.

NEUTER: remove the `_openAlreadyPositioned` widening from the capture guard →
render #2 re-snaps (force-scroll fires again) → assertion (2) fails. Proves the
widening is load-bearing.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const NEUTER = process.argv[4] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'oc-conv';

const MSG_H = 200;
const N = 6;
let _scrollTop = 0;
function _docTop(idx) { return idx * MSG_H; }
win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: 600, left: 0, right: 0, width: 0, height: 600 };
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) {
    const idx = parseInt(m[1], 10);
    const top = _docTop(idx) - _scrollTop;
    return { top, bottom: top + MSG_H, left: 0, right: 0, width: 0, height: MSG_H };
  }
  return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
};
const container = document.getElementById('chatContainer');
const inner = document.getElementById('chatInner');
Object.defineProperty(container, 'scrollTop', { get: () => _scrollTop, set: (v) => { _scrollTop = v; }, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => N * MSG_H, configurable: true });

const _innerHtmlDesc = Object.getOwnPropertyDescriptor(win.Element.prototype, 'innerHTML');
Object.defineProperty(inner, 'innerHTML', {
  get() { return _innerHtmlDesc.get.call(this); },
  set(v) { _innerHtmlDesc.set.call(this, v); _scrollTop = 0; },  // browser resets scroll on wipe
  configurable: true,
});

win._applyAutopilotRunFolds = global._applyAutopilotRunFolds = () => {};
win._convRenderFingerprint = global._convRenderFingerprint = (c) => {
  if (!c || !c.messages || !c.messages.length) return 'fp-0';
  const last = c.messages[c.messages.length - 1];
  return 'fp-' + c.messages.length + ':' + ((last && last.content) || '');
};
win._stampFreshness = global._stampFreshness = () => {};
win._ensureLazyObserver = global._ensureLazyObserver = () => {};
win._destroyLazyObserver = global._destroyLazyObserver = () => {};
win.buildTurnNav = global.buildTurnNav = () => {};
win.isNearBottom = global.isNearBottom = (thr) => {
  const max = N * MSG_H - 600;
  return _scrollTop >= max - (thr || 0);
};
let _forceScrollCalls = 0;
win._forceScrollToBottom = global._forceScrollToBottom = (c) => {
  _forceScrollCalls++;
  _scrollTop = N * MSG_H - 600;   // bottom
};
win.raw = global.raw = (s) => ({ toString: () => String(s) });
win.safeHtml = global.safeHtml = (strings, ...vals) => {
  let out = '';
  strings.forEach((s, i) => { out += s + (i < vals.length ? String(vals[i] == null ? '' : vals[i]) : ''); });
  return { toString: () => out };
};
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = Infinity;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._openScrollConvId = global._openScrollConvId = null;
win._prefetchConvCosts = global._prefetchConvCosts = () => Promise.resolve(false);
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => Promise.resolve(false);
const _noop = () => '';
for (const name of ['renderMarkdown','getToolRoundsFromMsg','renderFinishInfo',
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderPreferenceLearnedHtml',
  '_buildSwarmInboxChipsHTML','renderTurnCtxNote','_injectAnchoredBranches',
  'stripNoTranslateTags','showStreamingUIForConv','scrollToBottom',
  '_lazyObserver']) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}

let src = fs.readFileSync(process.argv[2], 'utf8');  // ui/chat_render.js

if (NEUTER === 'widen') {
  // Remove the open-already-positioned widening → later same-open renders
  // fall back to the plain near-bottom test and re-snap.
  const before = src;
  src = src.replace('      && (!_readerNearBottom || _openAlreadyPositioned))',
                    '      && (!_readerNearBottom))');
  if (src === before) { console.log('FAIL neuter_widen_not_applied'); process.exit(0); }
}

eval(src);

renderMessage = win.renderMessage = global.renderMessage = (msg, idx) =>
  '<div class="message" id="msg-' + idx + '" data-mfp="v' + ((msg && msg.content) || '') + '">' +
  ((msg && msg.content) || '') + '</div>';

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const conv = { id: 'oc-conv', activeTaskId: null, messages: [] };
for (let i = 0; i < N; i++) conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i });

// ── Simulate an OPEN: loadConversation resets the latch, then the multi-phase
//    renders fire while _initialSwitchLoad is set. ──
_openScrollConvId = null;          // loadConversation reset
conv._initialSwitchLoad = true;
_lazyConvId = null;                // a genuine SWITCH (prev conv was different)

// Render #1 — Phase-1 cache paint (switch first-paint).
_scrollTop = 0;
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render1_force_scrolls_once', _forceScrollCalls === 1);
check('open_render1_latched', _openScrollConvId === 'oc-conv');
check('open_render1_at_bottom', win.isNearBottom(0) === true);

// Render #2 — Phase-2 server reconcile paint, SAME open (_initialSwitchLoad
// still set), reader currently AT THE BOTTOM (isNearBottom true). This is the
// render that used to re-snap. It must NOT force-scroll again.
// Vary the tail content so the fingerprint guard does NOT short-circuit —
// Phase-1 (stale cache) and Phase-2 (server) legitimately differ; that's the
// whole reason two full renders run in one open.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-server-reconciled' };
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render2_no_resnap', _forceScrollCalls === 0);

// Render #3 — the loadConversation .then() fallback would also be same-open.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-final' };
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render3_no_resnap', _forceScrollCalls === 0);

// ── A genuinely FRESH open (user clicks AWAY to another conv, then BACK).
//    Switching away moves _lazyConvId to the other conv, so returning is a
//    real switch first-paint: _sameConvDom is false → force-scroll once again.
//    Proves the no-resnap suppression is scoped to ONE open, not permanent. ──
delete conv._initialSwitchLoad;    // previous open completed
const other = { id: 'other-conv', activeTaskId: null, messages: [
  { role: 'user', content: 'x0' }, { role: 'assistant', content: 'x1' },
] };
_openScrollConvId = null;          // loadConversation reset
other._initialSwitchLoad = true;
global.activeConvId = win.activeConvId = 'other-conv';
_scrollTop = 0; _forceScrollCalls = 0;
renderChat(other, false);          // renders other conv → _lazyConvId = other-conv
delete other._initialSwitchLoad;

// Now come BACK to oc-conv — a genuine switch first-paint.
global.activeConvId = win.activeConvId = 'oc-conv';
_openScrollConvId = null;          // loadConversation reset for the new open
conv._initialSwitchLoad = true;
_forceScrollCalls = 0;
renderChat(conv, false);
check('fresh_open_force_scrolls_again', _forceScrollCalls === 1);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_open_scroll_once_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'chat_render.js'),   # argv[2]
             ROOT,                                            # argv[3]
             neuter,                                          # argv[4]
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


def _lines(output):
    return {ln[5:]: ln[:4].strip() for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_open_positions_view_exactly_once():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'open-scroll-once failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_open_scroll_widening_is_load_bearing():
    """Remove the _openAlreadyPositioned widening → render #2 of the same open
    re-snaps to the bottom (force-scroll fires again). Proves the widening is
    what suppresses the repeated jumps."""
    lines = _lines(_run('widen'))
    assert lines.get('open_render1_force_scrolls_once') == 'PASS', lines
    assert lines.get('open_render2_no_resnap') == 'FAIL', lines

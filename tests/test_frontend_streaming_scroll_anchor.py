"""jsdom regression for showStreamingUIForConv scroll anchoring.

WHY
---
`showStreamingUIForConv(convId)` (static/js/ui/stream_lifecycle.js) rebuilds the
WHOLE message list with `inner.innerHTML = html` (which hard-resets scrollTop→0)
and used to ALWAYS end in `_forceScrollToBottom(null, true)`. During a LIVE turn,
ANY `renderChat()` funnels here via Guard 1c (chat_render.js) — e.g. a cold-round
`tool_compacted` fires `renderChat(conv, false)` roughly once per tool round — so
a reader who scrolled UP to read history was repeatedly YANKED back to the bottom
while the bottom block flashed/re-rendered. This is the user-reported
"scrolling up keeps forcing me back to the bottom + the file-edit block flashes
several times" bug.

THE FIX
-------
Mirror renderChat's full-render path + `_bgRefreshChat`: when the reader is parked
UP on the SAME conversation (DOM already shows this conv, not near the bottom),
capture their viewport anchor from the OLD DOM before the innerHTML wipe
(`_captureScrollAnchor`) and re-pin it afterwards (`_restoreScrollAnchor`) instead
of force-scrolling. A genuine conversation SWITCH, a first load, or a near-bottom
reader still lands at the bottom via `_forceScrollToBottom`.

jsdom does no layout, so this harness installs a DETERMINISTIC layout model and a
faithful copy of the two anchor helpers (their own arithmetic is covered by
tests/test_frontend_full_render_scroll_anchor.py). It seeds the conv DOM as if
already rendered (same-conv), parks the reader UP, triggers a
`showStreamingUIForConv` rebuild, and asserts the anchored element keeps its
viewport offset (a force-to-bottom would move it far away).

DOUBLE-NEUTER (both on the SHIPPED file):
  • 'capture'  — force `_preSwapAnchor = null` → the rebuild always
                 force-scrolls; the parked reader's offset is NOT preserved AND
                 the force-scroll path IS taken. Proves the capture guard is what
                 diverts a parked reader away from the yank-to-bottom.
  • 'restore'  — turn the `_restoreScrollAnchor(...)` call into a no-op → the
                 anchor branch is taken (no force-scroll) but scrollTop stays 0
                 from the wipe, so the offset is NOT preserved. Proves the
                 restore CALL is load-bearing.
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
global.setTimeout = win.setTimeout = (fn) => 0;   // no async layout in jsdom
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'sc-conv';
win.streamBufs = global.streamBufs = new Map();

// ── Deterministic layout model ───────────────────────────────────────────
// Container viewport = [0, 600]. 5 static messages (msg-0..msg-4) + a trailing
// #streaming-msg, each 200px tall, stacking vertically. Doc height = 1200px,
// taller than the viewport, so there is real room to be "parked up".
const MSG_H = 200;
const TOTAL_BLOCKS = 6;                       // 5 static + 1 streaming
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
  if (this.id === 'streaming-msg') {
    const top = _docTop(5) - _scrollTop;      // streaming bubble sits last
    return { top, bottom: top + MSG_H, left: 0, right: 0, width: 0, height: MSG_H };
  }
  return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
};
const container = document.getElementById('chatContainer');
const inner = document.getElementById('chatInner');
Object.defineProperty(container, 'scrollTop', { get: () => _scrollTop, set: (v) => { _scrollTop = v; }, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => TOTAL_BLOCKS * MSG_H, configurable: true });

// A full innerHTML wipe hard-resets the scroll container to 0 in a real
// browser — jsdom does not, so model it (this is what makes the 'restore'
// neuter observable: without the restore call, scrollTop stays 0).
const _innerHtmlDesc = Object.getOwnPropertyDescriptor(win.Element.prototype, 'innerHTML');
Object.defineProperty(inner, 'innerHTML', {
  get() { return _innerHtmlDesc.get.call(this); },
  set(v) { _innerHtmlDesc.set.call(this, v); _scrollTop = 0; },
  configurable: true,
});

// ── Faithful copy of the shared anchor helpers (their own arithmetic is
//    covered by test_frontend_full_render_scroll_anchor.py). ──
win._captureScrollAnchor = global._captureScrollAnchor = function (container, inner) {
  if (!container || !inner) return null;
  const cTop = container.getBoundingClientRect().top;
  const els = inner.querySelectorAll('[id^="msg-"]');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.bottom > cTop + 1) return { id: el.id, offset: r.top - cTop };
  }
  return null;
};
win._restoreScrollAnchor = global._restoreScrollAnchor = function (container, anchor) {
  if (!container || !anchor || !anchor.id) return false;
  const a = document.getElementById(anchor.id);
  if (!a) return false;
  const newOffset = a.getBoundingClientRect().top - container.getBoundingClientRect().top;
  container.scrollTop += (newOffset - anchor.offset);
  return true;
};

win.isNearBottom = global.isNearBottom = (thr) => {
  const max = TOTAL_BLOCKS * MSG_H - 600;
  return _scrollTop >= max - (thr || 0);
};
let _forceScrollCalls = 0;
win._forceScrollToBottom = global._forceScrollToBottom = () => {
  _forceScrollCalls++;
  _scrollTop = TOTAL_BLOCKS * MSG_H - 600;   // bottom
};

// ── Stubs for the render dependencies showStreamingUIForConv touches. ──
win._destroyLazyObserver = global._destroyLazyObserver = () => {};
win._ensureLazyObserver = global._ensureLazyObserver = () => {};
win.buildTurnNav = global.buildTurnNav = () => {};
win.updateSendButton = global.updateSendButton = () => {};
win.updateStreamingUI = global.updateStreamingUI = () => {};
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = () => [];
win.formatClockTime = global.formatClockTime = () => '00:00';
win._streamingBubbleHTML = global._streamingBubbleHTML =
  (role, status, time, msgId) => '<div class="message" id="streaming-msg">stream</div>';
win.conversations = global.conversations = [];
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
// Module-level `let`s that stream_lifecycle.js reads as free vars (declared in
// streaming_render.js / core.js in the real bundle).
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = Infinity;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._lazyObserver = global._lazyObserver = null;

let src = fs.readFileSync(process.argv[2], 'utf8');  // ui/stream_lifecycle.js

if (NEUTER === 'capture') {
  src = src.replace(
    '  const _preSwapAnchor = (_sameConvDom && !_readerNearBottom\n      && typeof _captureScrollAnchor === \'function\')\n    ? _captureScrollAnchor(container, inner)\n    : null;',
    '  const _preSwapAnchor = null;  // NEUTERED-capture');
  if (src.indexOf('// NEUTERED-capture') < 0) { console.log('FAIL neuter_capture_not_applied'); process.exit(0); }
}
if (NEUTER === 'restore') {
  src = src.replace('_restoreScrollAnchor(container, _preSwapAnchor);',
                    'void 0;  // NEUTERED-restore');
  if (src.indexOf('// NEUTERED-restore') < 0) { console.log('FAIL neuter_restore_not_applied'); process.exit(0); }
}

eval(src);

// Deterministic renderMessage marker.
renderMessage = win.renderMessage = global.renderMessage = (msg, idx) =>
  '<div class="message" id="msg-' + idx + '">' + ((msg && msg.content) || '') + '</div>';

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Conversation: 5 settled messages + 1 in-progress streaming assistant.
const conv = { id: 'sc-conv', activeTaskId: 't1', messages: [] };
for (let i = 0; i < 5; i++) conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i, done: true, _msgId: 'm' + i });
conv.messages.push({ role: 'assistant', content: 'streaming', done: false, _msgId: 'live' });
conversations.push(conv);
activeStreams.set('sc-conv', { taskId: 't1' });
streamBufs.set('sc-conv', { content: 'streaming', thinking: '', toolRounds: [], phase: null });

// Seed the DOM as if this conv is already rendered (same-conv rebuild). Set
// _lazyConvId so the same-conv guard fires — the very first call is a "switch".
_scrollTop = 0;
showStreamingUIForConv('sc-conv');       // first paint (switch) → _lazyConvId=sc-conv, force-scroll
check('first_paint_has_streaming', !!document.getElementById('streaming-msg'));
check('first_paint_has_msg_dom', !!inner.querySelector('[id^="msg-"]'));

// Park the reader UP: msg-1 near the top of the viewport.
_scrollTop = 200;                         // msg-1 top at viewport y=0
const anchorBefore = win._captureScrollAnchor(container, inner);
check('parked_up_not_near_bottom', win.isNearBottom(120) === false);
check('anchor_is_msg1', !!anchorBefore && anchorBefore.id === 'msg-1');

_forceScrollCalls = 0;
// Trigger a SAME-CONV rebuild (e.g. a cold-round tool_compacted → renderChat →
// Guard 1c → showStreamingUIForConv).
showStreamingUIForConv('sc-conv');

const anchorAfterEl = document.getElementById(anchorBefore.id);
const anchorAfterOff = anchorAfterEl.getBoundingClientRect().top - 0;
check('anchor_offset_preserved', Math.abs(anchorAfterOff - anchorBefore.offset) <= 1);
check('force_scroll_not_used', _forceScrollCalls === 0);

// Control: a near-bottom reader SHOULD still land at the bottom.
_scrollTop = TOTAL_BLOCKS * MSG_H - 600;  // exactly bottom → isNearBottom true
_forceScrollCalls = 0;
showStreamingUIForConv('sc-conv');
check('near_bottom_uses_force_scroll', _forceScrollCalls === 1);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_streaming_scroll_anchor_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'),   # argv[2]
             ROOT,                                                 # argv[3]
             neuter,                                               # argv[4]
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
def test_streaming_rebuild_preserves_reader_viewport():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'streaming scroll-anchor failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_streaming_capture_guard_is_load_bearing():
    """Neuter the anchor CAPTURE (→ always null) → the rebuild falls back to
    force-scroll-to-bottom on the same-conv rebuild, so the offset is NOT
    preserved AND the force-scroll path IS taken. Proves the capture guard is
    what diverts a parked reader away from the yank-to-bottom."""
    lines = _lines(_run('capture'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('force_scroll_not_used') == 'FAIL', lines
    assert lines.get('near_bottom_uses_force_scroll') == 'PASS', lines


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_streaming_restore_call_is_load_bearing():
    """Neuter the restore CALL (→ no-op) → the anchor branch is still taken (no
    force-scroll) but scrollTop stays 0 from the innerHTML wipe, so the offset
    is NOT preserved. Proves the restore call itself is load-bearing."""
    lines = _lines(_run('restore'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('force_scroll_not_used') == 'PASS', lines
    assert lines.get('near_bottom_uses_force_scroll') == 'PASS', lines

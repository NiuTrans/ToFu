"""jsdom regression for the "send-at-bottom lands mid-history" scroll bug.

WHY
---
Every `.message` carries `content-visibility:auto; contain-intrinsic-size:auto
120px` (static/styles.css:325). A rendered bubble whose REAL height has not been
laid out reports the flat 120px estimate; a `cv-off` pass
(content-visibility:visible) forces real layout and — via the `auto`
intrinsic-size keyword — caches the true height for the session. Conversation
OPEN runs `_forceScrollToBottom(container,true)`, which does exactly that cv-off
+ forced-reflow + re-assert dance, so a freshly-opened conversation measures
correctly. But the send path used the plain single-rAF `scrollToBottom`, which
reads `scrollTop = scrollHeight` with NO cv-off guard.

The genuine trigger (the reviewer's #2/#3, NOT stale open-time history): a
rendered bubble whose real height is not currently cached — a lazy-loaded older
bubble (`_loadOlderMessages` inserts via a fragment and never runs a cv-off
pass) or a far-off-screen bubble whose `content-visibility:auto` size the
browser evicted — reverts to the 120px estimate. `scrollHeight` is then an
UNDER-estimate, `scrollTop = scrollHeight` clamps short, and after the browser
paints the newly-scrolled region and resolves real (taller) heights the true
bottom moves further down → the reader is parked in the MIDDLE.

This harness installs a DETERMINISTIC `content-visibility` layout model:
  • a bubble reports 120px until it has been "measured" (a cv-off reflow),
    after which its real (taller) height is cached;
  • `.cv-off` on #chatInner forces every bubble to measure NOW (what the real
    CSS `content-visibility:visible!important` + forced reflow achieve);
  • `scrollTop` is clamped to `scrollHeight - clientHeight` like a real element.

It reproduces the exact scenario: two above-fold bubbles are UN-measured
(lazy-loaded, still 120px) when the send fires. It records the trace the
reviewer asked for — `scrollHeight` at the plain-scroll moment vs after a forced
cv-off reflow — and asserts the post-send scroll lands at the TRUE bottom.

NEUTER: strip the `cv-off` guard out of `_forceScrollToBottom` (so it measures
against the 120px estimates like plain scrollToBottom) and prove the test then
catches the mid-scroll.
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
const ROOT = process.argv[2];
const NEUTER = process.argv[3] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ── Deterministic content-visibility:auto layout model ────────────────────
// Viewport (container clientHeight) = 800. Each rendered bubble has a REAL
// height; until it has been "measured" it reports the flat 120px estimate.
//
// FAITHFUL MODEL of content-visibility:auto in a NO-PAINT environment:
//   • A bubble's REAL height is resolved (and cached — the `auto` intrinsic-
//     size keyword) ONLY when it is forced to lay out. Two things force it:
//       (a) #chatInner carries the `cv-off` class
//           (content-visibility:visible!important + a forced reflow) — this
//           measures EVERY rendered bubble synchronously; OR
//       (b) an explicit paint pass over the bubbles inside the viewport
//           (`_paint()`) — the browser painting the newly-scrolled region.
//   • A plain `scrollTop = scrollHeight` read does NOT itself measure anything
//     (there is no synchronous layout of content-visibility:auto content on a
//     bare property read) — it just clamps against whatever the current
//     (possibly estimated) scrollHeight is.
//
// The bug: the freshly-appended user + streaming bubbles at the BOTTOM are
// un-measured (120px) when the single-rAF scrollToBottom reads scrollHeight. It
// clamps short; the bottom bubbles then PAINT to their real (taller) height, so
// scrollHeight grows BELOW the now-fixed scrollTop → the reader is left ABOVE
// the true bottom (mid-history). cv-off fixes it by resolving those heights
// BEFORE the scrollTop read, so the clamp targets the true bottom.
const CLIENT_H = 800;
const ESTIMATE = 120;
const REAL = {           // real heights — deliberately DIVERGENT from 120px
  0: 300,   // user      (history — measured at open)
  1: 500,   // assistant (history — measured at open)
  2: 300,   // user      (history — measured at open)
  3: 500,   // assistant (history — measured at open)
  4: 260,   // optimistic user bubble appended by "send" — UN-measured
  5: 160,   // streaming bubble appended by "send"        — UN-measured
};
const _measured = new Set();
let _scrollTop = 0;

function _cvOff() {
  const inner = document.getElementById('chatInner');
  return inner && inner.classList && inner.classList.contains('cv-off');
}
function _heightOf(idx) {
  if (!(idx in REAL)) return 0;
  // cv-off forces synchronous layout of ALL bubbles → measure + cache.
  if (_cvOff()) { _measured.add(idx); return REAL[idx]; }
  return _measured.has(idx) ? REAL[idx] : ESTIMATE;
}
function _renderedIndices() {
  const els = document.getElementById('chatInner').querySelectorAll('[id^="msg-"]');
  const out = [];
  els.forEach((el) => { const m = el.id.match(/^msg-(\d+)$/); if (m) out.push(parseInt(m[1], 10)); });
  return out.sort((a, b) => a - b);
}
function _scrollHeight() {
  let h = 0;
  for (const idx of _renderedIndices()) h += _heightOf(idx);
  return h;
}
function _docTop(idx) {
  let t = 0;
  for (const i of _renderedIndices()) { if (i >= idx) break; t += _heightOf(i); }
  return t;
}
// Explicit paint pass: the browser lays out (measures + caches) whatever bubbles
// currently intersect the viewport. Called to SIMULATE the async height
// resolution that happens AFTER a scroll — NOT wired into the scrollTop setter,
// because a bare scrollTop write triggers no synchronous cv:auto layout.
function _paint() {
  for (const idx of _renderedIndices()) {
    const top = _docTop(idx) - _scrollTop;
    const bot = top + _heightOf(idx);
    if (bot > 0 && top < CLIENT_H) _measured.add(idx);
  }
}
const container = document.getElementById('chatContainer');
Object.defineProperty(container, 'clientHeight', { get: () => CLIENT_H, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => _scrollHeight(), configurable: true });
Object.defineProperty(container, 'scrollTop', {
  get: () => _scrollTop,
  set: (v) => {
    const max = Math.max(0, _scrollHeight() - CLIENT_H);
    _scrollTop = Math.max(0, Math.min(v, max));   // clamp like a real element
  },
  configurable: true,
});
container.style = container.style || {};

win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: CLIENT_H, height: CLIENT_H, left: 0, right: 0, width: 0 };
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) { const idx = parseInt(m[1], 10); const top = _docTop(idx) - _scrollTop; const h = _heightOf(idx);
    return { top, bottom: top + h, height: h, left: 0, right: 0, width: 0 }; }
  return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 };
};

// rAF / setTimeout run synchronously so the double-rAF + 150ms re-assert in
// _forceScrollToBottom all fire within the test.
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };

global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);

// ── Load the REAL _forceScrollToBottom from streaming_render.js ────────────
let src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8');

if (NEUTER === 'cvoff') {
  // Strip the cv-off guard so _forceScrollToBottom measures against the 120px
  // estimates exactly like plain scrollToBottom — the pre-fix behaviour.
  src = src.replace("inner.classList.add('cv-off');\n    // Force sync reflow so heights are computed NOW.\n    void container.scrollHeight;",
                    "/* NEUTERED-cvoff: guard removed */ void 0;");
  if (src.indexOf('NEUTERED-cvoff') < 0) { console.log('FAIL neuter_cvoff_not_applied'); process.exit(0); }
}

// Extract just the _forceScrollToBottom function body via eval of the file in a
// sandbox that no-ops everything else. Simplest: eval the whole file after
// stubbing the symbols its module-scope references. It only DEFINES functions;
// nothing runs at load, so a bare eval is safe.
// Provide the few globals other funcs in the file close over.
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win.conversations = global.conversations = [];
win.activeConvId = global.activeConvId = 'c1';
win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
for (const n of ['renderMessage','buildTurnNav','_applyAutopilotRunFolds','_applyAutopilotSummaryPanels',
  'getToolRoundsFromMsg','updateStreamingUI','scrollToBottom','isNearBottom','formatClockTime',
  '_renderStreamingTranslatePreview','ConvView','ConvCache','saveConversations','twStart','twStop',
  'twUpdate','_streamingBubbleHTML']) {
  if (typeof win[n] === 'undefined') { win[n] = global[n] = () => {}; }
}
eval(src);

const out = [];
const trace = {};
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _forceScrollToBottom !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }

// ── Scenario builder ─────────────────────────────────────────────────────
// Rendered history (msgs 0..3), all measured at open. Then "send" appends the
// optimistic user bubble (msg-4) + the streaming bubble (msg-5), BOTH
// un-measured (120px estimate) — the appended-bottom content whose real height
// resolves only after the scroll paints. This is the faithful reproduction of
// reviewer option #1: the single-rAF scroll clamps against the estimate before
// the new bottom bubbles have their real height.
const inner = document.getElementById('chatInner');
function seed() {
  _measured.clear();
  inner.innerHTML =
    '<div class="message user-msg" id="msg-0">q1</div>' +
    '<div class="message" id="msg-1">a1</div>' +
    '<div class="message user-msg" id="msg-2">q2</div>' +
    '<div class="message" id="msg-3">a3</div>';
  // History was measured on open (its heights are cached).
  _measured.add(0); _measured.add(1); _measured.add(2); _measured.add(3);
  _scrollTop = 100000;   // sitting at the true bottom of history
  // "send": append the two new bottom bubbles, still at the 120px estimate.
  inner.insertAdjacentHTML('beforeend', '<div class="message user-msg" id="msg-4">new</div>');
  inner.insertAdjacentHTML('beforeend', '<div class="message" id="msg-5">stream</div>');
}

// True bottom = scrollTop that shows the end when ALL heights are real.
function trueBottomScrollTop() {
  const save = new Set(_measured);
  for (const k of Object.keys(REAL)) _measured.add(parseInt(k, 10));
  const tb = Math.max(0, _scrollHeight() - CLIENT_H);
  _measured.clear(); for (const v of save) _measured.add(v);
  return tb;
}

// ── TRACE: scrollHeight the plain single-rAF scrollToBottom reads (new bottom
//    bubbles still at 120px) vs after a forced cv-off reflow (real heights). ──
seed();
const shEstimate = container.scrollHeight;          // msg-4/5 → 120px each
inner.classList.add('cv-off'); void container.scrollHeight;
const shReal = container.scrollHeight;              // msg-4/5 → real heights
inner.classList.remove('cv-off');
trace.scrollHeight_estimate = shEstimate;
trace.scrollHeight_real = shReal;
trace.divergence_px = shReal - shEstimate;
check('trace_diverges', shReal > shEstimate);

// ── (a) PLAIN scrollToBottom path (the bug): single clamp against the
//    estimate, THEN the browser paints and the appended bottom bubbles resolve
//    to their real heights → scrollHeight grows below the fixed scrollTop. ──
seed();
container.scrollTop = container.scrollHeight;   // single-rAF clamp vs estimate
const plainClampedTop = _scrollTop;
_paint();                                       // async height resolution
const plainDist = container.scrollHeight - _scrollTop - CLIENT_H;
trace.plain_clamped_scrollTop = plainClampedTop;
trace.plain_distance_from_bottom = plainDist;
check('plain_lands_mid_history', plainDist > 1);

// ── (b) THE FIX: _forceScrollToBottom(null, true). cv-off resolves the new
//    bottom bubbles' real heights BEFORE the clamp, so scrollTop targets the
//    TRUE bottom. A post-scroll paint then changes nothing. ──
seed();
_forceScrollToBottom(null, true);
_paint();                                       // async resolution — no-op if fixed
const fixedDist = container.scrollHeight - container.scrollTop - CLIENT_H;
const fixedTop = container.scrollTop;
trace.true_bottom_scrollTop = trueBottomScrollTop();
trace.fixed_scrollTop = fixedTop;
trace.fixed_distance_from_bottom = fixedDist;
check('fixed_lands_at_true_bottom', Math.abs(fixedDist) <= 1);

console.log('TRACE ' + JSON.stringify(trace));
console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_send_scroll_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, neuter],
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
def test_send_scroll_lands_at_true_bottom():
    """With un-measured above-fold bubbles, the real-height path
    (_forceScrollToBottom cv-off dance) must land at the TRUE bottom, and the
    trace must show the estimate genuinely under-measures (else the scenario is
    not exercising the bug)."""
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'send-scroll failures:\n' + output
    # Print the trace for the record (visible with -s).
    for ln in output.splitlines():
        if ln.startswith('TRACE'):
            print(ln)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_cvoff_guard_is_load_bearing():
    """Neuter the cv-off guard inside _forceScrollToBottom (→ it measures
    against the 120px estimates like plain scrollToBottom). The
    land-at-true-bottom check MUST then fail, proving the guard is what fixes
    the mid-history landing."""
    lines = _lines(_run('cvoff'))
    assert lines.get('fixed_lands_at_true_bottom') == 'FAIL', lines
    # The scenario itself must still be valid (estimate diverges).
    assert lines.get('trace_diverges') == 'PASS', lines
    assert lines.get('plain_lands_mid_history') == 'PASS', lines

"""jsdom regression for the FULL-RENDER-path scroll anchoring.

WHY
---
`renderChat(conv, true)` (the full re-render path) rebuilds the message list
with `inner.innerHTML = html`, which hard-resets `scrollTop`→0. Recovery used to
depend ENTIRELY on `_forceScrollToBottom(container, true)` — so a background full
re-render (cost/file-change settle, endpoint poll, artifact hydrate, …) yanked a
reader who had scrolled UP into history straight to the bottom (or, when heights
mis-measured, stranded them at the top). This is the user-reported "the page
suddenly jumps" bug.

THE FIX (static/js/ui/chat_render.js)
-------------------------------------
When the full-render path runs for the SAME conversation whose DOM is already on
screen AND the reader is parked up in history (not near the bottom, no live
stream), capture their viewport anchor from the OLD DOM before the innerHTML
wipe (`_captureScrollAnchor`) and re-pin it afterwards (`_restoreScrollAnchor`) —
the exact anchor-relative technique already proven in `_bgRefreshChat`, now
extracted into shared helpers. A genuine conversation SWITCH, a first load, or a
near-bottom reader still lands at the bottom via `_forceScrollToBottom`.

jsdom does no layout, so this harness installs a DETERMINISTIC layout model
(a `getBoundingClientRect` override + a backed `scrollTop`/`scrollHeight`) in
which the tail message list is taller than the viewport. It parks the reader so
an above-fold message is the anchor, triggers a same-conv full re-render, and
asserts the anchored element keeps its viewport offset (a force-to-bottom would
move it far away).

DOUBLE-NEUTER:
  • 'anchor'  — break the re-pin arithmetic in `_restoreScrollAnchor`
                (→ raw scrollTop=0 survives) → the offset-preserved check fails
                while the capture-guard check (that force-scroll was NOT used)
                still passes.
  • 'capture' — force `_preSwapAnchor` to null (→ always force-scroll to bottom)
                → the force-scroll path IS taken (bottom reached) so the
                offset-preserved check fails; proves the capture guard is what
                diverts away from force-scroll.
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
win.activeConvId = global.activeConvId = 'fr-conv';

// ── Deterministic layout model ───────────────────────────────────────────
// Container viewport = [0, 600]. Each message is a fixed 200px tall block
// stacking vertically. With 6 messages the doc is 1200px, taller than the
// viewport, so there is real room to scroll and to be "parked up".
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

// Model the REAL browser behaviour that makes anchor-restore load-bearing: a
// full `inner.innerHTML = html` wipe hard-resets the scroll container's
// scrollTop to 0. jsdom does not do this, so without it the neuter (which turns
// the re-pin arithmetic into a no-op) would be inert (scrollTop never moved).
const _innerHtmlDesc = Object.getOwnPropertyDescriptor(win.Element.prototype, 'innerHTML');
Object.defineProperty(inner, 'innerHTML', {
  get() { return _innerHtmlDesc.get.call(this); },
  set(v) { _innerHtmlDesc.set.call(this, v); _scrollTop = 0; },  // browser resets scroll on wipe
  configurable: true,
});

// ── Stubs for the render dependencies renderChat touches. ──
win._applyAutopilotRunFolds = global._applyAutopilotRunFolds = () => {};
win._convRenderFingerprint = global._convRenderFingerprint = (c) => 'fp-' + (c && c.messages ? c.messages.length : 0);
win._stampFreshness = global._stampFreshness = () => {};
win._ensureLazyObserver = global._ensureLazyObserver = () => {};
win._destroyLazyObserver = global._destroyLazyObserver = () => {};
win.buildTurnNav = global.buildTurnNav = () => {};
win.isNearBottom = global.isNearBottom = (thr) => {
  // near bottom iff scrollTop within `thr` of max scroll
  const max = N * MSG_H - 600;
  return _scrollTop >= max - (thr || 0);
};
// _forceScrollToBottom: emulate the real "land at the bottom" effect so we can
// detect whether the code chose force-scroll vs anchor-restore.
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
// Module-level vars renderChat reads that are declared in OTHER bundle files
// (core.js). eval-scope `let`s inside chat_render.js (_lazyConvId,
// _lastRenderedFingerprint, _lazyRenderedFrom) resolve within the eval; these
// cross-file ones must be provided.
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
// _lazyConvId / _lazyRenderedFrom are `let`-declared in streaming_render.js and
// _lastRenderedFingerprint likewise; chat_render.js reads them as free vars.
// Predefine on global so the first read (before chat_render's bare assignment)
// doesn't throw ReferenceError in an isolated eval.
win._lazyConvId = global._lazyConvId = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = Infinity;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// Prefetch hooks return a resolved false so the .then repaint never fires.
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

// argv[5..] = ordered shipped sources (resolved BY SYMBOL from the production
// bundle manifests — see tests/_conv_bundle_sources.py), CONCATENATED into ONE
// eval exactly as the bundler concatenates them. That is load-bearing, not
// stylistic: `_explicitBottomLatch` is `let`-declared in ui/streaming_render.js
// and a `let` does NOT escape its own eval, so eval'ing the files separately
// still leaves chat_render.js with a bare ReferenceError that reads like a
// product bug but is pure harness drift.
let src = process.argv.slice(5).map((p) => fs.readFileSync(p, 'utf8')).join('\n;\n');

if (NEUTER === 'anchor') {
  src = src.replace('container.scrollTop += (newOffset - anchor.offset);  // re-pin the anchor',
                    'container.scrollTop += (0);  // NEUTERED-anchor');
  if (src.indexOf('// NEUTERED-anchor') < 0) { console.log('FAIL neuter_anchor_not_applied'); process.exit(0); }
}
if (NEUTER === 'capture') {
  // Force the capture to yield null → the full-render path always force-scrolls.
  // Anchored on the SINGLE stable line that performs the capture, not on the
  // whole multi-line condition: that condition has already grown a clause
  // (`_explicitBottomLatch !== conv.id`) since this neuter was written, and a
  // multi-line literal silently stops matching on any such edit — the neuter
  // then "does not bite" and reads exactly like a passing guard.
  const before = src;
  src = src.replace('    ? _captureScrollAnchor(container, inner)',
                    '    ? null  // NEUTERED-capture');
  if (src === before) { console.log('FAIL neuter_capture_not_applied'); process.exit(0); }
}

eval(src);

// Re-install the observation stubs the REAL sources just shadowed:
// ui/streaming_render.js declares `function _forceScrollToBottom(...)` /
// `function _ensureLazyObserver()`, and a hoisted function declaration in the
// eval overwrites the pre-eval assignment — the counting stub would never see
// a call, so every force-scroll assertion would read 0.
_forceScrollToBottom = win._forceScrollToBottom = global._forceScrollToBottom = (c) => {
  _forceScrollCalls++;
  _scrollTop = N * MSG_H - 600;   // bottom
};
_ensureLazyObserver = win._ensureLazyObserver = global._ensureLazyObserver = () => {};
_destroyLazyObserver = win._destroyLazyObserver = global._destroyLazyObserver = () => {};

// Override the hoisted renderMessage with a deterministic marker version.
renderMessage = win.renderMessage = global.renderMessage = (msg, idx) =>
  '<div class="message" id="msg-' + idx + '" data-mfp="v' + ((msg && msg.content) || '') + '">' +
  ((msg && msg.content) || '') + '</div>';

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _captureScrollAnchor !== 'function' || typeof _restoreScrollAnchor !== 'function') {
  console.log('FAIL helpers_exposed'); process.exit(0);
}
check('helpers_exposed', true);

const conv = { id: 'fr-conv', activeTaskId: null, messages: [] };
for (let i = 0; i < N; i++) conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i });

// Seed the DOM as if this conv is already rendered (same-conv full re-render).
// _lazyConvId must equal conv.id for the same-conv guard to fire — renderChat
// sets it, but the very first full render is a "switch". So do a first render
// (which force-scrolls, establishing _lazyConvId), THEN park the reader up and
// re-render.
_scrollTop = 0;
renderChat(conv, true);           // first paint (switch) → _lazyConvId = fr-conv
check('first_paint_has_dom', !!inner.querySelector('[id^="msg-"]'));

// Park the reader UP in history: show msg-1 near the top of the viewport.
_scrollTop = 200;                  // msg-1 top at viewport y=0
const anchorBefore = (function () {
  const cTop = 0;
  for (const el of inner.querySelectorAll('[id^="msg-"]')) {
    const r = el.getBoundingClientRect();
    if (r.bottom > cTop + 1) return { id: el.id, off: r.top - cTop };
  }
  return null;
})();
check('parked_up_not_near_bottom', win.isNearBottom(120) === false);
check('anchor_is_msg1', !!anchorBefore && anchorBefore.id === 'msg-1');

_forceScrollCalls = 0;
// Trigger a SAME-CONV full re-render (e.g. a background settle path).
renderChat(conv, true);

// The anchored element must keep its viewport offset (a force-scroll-to-bottom
// would move msg-1 far above the viewport top → large negative offset).
const anchorAfterEl = document.getElementById(anchorBefore.id);
const anchorAfterOff = anchorAfterEl.getBoundingClientRect().top - 0;
check('anchor_offset_preserved', Math.abs(anchorAfterOff - anchorBefore.off) <= 1);
// And the force-scroll path must NOT have been used on this same-conv re-render.
check('force_scroll_not_used', _forceScrollCalls === 0);

// Control: a near-bottom reader SHOULD still be taken to the bottom.
_scrollTop = N * MSG_H - 600;      // exactly bottom → isNearBottom true
_forceScrollCalls = 0;
renderChat(conv, true);
check('near_bottom_uses_force_scroll', _forceScrollCalls === 1);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    from tests._conv_bundle_sources import sources_defining
    deps = sources_defining('_explicitBottomLatch')
    target = os.path.join(JS_DIR, 'ui', 'chat_render.js')
    src_paths = [p for p in deps if p != target] + [target]

    harness = os.path.join(HERE, '_full_render_anchor_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             target,                                          # argv[2] (legacy)
             ROOT,                                            # argv[3]
             neuter,                                          # argv[4]
             *src_paths,                                      # argv[5..]
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
def test_full_render_preserves_reader_viewport():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'full-render anchor failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_full_render_anchor_arithmetic_is_load_bearing():
    """Neuter the re-pin arithmetic → the offset-preserved check fails while the
    capture-guard still diverts away from force-scroll (offset breaks, not the
    branch choice)."""
    lines = _lines(_run('anchor'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('force_scroll_not_used') == 'PASS', lines
    assert lines.get('near_bottom_uses_force_scroll') == 'PASS', lines


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_full_render_capture_guard_is_load_bearing():
    """Neuter the anchor CAPTURE (→ always null) → the code falls back to
    force-scroll-to-bottom on the same-conv re-render, so the offset is NOT
    preserved AND the force-scroll path IS taken. Proves the capture guard is
    what diverts a parked reader away from the yank-to-bottom."""
    lines = _lines(_run('capture'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('force_scroll_not_used') == 'FAIL', lines

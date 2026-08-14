"""Same-open renders must ANCHOR-HOLD a near-bottom reader (no jump-to-top).

WHY
---
A single conversation OPEN drives SEVERAL full renders — the Phase-1 IndexedDB
cache paint, the Phase-2 server-reconcile paint, and the loadConversation()
`.then()` fallback. Each full render wipes `inner.innerHTML` (scrollTop→0 in a
real browser). A reader who scrolled NEAR THE BOTTOM mid-open would be yanked
to the top by every one of those paints — the same "jumps around the page"
complaint family — unless something re-pins them.

That something is the `_openAlreadyPositioned` widening in chat_render.js's
anchor-capture guard:

    const _preSwapAnchor = (_sameConvDom && ...
        && (!_readerNearBottom || _openAlreadyPositioned))
        ? _captureScrollAnchor(container, inner) : null;

During an open (`conv._initialSwitchLoad` set, `_openScrollConvId === conv.id`
latched by the first paint), a same-open render with a NEAR-BOTTOM reader
captures the scroll anchor ONLY because the widening discharges the
`!_readerNearBottom` veto. `_restoreScrollAnchor` then re-pins the viewport
after the wipe. Without the widening the anchor is skipped, the render takes
the open no-scroll branch, and the wipe leaves the reader at the TOP.

SCOPE (disjoint guards, one contract)
-------------------------------------
• The open NEVER force-scrolls (owner directive: an open must not yank the
  view to the bottom) — pinned by tests/test_frontend_open_conv_no_autoscroll.py,
  including the no-scroll branch's own NC. This suite does NOT re-pin that.
• THIS suite pins the widening's modern, load-bearing role: anchor-restore
  for a near-bottom reader across same-open renders, and its per-open scoping
  (once the open completes, the widening disengages and a near-bottom
  BACKGROUND re-render force-scrolls again).

DRIFT HISTORY (why this file went red)
--------------------------------------
Two stacked layers, found 2026-08-03:
1. HARNESS DRIFT — the harness eval'd chat_render.js standalone, but the
   render decomposition moved `_explicitBottomLatch` (and the other latch
   lets) into ui/streaming_render.js → bare ReferenceError. Fixed by resolving
   the eval list BY SYMBOL (tests/_conv_bundle_sources.py) and concatenating
   into ONE eval (a `let` does not escape its own eval), with stubs/state
   installed through a `__H` bridge appended to the same source string — the
   exact pattern test_frontend_open_conv_no_autoscroll.py established.
2. CONTRACT DRIFT — the old scenario asserted render #1 of an open
   force-scrolls exactly once. The "No-auto-scroll-on-OPEN" owner directive
   (5286eada era) superseded that: open renders never force-scroll now, so
   the old scenario's premise (reader parked at the bottom BY render #1)
   cannot happen, and its NC could never bite. The scenario is rebuilt around
   the widening's actual modern effect: the reader scrolls near the bottom
   mid-open BY HAND.

NEUTER: remove the `_openAlreadyPositioned` widening → render #2 (near-bottom
reader, same open) captures no anchor and the wipe leaves scrollTop=0 → the
anchor-restored assertion fails while render #1 checks stay green. Proves the
widening is load-bearing. Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


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
win._applyAutopilotRunNotices = global._applyAutopilotRunNotices = () => {};
win._convRenderFingerprint = global._convRenderFingerprint = (c) => {
  if (!c || !c.messages || !c.messages.length) return 'fp-0';
  const last = c.messages[c.messages.length - 1];
  return 'fp-' + c.messages.length + ':' + ((last && last.content) || '');
};
win._stampFreshness = global._stampFreshness = () => {};
win.buildTurnNav = global.buildTurnNav = () => {};
win.isNearBottom = global.isNearBottom = (thr) => {
  const max = N * MSG_H - 600;
  return _scrollTop >= max - (thr || 0);
};
let _forceScrollCalls = 0;
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
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
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

// argv[5..] = ordered shipped sources (resolved by symbol, see
// tests/_conv_bundle_sources.py). They are CONCATENATED into ONE eval, which
// is exactly what the bundler does — and is load-bearing here, not stylistic:
// `_explicitBottomLatch` is `let`-declared in ui/streaming_render.js, and a
// `let` does NOT escape its own eval, so eval'ing the dependency separately
// still leaves chat_render.js with a bare ReferenceError. One eval gives the
// files the single shared scope the concatenated bundle gives them. The
// NEUTER rewrite below applies to the joined text.
const SRC_PATHS = process.argv.slice(5);
let src = SRC_PATHS.map((p) => fs.readFileSync(p, 'utf8')).join('\n;\n');

if (NEUTER === 'widen') {
  // Remove the open-already-positioned widening → a same-open render with a
  // near-bottom reader captures no anchor → the wipe strands them at the top.
  const before = src;
  src = src.replace('      && (!_readerNearBottom || _openAlreadyPositioned))',
                    '      && (!_readerNearBottom))');
  if (src === before) { console.log('FAIL neuter_widen_not_applied'); process.exit(0); }
}

// ui/streaming_render.js `let`-declares _openScrollConvId / _lazyConvId /
// _lazyRenderedFrom, and declares `function _forceScrollToBottom(...)` /
// `_ensureLazyObserver()`. Those bindings live in the EVAL's own scope, which a
// SEPARATE eval cannot see and which a post-eval bare assignment does not
// reach (measured in this very harness: pre-eval global stubs stayed dead —
// the product latched the lexical binding while the assertion read the global
// and saw null). So the state accessors and stub installers are APPENDED TO
// THE SAME SOURCE STRING, closing over exactly the bindings renderChat reads
// and writes — the pattern test_frontend_open_conv_no_autoscroll.py set.
src += `
;
globalThis.__H = {
  get openScroll(){ return _openScrollConvId; },
  set openScroll(v){ _openScrollConvId = v; },
  set lazyConv(v){ _lazyConvId = v; },
  set lazyFrom(v){ _lazyRenderedFrom = v; },
  installStubs(force, noop){ _forceScrollToBottom = force; _ensureLazyObserver = noop; _destroyLazyObserver = noop; },
  setRenderMessage(fn){ renderMessage = fn; },
};
`;

eval(src);

const S = globalThis.__H;
S.installStubs(
  (c) => { _forceScrollCalls++; _scrollTop = N * MSG_H - 600; },
  () => {},
);
S.lazyFrom = Infinity;
S.setRenderMessage((msg, idx) =>
  '<div class="message" id="msg-' + idx + '" data-mfp="v' + ((msg && msg.content) || '') + '">' +
  ((msg && msg.content) || '') + '</div>');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const conv = { id: 'oc-conv', activeTaskId: null, messages: [] };
for (let i = 0; i < N; i++) conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i });

// ── Simulate an OPEN: loadConversation resets the latch, then the multi-phase
//    renders fire while _initialSwitchLoad is set. ──
S.openScroll = null;               // loadConversation reset
conv._initialSwitchLoad = true;
S.lazyConv = null;                 // a genuine SWITCH (prev conv was different)

// Render #1 — Phase-1 cache paint (switch first-paint). The no-autoscroll
// contract (pinned by test_frontend_open_conv_no_autoscroll.py): no
// force-scroll, stays at the top, latches this open. Setup sanity for the
// scenario below — the widening means nothing until the latch exists.
_scrollTop = 0;
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render1_no_force_scroll', _forceScrollCalls === 0);
check('open_render1_stays_at_top', _scrollTop === 0);
check('open_render1_latched', S.openScroll === 'oc-conv');

// The reader scrolls NEAR THE BOTTOM mid-open (the first paint let them read
// on while the server reconcile is still in flight).
_scrollTop = N * MSG_H - 600;

// Render #2 — Phase-2 server reconcile, SAME open (_initialSwitchLoad still
// set). The full-render wipe resets scrollTop→0; ONLY the widening's anchor
// capture re-pins the reader near the bottom. Vary the tail so the
// fingerprint guard does NOT short-circuit — Phase-1 (stale cache) and
// Phase-2 (server) legitimately differ; that's why two full renders run.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-server-reconciled' };
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render2_anchor_restored', Math.abs(_scrollTop - (N * MSG_H - 600)) <= 1);
check('open_render2_no_force_scroll', _forceScrollCalls === 0);

// Render #3 — the loadConversation .then() fallback, same open, reader still
// near the bottom after the restore. Same anchor-hold.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-final' };
renderChat(conv, false);
check('open_render3_anchor_restored', Math.abs(_scrollTop - (N * MSG_H - 600)) <= 1);

// ── The widening DISENGAGES when the open completes: a near-bottom BACKGROUND
//    re-render (settle/poll following new content, NOT an open — the
//    forceScroll=true full-render path; the surgical forceScroll===false path
//    preserves scroll on its own and is out of scope) force-scrolls again.
//    Proves the suppression is scoped to ONE open, not permanent. ──
delete conv._initialSwitchLoad;
S.openScroll = null;
_scrollTop = N * MSG_H - 600;      // reader parked exactly at the bottom
conv.messages.push({ role: 'assistant', content: 'm6-settled-later' });
_forceScrollCalls = 0;
renderChat(conv, true);
check('bg_after_open_force_scrolls', _forceScrollCalls === 1);

// ── A genuinely FRESH open (clicked AWAY to another conv, then BACK): the
//    latch re-arms per open — no force-scroll, latched again. ──
const other = { id: 'other-conv', activeTaskId: null, messages: [
  { role: 'user', content: 'x0' }, { role: 'assistant', content: 'x1' },
] };
S.openScroll = null;
other._initialSwitchLoad = true;
global.activeConvId = win.activeConvId = 'other-conv';
renderChat(other, false);          // renders other conv → _lazyConvId = other-conv
delete other._initialSwitchLoad;

global.activeConvId = win.activeConvId = 'oc-conv';
S.openScroll = null;               // loadConversation reset for the new open
conv._initialSwitchLoad = true;
_forceScrollCalls = 0;
renderChat(conv, false);
check('fresh_open_no_force_scroll', _forceScrollCalls === 0);
check('fresh_open_relatched', S.openScroll === 'oc-conv');

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    # Resolve the files to eval BY SYMBOL from the production bundle manifests
    # (never a hard-coded path — see tests/_conv_bundle_sources.py). chat_render
    # reads `_explicitBottomLatch`, which lives in ui/streaming_render.js since
    # the render decomposition; eval'ing chat_render alone gave a bare
    # ReferenceError that reads like a product bug but is pure harness drift.
    from tests._conv_bundle_sources import sources_defining
    deps = sources_defining('_explicitBottomLatch')
    target = os.path.join(JS_DIR, 'ui', 'chat_render.js')
    src_paths = [p for p in deps if p != target] + [target]

    harness = os.path.join(HERE, '_open_scroll_once_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             target,                                          # argv[2] (legacy, unused)
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
def test_open_positions_view_exactly_once():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'open-scroll-once failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_open_scroll_widening_is_load_bearing():
    """Remove the _openAlreadyPositioned widening → render #2 of the same open
    (reader near the bottom) captures no anchor and the wipe strands them at
    the top. Proves the widening is what holds the position."""
    lines = _lines(_run('widen'))
    assert lines.get('open_render1_no_force_scroll') == 'PASS', lines
    assert lines.get('open_render1_latched') == 'PASS', lines
    assert lines.get('open_render2_anchor_restored') == 'FAIL', lines

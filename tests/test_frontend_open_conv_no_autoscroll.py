"""Opening a conversation must NOT auto-scroll (owner directive — option A).

WHY
---
Owner directive: "when I click a conversation without manually scrolling, the
scroll position should stay put — no jumping around." The previous design
force-scrolled to the BOTTOM once per open (open-scroll coalescing). That is
itself a jump. Under the new contract a conversation OPEN positions the view at
the natural post-render location (the top of the freshly rebuilt tail window,
where the `inner.innerHTML` wipe leaves `scrollTop`) and NEVER calls
`_forceScrollToBottom`.

A single open still drives SEVERAL full renders — the Phase-1 IndexedDB cache
paint, the Phase-2 server-reconcile paint, and the loadConversation `.then()`
fallback. NONE of them may force-scroll:
  • render #1 (switch first-paint / `_initialSwitchLoad`): the new
    `!_sameConvDom || conv._initialSwitchLoad` branch takes the no-scroll path
    and latches `_openScrollConvId`;
  • render #2/#3 (SAME open, `_initialSwitchLoad` still set): `_openAlreadyPositioned`
    widens the anchor-capture guard so they re-pin the current position instead
    of snapping.

The streaming-follow path (showStreamingUIForConv / the send pipeline) and the
turn-nav dot clicks are DELIBERATELY untouched — those are user-driven /
live-turn scrolls and still land at the bottom.

THE FIX (static/js/ui/chat_render.js)
-------------------------------------
In the full-render else-branch, an OPEN (`!_sameConvDom` genuine switch, first
load, or any render while `_initialSwitchLoad` is set) takes a no-scroll branch;
only a same-conv near-bottom BACKGROUND re-render (settle/poll following new
content, NOT an open) still `_forceScrollToBottom`.

This harness installs a deterministic layout model and asserts:
  1. render #1 of an open (switch) does NOT force-scroll and stays at the top;
  2. render #2 of the SAME open (Phase-2 reconcile) does NOT force-scroll;
  3. render #3 (.then fallback) does NOT force-scroll;
  4. a same-conv NEAR-BOTTOM background re-render (NOT an open) DOES still
     force-scroll — proving the change is scoped to opens, not a blanket
     "never scroll".

NEUTER ('open'): collapse the open no-scroll branch condition to `false` → an
open falls through to the force-scroll ELSE branch → render #1 force-scrolls
again → assertion (1) fails. Proves the open-branch is load-bearing.
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

// argv[5..] = ordered shipped sources (resolved BY SYMBOL from the production
// bundle manifests — see tests/_conv_bundle_sources.py), CONCATENATED into ONE
// eval exactly as the bundler concatenates them. Load-bearing:
// `_explicitBottomLatch` is `let`-declared in ui/streaming_render.js and a
// `let` does NOT escape its own eval, so separate evals still leave
// chat_render.js with a bare ReferenceError.
let src = process.argv.slice(5).map((p) => fs.readFileSync(p, 'utf8')).join('\n;\n');

if (NEUTER === 'open') {
  // Collapse the OPEN no-scroll branch → an open falls through to the
  // force-scroll ELSE branch and re-snaps to the bottom.
  const before = src;
  src = src.replace('} else if (!_sameConvDom || conv._initialSwitchLoad) {',
                    '} else if (false) {  // NEUTERED-open');
  if (src === before) { console.log('FAIL neuter_open_not_applied'); process.exit(0); }
}

// ui/streaming_render.js `let`-declares _openScrollConvId / _lazyConvId /
// _lazyRenderedFrom, and declares `function _forceScrollToBottom(...)` /
// `_ensureLazyObserver()`. Those bindings live in the EVAL's own scope, which a
// SEPARATE eval cannot see (measured: a second eval reading `_lazyConvId`
// throws ReferenceError) and which a post-eval bare assignment does not reach.
// So the stubs and state accessors are APPENDED TO THE SAME SOURCE STRING,
// closing over exactly the bindings renderChat reads and writes. Getting this
// wrong is silent: the harness seeds one variable, the product latches another,
// and the latch assertion reads a stale value while the DOM renders fine.
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

// Render #1 — Phase-1 cache paint (switch first-paint). Must NOT force-scroll.
_scrollTop = 0;
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render1_no_force_scroll', _forceScrollCalls === 0);
check('open_render1_stays_at_top', _scrollTop === 0);
check('open_render1_latched', S.openScroll === 'oc-conv');

// Render #2 — Phase-2 server reconcile paint, SAME open (_initialSwitchLoad
// still set). Vary the tail so the fingerprint guard doesn't short-circuit.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-server-reconciled' };
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render2_no_force_scroll', _forceScrollCalls === 0);
check('open_render2_stays_at_top', _scrollTop === 0);

// Render #3 — the loadConversation .then() fallback would also be same-open.
conv.messages[N - 1] = { role: 'assistant', content: 'm5-final' };
_forceScrollCalls = 0;
renderChat(conv, false);
check('open_render3_no_force_scroll', _forceScrollCalls === 0);

// ── Scoping control: a same-conv NEAR-BOTTOM BACKGROUND full re-render (NOT an
//    open — _initialSwitchLoad cleared, reader sitting at the bottom while new
//    content settles, e.g. the autopilot/disarm `renderChat(conv, true)` path)
//    MUST still force-scroll so the newest content stays in view. Proves the
//    change is scoped to opens, not a blanket no-scroll. We use the
//    forceScroll=true full-render path (the surgical forceScroll===false path
//    preserves scroll on its own and is out of scope here). ──
delete conv._initialSwitchLoad;
S.openScroll = null;
_scrollTop = N * MSG_H - 600;      // reader parked exactly at the bottom
conv.messages.push({ role: 'assistant', content: 'm6-settled-later' });
_forceScrollCalls = 0;
renderChat(conv, true);
check('bg_near_bottom_still_force_scrolls', _forceScrollCalls === 1);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    from tests._conv_bundle_sources import sources_defining
    deps = sources_defining('_explicitBottomLatch')
    target = os.path.join(JS_DIR, 'ui', 'chat_render.js')
    src_paths = [p for p in deps if p != target] + [target]

    harness = os.path.join(HERE, '_open_no_autoscroll_harness.js')
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
def test_open_never_auto_scrolls():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'open-no-autoscroll failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_open_no_scroll_branch_is_load_bearing():
    """Collapse the open no-scroll branch → an open re-snaps to the bottom
    (force-scroll fires on render #1). Proves the branch is what suppresses the
    jump-to-bottom on open, while the scoping control still force-scrolls."""
    lines = _lines(_run('open'))
    assert lines.get('open_render1_no_force_scroll') == 'FAIL', lines
    assert lines.get('bg_near_bottom_still_force_scrolls') == 'PASS', lines

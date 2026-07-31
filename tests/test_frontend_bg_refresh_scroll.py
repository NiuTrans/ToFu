"""jsdom regression for the background-refresh scroll hardening.

WHY
---
Cost + file-change batch prefetches land data that used to be invisible to the
per-message content version, so `renderChat`'s surgical diff would skip the
repaint. The old workaround was a SEPARATE second DOM-owner, `_bgRefreshChat`,
that re-rendered assistant bubbles by comparing rendered OUTPUT (`__bgHtml`).

RENDER_CONTRACT Phase 2b RETIRED that parallel path: the content version now
folds cost/modifiedFileList/_fcResolvedFp/_artifacts/_compactions, so the ONE
surgical `renderChat(conv,false)` path repaints them, and `_bgRefreshChat` is
now a thin SHIM that (a) clears the Guard-2 fingerprint (which samples only the
last message) so a mid-history data land isn't skipped, and (b) sets
`conv._bgRepaint` so the surgical path runs even during `_initialSwitchLoad`
AND wraps its swaps in the scroll anchor as a FIXED STEP.

So this suite now drives the REAL shipped `renderChat` (not a stub) through the
`_bgRefreshChat` shim and pins the two invariants ON the unified path:

  (1) ANCHOR-RELATIVE RESTORE — pin the topmost message intersecting the
      viewport at its pre-repaint offset, so the reader's viewport is preserved
      even when above-fold bubbles change height (the surgical path's fixed
      anchor step).
  (2) ID-KEYED REUSE — the surgical diff repaints a row whose content version
      (data-mfp) changed and REUSES the DOM node of an unchanged row (Phase 1),
      preserving any manually-expanded tool-round `<details>` state.

jsdom does no layout, so this harness installs a DETERMINISTIC layout model
(a `getBoundingClientRect` override + a backed `scrollTop`) in which assistant
bubbles grow from `short`→`tall` the moment their data-mfp changes (the stub
renderMessage stamps a per-content mfp + data-repainted). It asserts the
anchored element's viewport offset survives an above-fold growth, and that a
row whose mfp is unchanged keeps its exact DOM node across a second refresh.

DOUBLE-NEUTER: one neuter per invariant, each flipping ONLY its own checks.
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
global.setTimeout = win.setTimeout = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'bg-conv';

// ── Deterministic layout model ───────────────────────────────────────────
// Container viewport top is fixed at 0. Each message stacks vertically; an
// assistant bubble is `short` until repainted (data-repainted="1"), then `tall`
// (simulating the cost/finish bar arriving). User bubbles are fixed height.
const MSG = {
  0: { role: 'user',      h: 200 },
  1: { role: 'assistant', short: 100, tall: 130 },
  2: { role: 'user',      h: 200 },
  3: { role: 'assistant', short: 100, tall: 130 },
};
let _scrollTop = 0;
function _msgHeight(idx) {
  const info = MSG[idx];
  if (!info) return 0;
  if (info.role !== 'assistant') return info.h;
  const node = document.getElementById('msg-' + idx);
  const tall = node && node.getAttribute && node.getAttribute('data-repainted') === '1';
  return tall ? info.tall : info.short;
}
function _docTop(idx) { let t = 0; for (let i = 0; i < idx; i++) t += _msgHeight(i); return t; }
win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: 800, left: 0, right: 0, width: 0, height: 800 };
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) {
    const idx = parseInt(m[1], 10);
    const top = _docTop(idx) - _scrollTop;
    const h = _msgHeight(idx);
    return { top, bottom: top + h, left: 0, right: 0, width: 0, height: h };
  }
  return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
};
const container = document.getElementById('chatContainer');
const inner = document.getElementById('chatInner');
Object.defineProperty(container, 'scrollTop', { get: () => _scrollTop, set: (v) => { _scrollTop = v; }, configurable: true });
Object.defineProperty(container, 'scrollHeight', {
  get: () => { let t = 0; for (const k of Object.keys(MSG)) t += _msgHeight(parseInt(k, 10)); return t; },
  configurable: true,
});

win._applyAutopilotRunFolds = global._applyAutopilotRunFolds = () => {};
const _noop = () => '';
for (const name of [
  'renderMarkdown','safeHtml','raw','renderToolRoundsHTML','getToolRoundsFromMsg',
  'renderFinishInfo','renderMcpLoginHintHtml','renderTurnProvenanceHtml',
  'renderPreferenceLearnedHtml','_buildSwarmInboxChipsHTML','renderTurnCtxNote',
  '_injectAnchoredBranches','stripNoTranslateTags','buildTurnNav','_forceScrollToBottom',
  '_stampFreshness','scrollToBottom',
  'isNearBottom','showStreamingUIForConv','_ensureLazyObserver','_destroyLazyObserver',
  'ConvCache','saveConversations','_buildConvConfig',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
// renderChat awaits these two via .then() — return an inert thenable so the
// callback never fires (we drive the repaint directly through _bgRefreshChat).
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
// _msgFingerprint calls translationFingerprint (defined in translation_model.js,
// not loaded standalone here). Stub it so the REAL _msgFingerprint runs — the
// stub renderMessage below stamps data-mfp FROM the real _msgFingerprint so the
// surgical diff's oldFp/newFp comparison is self-consistent.
win.translationFingerprint = global.translationFingerprint = () => '0:F:';
// Free vars renderChat reads at call time (declared with let in core.js; here
// they resolve to the global scope).
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._lazyConvId = global._lazyConvId = 'bg-conv';   // same conv already in DOM
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// The REAL _convRenderFingerprint is in core.js; the shim clears the cached
// value so Guard 2 never short-circuits — give a stable value so the ONLY thing
// deciding a repaint is the per-message data-mfp diff.
win._convRenderFingerprint = global._convRenderFingerprint = () => 'CONVFP';

// argv[5..] = ordered shipped sources (resolved by symbol, see
// tests/_conv_bundle_sources.py). They are CONCATENATED into ONE eval, which
// is exactly what the bundler does — and is load-bearing here, not stylistic:
// `_explicitBottomLatch` is `let`-declared in ui/streaming_render.js, and a
// `let` does NOT escape its own eval, so eval'ing the dependency separately
// still leaves chat_render.js with a bare ReferenceError. One eval gives the
// two files the single shared scope the concatenated bundle gives them.
// The NEUTER rewrite is applied to the joined text.
const SRC_PATHS = process.argv.slice(5);
let src = SRC_PATHS.map((p) => fs.readFileSync(p, 'utf8')).join('\n;\n');

// ── NEUTER injection (per-invariant double-neuter) ─────────────────────────
if (NEUTER === 'anchor') {
  // Break the anchor compensation → behaves like a raw scrollTop restore.
  // The re-pin arithmetic lives in `_restoreScrollAnchor`, now a FIXED STEP of
  // the surgical path (gated on _bgRepaint).
  src = src.replace('container.scrollTop += (newOffset - anchor.offset);  // re-pin the anchor',
                    'container.scrollTop += (0);  // NEUTERED-anchor');
  if (src.indexOf('// NEUTERED-anchor') < 0) { console.log('FAIL neuter_anchor_not_applied'); process.exit(0); }
}
if (NEUTER === 'reuse') {
  // Break id-keyed node reuse → force a destroy+rebuild of every drifted node
  // by repainting unconditionally (never take the "unchanged → reuse" branch).
  src = src.replace('if (oldFp !== newFp) {', 'if (true) {  // NEUTERED-reuse');
  if (src.indexOf('// NEUTERED-reuse') < 0) { console.log('FAIL neuter_reuse_not_applied'); process.exit(0); }
}

eval(src);

// Override renderMessage with a marker version that stamps BOTH a stable
// data-msg-id (for id-keyed reconcile) AND a CONTENT-DERIVED data-mfp so the
// surgical diff sees a change when content changes. Assistant bubbles carry
// data-repainted="1" → grow short→tall in the layout model.
let _renderCalls = [];
renderMessage = win.renderMessage = global.renderMessage = (msg, idx) => {
  _renderCalls.push(idx);
  const role = (msg && msg.role) || 'assistant';
  const mid = (msg && msg._msgId) || ('m' + idx);
  // Stamp data-mfp from the REAL _msgFingerprint so the surgical diff's
  // oldFp/newFp comparison is self-consistent with what the shipped code computes.
  const mfp = _msgFingerprint(msg);
  return '<div class="message' + (role === 'assistant' ? '' : ' user-msg') +
    '" id="msg-' + idx + '" data-msg-id="' + mid + '" data-mfp="' + escapeHtml(mfp) +
    '" data-repainted="1">' + ((msg && msg.content) || '') + '</div>';
};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _bgRefreshChat !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
check('fn_exposed', true);

const conv = {
  id: 'bg-conv', activeTaskId: null,
  messages: [
    { role: 'user', _msgId: 'm0', content: 'q1' },
    { role: 'assistant', _msgId: 'm1', content: 'a1' },
    { role: 'user', _msgId: 'm2', content: 'q2' },
    { role: 'assistant', _msgId: 'm3', content: 'a2' },
  ],
};
win.getActiveConv = global.getActiveConv = () => conv;

// Seed the DOM with a STALE data-mfp for the assistant bubbles (v1) so the
// first _bgRefreshChat's surgical diff sees a change and repaints them (→ grow
// short→tall). User bubbles are seeded with their CURRENT mfp so they are
// unchanged → reused (never re-rendered). data-repainted starts absent (short).
function seedDom() {
  // User bubbles seeded with their CURRENT (real) fingerprint → unchanged →
  // reused, never re-rendered. Assistant bubbles seeded with a STALE mfp →
  // the surgical diff sees a change → repaints them (→ grow short→tall).
  const u0 = escapeHtml(_msgFingerprint(conv.messages[0]));
  const u2 = escapeHtml(_msgFingerprint(conv.messages[2]));
  inner.innerHTML =
    '<div class="message user-msg" id="msg-0" data-msg-id="m0" data-mfp="' + u0 + '" data-orig="U0">q1</div>' +
    '<div class="message" id="msg-1" data-msg-id="m1" data-mfp="STALE1">a1</div>' +
    '<div class="message user-msg" id="msg-2" data-msg-id="m2" data-mfp="' + u2 + '" data-orig="U2">q2</div>' +
    '<div class="message" id="msg-3" data-msg-id="m3" data-mfp="STALE3">a2</div>';
}
seedDom();

// Reader parked so msg-3 is the topmost bubble intersecting the viewport, with
// an above-fold assistant bubble (msg-1) that WILL grow when the bars land.
_scrollTop = 510;

function findAnchor() {
  const cTop = container.getBoundingClientRect().top;
  const els = inner.querySelectorAll('[id^="msg-"]');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.bottom > cTop + 1) return { id: el.id, off: r.top - cTop };
  }
  return null;
}
const preAnchor = findAnchor();
const preScroll = _scrollTop;

_renderCalls = [];
_bgRefreshChat(conv);   // FIRST refresh: stale-mfp assistant bubbles repaint → grow

// Repaint scope: assistant bubbles (stale mfp) repainted; user bubbles
// (current mfp) reused, never re-rendered.
const repainted = new Set(_renderCalls);
check('assistant1_repainted', repainted.has(1));
check('assistant3_repainted', repainted.has(3));
check('user0_not_repainted', !repainted.has(0));
check('user2_not_repainted', !repainted.has(2));
check('user0_dom_intact', (document.getElementById('msg-0') || {}).getAttribute
      && document.getElementById('msg-0').getAttribute('data-orig') === 'U0');
check('user2_dom_intact', (document.getElementById('msg-2') || {}).getAttribute
      && document.getElementById('msg-2').getAttribute('data-orig') === 'U2');

// (1) ANCHOR-RELATIVE RESTORE: the anchored element's viewport offset is
// preserved despite the above-fold growth (raw restore would drift it down).
const postTop = document.getElementById(preAnchor.id).getBoundingClientRect().top;
check('anchor_offset_preserved', Math.abs(postTop - preAnchor.off) <= 1);
check('scroll_actively_adjusted', _scrollTop !== preScroll);

// (2) ID-KEYED REUSE: a row whose mfp is unchanged on a SECOND refresh keeps
// its exact DOM node (→ expanded state survives). After the first refresh msg-1
// now carries its CURRENT mfp; tag it, refresh again → it must be reused.
const ref1 = document.getElementById('msg-1');
ref1.__keepMarker = 'EXPANDED';
_renderCalls = [];
_bgRefreshChat(conv);   // SECOND refresh: nothing changed → no swaps
const ref2 = document.getElementById('msg-1');
check('node_identity_retained', ref1 === ref2 && ref2.__keepMarker === 'EXPANDED');

// No-op safety: empty inner (welcome/skeleton) → no throw, no scroll write.
inner.innerHTML = '';
_scrollTop = 333;
_bgRefreshChat(conv);
check('empty_inner_noop', _scrollTop === 333);

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

    harness = os.path.join(HERE, '_bg_refresh_harness.js')
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
def test_bg_refresh_anchor_and_compare():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'bg-refresh failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_anchor_restore_is_load_bearing():
    """Neuter the anchor compensation (→ raw scrollTop) → the anchor-offset
    checks MUST fail while id-keyed reuse still passes (specificity)."""
    lines = _lines(_run('anchor'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('scroll_actively_adjusted') == 'FAIL', lines
    assert lines.get('node_identity_retained') == 'PASS', lines
    assert lines.get('assistant1_repainted') == 'PASS', lines


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_id_keyed_reuse_is_load_bearing():
    """Neuter the surgical diff's "unchanged → reuse" branch (force a rebuild of
    every drifted node) → the node-identity check MUST fail while the anchor
    checks still pass (specificity)."""
    lines = _lines(_run('reuse'))
    assert lines.get('node_identity_retained') == 'FAIL', lines
    assert lines.get('anchor_offset_preserved') == 'PASS', lines
    assert lines.get('scroll_actively_adjusted') == 'PASS', lines

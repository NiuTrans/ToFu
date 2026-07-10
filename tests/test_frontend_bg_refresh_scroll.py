"""jsdom regression for the background-refresh scroll hardening.

WHY
---
Cost + file-change batch prefetches land data that `_msgFingerprint` does NOT
track, so `renderChat`'s surgical diff would skip the repaint. The old
workaround `renderChat(conv, true)` force-scrolled to the bottom. The first fix
(`_bgRefreshChat`) repainted assistant bubbles in place and restored the RAW
`scrollTop` — but that only holds when heights ABOVE the fold are static. In the
actual bug scenario they are NOT: on first open the cost/finish bars aren't
fetched yet, so above-fold assistant bubbles render SHORT and GROW ~1s later
when the batch lands, and a raw-pixel restore then drifts the reader downward.

`_bgRefreshChat` (static/js/ui/chat_render.js) now upholds two invariants:

  (1) ANCHOR-RELATIVE RESTORE — pin the topmost message intersecting the
      viewport at its pre-repaint offset, so the reader's viewport is preserved
      even when above-fold bubbles change height.
  (2) COMPARE-BEFORE-SWAP — only `outerHTML`-replace a bubble whose rendered
      HTML actually changed; unchanged bubbles keep their DOM node (and any
      manually-expanded tool-round `<details>` state).

jsdom does no layout, so this harness installs a DETERMINISTIC layout model
(a `getBoundingClientRect` override + a backed `scrollTop`) in which assistant
bubbles grow from `short`→`tall` the moment they are repainted (detected via the
`data-repainted` marker the stub `renderMessage` stamps). It then asserts the
anchored element's viewport offset survives an above-fold growth, and that an
unchanged bubble's DOM node is reused across a second refresh.

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
win._convRenderFingerprint = global._convRenderFingerprint = () => 'fp';
const _noop = () => '';
for (const name of [
  'renderMarkdown','safeHtml','raw','renderToolRoundsHTML','getToolRoundsFromMsg',
  'renderFinishInfo','renderMcpLoginHintHtml','renderTurnProvenanceHtml',
  'renderPreferenceLearnedHtml','_buildSwarmInboxChipsHTML','renderTurnCtxNote',
  '_injectAnchoredBranches','stripNoTranslateTags','buildTurnNav','_forceScrollToBottom',
  '_prefetchConvCosts','_prefetchConvFileChanges','_stampFreshness','scrollToBottom',
  'isNearBottom','showStreamingUIForConv','_ensureLazyObserver','_destroyLazyObserver',
  'ConvCache','saveConversations','_buildConvConfig','renderChat',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

let src = fs.readFileSync(process.argv[2], 'utf8');  // ui/chat_render.js

// ── NEUTER injection (per-invariant double-neuter) ─────────────────────────
if (NEUTER === 'anchor') {
  // Break the anchor compensation → behaves like a raw scrollTop restore.
  // The re-pin arithmetic now lives in the extracted `_restoreScrollAnchor`
  // helper (shared by _bgRefreshChat + the full-render path).
  src = src.replace('container.scrollTop += (newOffset - anchor.offset);  // re-pin the anchor',
                    'container.scrollTop += (0);  // NEUTERED-anchor');
  if (src.indexOf('// NEUTERED-anchor') < 0) { console.log('FAIL neuter_anchor_not_applied'); process.exit(0); }
}
if (NEUTER === 'compare') {
  // Break compare-before-swap → every assistant bubble always re-created.
  src = src.replace('if (el.__bgHtml === fresh) return;  // unchanged → keep DOM (expand state intact)',
                    'if (false) return;  // NEUTERED-compare');
  if (src.indexOf('// NEUTERED-compare') < 0) { console.log('FAIL neuter_compare_not_applied'); process.exit(0); }
}

eval(src);

// Override the real renderMessage (hoisted decl shadows a pre-eval stub) with a
// marker version: assistant bubbles carry data-repainted="1" (→ grow to tall).
let _renderCalls = [];
renderMessage = win.renderMessage = global.renderMessage = (msg, idx) => {
  _renderCalls.push(idx);
  const role = (msg && msg.role) || 'assistant';
  return '<div class="message' + (role === 'assistant' ? '' : ' user-msg') +
    '" id="msg-' + idx + '" data-mfp="v2" data-repainted="1">' +
    ((msg && msg.content) || '') + '</div>';
};

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _bgRefreshChat !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
check('fn_exposed', true);

const conv = {
  id: 'bg-conv', activeTaskId: null,
  messages: [
    { role: 'user', content: 'q1' },
    { role: 'assistant', content: 'a1' },
    { role: 'user', content: 'q2' },
    { role: 'assistant', content: 'a2' },
  ],
};
win.getActiveConv = global.getActiveConv = () => conv;

function seedDom() {
  inner.innerHTML =
    '<div class="message user-msg" id="msg-0" data-mfp="v1" data-orig="U0">q1</div>' +
    '<div class="message" id="msg-1" data-mfp="v1">a1</div>' +
    '<div class="message user-msg" id="msg-2" data-mfp="v1" data-orig="U2">q2</div>' +
    '<div class="message" id="msg-3" data-mfp="v1">a2</div>';
}
seedDom();

// Reader parked so msg-3 is the topmost bubble intersecting the viewport, with
// an above-fold assistant bubble (msg-1) that WILL grow when the bars land.
_scrollTop = 510;

// Record the anchor + its viewport offset the same way the code does.
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
_bgRefreshChat(conv);   // FIRST refresh: all assistant bubbles grow short→tall

// (2)-repaint scope: assistant bubbles repainted; user bubbles never rendered.
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
// And scrollTop was ACTIVELY adjusted to compensate (raw restore would not move).
check('scroll_actively_adjusted', _scrollTop !== preScroll);

// (2) COMPARE-BEFORE-SWAP: a bubble whose HTML is unchanged on a SECOND refresh
// keeps its exact DOM node (→ expanded state survives). Tag msg-1's node and a
// simulated expanded <details>, then refresh again with identical content.
const ref1 = document.getElementById('msg-1');
ref1.__keepMarker = 'EXPANDED';
_renderCalls = [];
_bgRefreshChat(conv);   // SECOND refresh: nothing changed → no swaps
const ref2 = document.getElementById('msg-1');
check('node_identity_retained', ref1 === ref2 && ref2.__keepMarker === 'EXPANDED');

// No-op safety: empty inner (welcome/skeleton) → no throw, no scroll write.
seedDom();
inner.innerHTML = '';
_scrollTop = 333;
_bgRefreshChat(conv);
check('empty_inner_noop', _scrollTop === 333);

console.log(out.join('\n'));
"""


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_bg_refresh_harness.js')
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
def test_bg_refresh_anchor_and_compare():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'bg-refresh failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_anchor_restore_is_load_bearing():
    """Neuter the anchor compensation (→ raw scrollTop) → the anchor-offset
    checks MUST fail while compare-before-swap still passes (specificity)."""
    lines = _lines(_run('anchor'))
    assert lines.get('anchor_offset_preserved') == 'FAIL', lines
    assert lines.get('scroll_actively_adjusted') == 'FAIL', lines
    assert lines.get('node_identity_retained') == 'PASS', lines
    assert lines.get('assistant1_repainted') == 'PASS', lines


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_compare_before_swap_is_load_bearing():
    """Neuter compare-before-swap (→ always re-create) → node-identity check
    MUST fail while the anchor-offset checks still pass (specificity)."""
    lines = _lines(_run('compare'))
    assert lines.get('node_identity_retained') == 'FAIL', lines
    assert lines.get('anchor_offset_preserved') == 'PASS', lines
    assert lines.get('scroll_actively_adjusted') == 'PASS', lines

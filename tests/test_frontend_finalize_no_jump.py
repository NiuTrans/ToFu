"""jsdom regression for the "生成结束后莫名跳动" (unexplained jump when a
streaming turn finalizes) fix.

WHY
---
The chat container declares ``scroll-behavior:smooth``
(``[data-theme="tofu"] .chat-container`` in styles.css). That is nice for a
user's own wheel/keyboard scroll, but it ALSO animates every programmatic
``el.scrollTop = …`` assignment. Two completion-path scrolls hit that:

  1. ``ConvView.finalizeStreaming`` swaps ``#streaming-msg`` (tall: expanded
     thinking + phase indicator) for the final ``renderMessage`` node (short:
     thinking collapsed, no phase, re-highlighted). It restored the pre-swap
     ``scrollTop`` raw — a DIFFERENT geometry, so content shifts — and the
     smooth behaviour ANIMATED that shift into a visible slide.
  2. ``scrollToBottom`` (core.js) wrote ``scrollTop = scrollHeight`` under the
     same smooth behaviour.

Additionally, hljs syntax-highlighting and lazy KaTeX typesetting change block
heights AFTER finalize's synchronous scroll write, so a position set once was
stale the moment they landed ("定位完再变高" second jump).

THE FIX
-------
  • ``_withInstantScroll(el, fn)`` (core.js) — forces
    ``el.style.scrollBehavior='auto'`` around a programmatic scroll write, then
    restores it. ``scrollToBottom`` and ``finalizeStreaming`` route their writes
    through it, so the snap is instant, never animated.
  • ``finalizeStreaming`` decides the target BEFORE the swap (bottom-parked →
    re-pin to bottom; else hold offset) and RE-APPLIES it on the next two frames
    (rAF²) so the FINAL position is taken AFTER hljs/KaTeX layout settles.

This harness loads the REAL ``conv_view.js`` under jsdom with an instrumented
scroll model (records the ``scroll-behavior`` in force during each scrollTop
write; grows ``scrollHeight`` between the synchronous scroll and the rAF re-pin
to emulate post-swap layout growth). It also extracts + unit-tests the real
``_withInstantScroll`` from ``core.js`` source (evaling core.js is avoided — it
has heavy top-level side effects).

DOUBLE-NEUTER (run via direct node, expecting the discriminating FAIL line):
  • 'nosmooth' — ``_withInstantScroll`` → passthrough → the scrollTop write
    happens under ``smooth`` → ``A_write_smooth_disabled`` FAILS.
  • 'norepin'  — strip the rAF² re-pin → the final position is taken against the
    PRE-growth height → ``A_final_at_bottom_after_growth`` FAILS.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')
_SHARED_HARNESS = os.path.join(HERE, '_jsdom_harness.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const CONV_VIEW = process.argv[2];
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

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Deterministic scroll model ────────────────────────────────────────────
let _scrollTop = 0;
let _scrollHeight = 2000;
let _writeUnderBehavior = null;
const container = document.getElementById('chatContainer');
container.style.scrollBehavior = 'smooth';   // the CSS-declared default
Object.defineProperty(container, 'scrollTop', {
  get: () => _scrollTop,
  set: (v) => { _writeUnderBehavior = container.style.scrollBehavior; _scrollTop = v; },
  configurable: true,
});
Object.defineProperty(container, 'scrollHeight', {
  get: () => _scrollHeight, set: (v) => { _scrollHeight = v; }, configurable: true });
Object.defineProperty(container, 'clientHeight', { get: () => 600, configurable: true });

// rAF: default synchronous so nested rAF² re-pin is observable. Scenario A
// swaps in a growth-injecting variant.
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => 0;

// ── Extract + (optionally neuter) the REAL _withInstantScroll from core.js ──
const coreSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'core.js'), 'utf8');
const _wisMatch = coreSrc.match(/function _withInstantScroll\(el, fn\)\s*\{[\s\S]*?\n\}/);
if (!_wisMatch) { console.log('FAIL withinstantscroll_extracted'); process.exit(0); }
check('withinstantscroll_extracted', true);
let _wisSrc = _wisMatch[0];
if (NEUTER === 'nosmooth') {
  _wisSrc = 'function _withInstantScroll(el, fn) { fn(); }  /* NEUTERED-nosmooth */';
}
(0, eval)(_wisSrc);
win._withInstantScroll = global._withInstantScroll = _withInstantScroll;

// ── Seam unit test (non-inverting): the real seam writes under auto + restores.
(function () {
  const fakeEl = { style: { scrollBehavior: 'smooth' } };
  let seen = null;
  _withInstantScroll(fakeEl, () => { seen = fakeEl.style.scrollBehavior; });
  check('seam_writes_under_auto', seen === 'auto');
  check('seam_restores_prev', fakeEl.style.scrollBehavior === 'smooth');
})();

// ── Stubs conv_view.js touches ──
win.activeConvId = global.activeConvId = 'c1';
win.isNearBottom = global.isNearBottom = (thr) =>
  (_scrollHeight - _scrollTop - 600) < (thr || 150);
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
win._convRenderFingerprint = global._convRenderFingerprint = () => 'fp';
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'tmp'; return m; };
win.CSS = global.CSS = { escape: (s) => s };
win.renderMessage = global.renderMessage = (msg, idx) =>
  '<div class="message" id="msg-' + idx + '" data-msg-id="' + (msg._msgId || '') + '">' +
  '<div class="message-body">' + (msg.content || '') + '</div></div>';

let src = fs.readFileSync(CONV_VIEW, 'utf8');
if (NEUTER === 'norepin') {
  src = src.replace(
    'if (typeof requestAnimationFrame === \'function\') {\n        requestAnimationFrame(() => requestAnimationFrame(_repin));\n      }',
    '/* NEUTERED-norepin */');
  if (src.indexOf('NEUTERED-norepin') < 0) { console.log('FAIL neuter_norepin_applied'); process.exit(0); }
}
(0, eval)(src);
if (!win.ConvView) { console.log('FAIL convview_exposed'); process.exit(0); }
check('convview_exposed', true);

const inner = document.getElementById('chatInner');
const assistant = { role: 'assistant', content: 'final answer', _msgId: 'm1' };
const conv = { id: 'c1', messages: [{ role: 'user', content: 'q', _msgId: 'u1' }, assistant] };
globalThis.conversations = win.conversations = [conv];

function seedStreaming() {
  inner.innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body">streaming…</div></div>';
}

// ── Scenario A: reader parked at the BOTTOM. Layout GROWS (2000→2600) after the
//    synchronous swap. WITH the rAF² re-pin the final scrollTop tracks the GROWN
//    height (2600); a purely-synchronous scroll would be stuck at 2000. Also the
//    scrollTop write must happen with smooth OFF. ──
(function () {
  seedStreaming();
  _scrollHeight = 2000;
  _scrollTop = _scrollHeight - 600;          // exactly bottom → isNearBottom true
  _writeUnderBehavior = null;
  // Growth-injecting rAF: the FIRST scheduled frame grows the doc (post-swap
  // hljs/KaTeX layout), THEN runs the callback (which schedules rAF² → _repin
  // against the grown height).
  let _grew = false;
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => {
    if (!_grew) { _grew = true; _scrollHeight = 2600; }
    fn(); return 0;
  };
  win.ConvView.finalizeStreaming('c1', assistant);
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
  check('A_final_at_bottom_after_growth', _scrollTop === 2600);
  check('A_write_smooth_disabled', _writeUnderBehavior === 'auto');
})();

// ── Scenario B: reader parked UP in history. The offset must be HELD, not
//    yanked to the bottom. ──
(function () {
  seedStreaming();
  _scrollHeight = 2000;
  _scrollTop = 300;                          // parked up (not near bottom)
  win.ConvView.finalizeStreaming('c1', assistant);
  check('B_offset_held_not_bottom', _scrollTop === 300);
})();

console.log(out.join('\n'));
"""


def _run_node(neuter: str = 'none') -> str:
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE,
                                     delete=False, encoding='utf-8') as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, CONV_VIEW, ROOT, neuter],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _SHARED_HARNESS},
        )
    finally:
        try:
            os.remove(hp)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return (proc.stdout or '').strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_finalize_no_jump():
    output = _run_node('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'finalize-no-jump failures:\n' + output
    for name in ('seam_writes_under_auto', 'seam_restores_prev',
                 'A_final_at_bottom_after_growth', 'A_write_smooth_disabled',
                 'B_offset_held_not_bottom'):
        assert f'PASS {name}' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_instant_scroll_seam_is_load_bearing():
    """Passthrough ``_withInstantScroll`` → the finalize scrollTop write happens
    under the CSS-declared ``smooth`` (an animated slide = the bug)."""
    output = _run_node('nosmooth')
    assert 'FAIL A_write_smooth_disabled' in output, (
        'expected the scroll write to be under smooth without the instant-scroll '
        'seam:\n' + output)
    # Seam unit test also flips (passthrough leaves smooth in force):
    assert 'FAIL seam_writes_under_auto' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_post_layout_repin_is_load_bearing():
    """Strip the rAF² re-pin → the final position is set only synchronously,
    against the PRE-growth height, so it does not track the post-swap layout
    growth."""
    output = _run_node('norepin')
    assert 'FAIL A_final_at_bottom_after_growth' in output, (
        'expected the after-growth bottom re-pin to FAIL without the rAF² '
        're-pin:\n' + output)

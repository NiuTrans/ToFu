"""tests/test_frontend_scroll_to_bottom.py — the "scroll to latest" button must
REACH the latest message, not the bottom of a partially-rendered DOM.

WHY
---
Reported symptom: "whenever I click the scroll-to-bottom button I am repeatedly
pushed back to the middle by newly loaded content."

The bounded render window (`_MAX_RENDER_WINDOW`, streaming_render.js) evicts
TAIL bubbles while the reader scrolls up through history, leaving a bottom
sentinel (`_lazyLoadSentinelBottom`) as the last DOM node. The old
`scrollChatToBottom` ran a bare `_forceScrollToBottom`, whose
`scrollTop = scrollHeight` could only reach that SENTINEL — the newest
messages were not in the DOM at all. The IntersectionObserver then drip-fed
one 20-message batch at a time INTO the fold (`_loadNewerMessages` inserts
above the sentinel while the reader sits at the bottom), and each insert left
the reader stranded further above the real bottom. Net effect: one click →
several visible yanks → the reader lands in the middle, exactly as reported.

FIX (static/js/core.js `scrollChatToBottom`)
  Restore the hidden tail BEFORE scrolling:
    • few hidden (≤ 60): walk the existing downward loader — the same guarded
      pump `scrollToTurn` Case A0 uses; continuous DOM, no repaint fallout;
    • many hidden: repaint just the tail window via `ConvView.replaceAll`,
      after pre-pinning scrollTop to the old DOM's bottom so renderChat's
      reader-parked-up anchor heuristic (meant for UNSOLICITED background
      repaints) cannot override this EXPLICIT jump-to-bottom command.

NEUTER CONTROL
  NC: disable the restore block → the tail is NOT restored on click (the
  reader is stranded above the evicted messages again), while the scroll
  itself still happens — proving the restore block, and only it, is
  load-bearing.

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
# streaming_render.js + the REAL scrollChatToBottom extracted from core.js are
# eval'd in ONE scope (exactly how the bundler concatenates them): the render
# window bounds `_lazyRenderedTo` / `_loadingNewer` are `let`-declared in the
# former and READ/WRITTEN by the latter. Evaluating them separately would give
# the button different bindings than the loaders, and the test would prove
# nothing.
# ══════════════════════════════════════════════════════════════════════════

_DRIVER = r"""
;(function () {
  const out = global.__out;
  const inner = document.getElementById('chatInner');
  const container = document.getElementById('chatContainer');
  function check(name, cond, extra) {
    out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : ''));
  }

  /* Faithful stand-in for renderChat's FULL re-render path (what the REAL
   * ConvView.replaceAll drives): repaints the TAIL window
   * [total-_INITIAL_RENDER, total), resets the lazy bounds exactly as
   * chat_render.js does, and force-scrolls like its forceScroll branch.
   * Defined INSIDE the eval scope so it writes the same let-bindings the
   * loaders read. Records calls so the pump/repaint split is observable. */
  window.ConvView = { calls: 0, replaceAll: function (cid) {
    this.calls++;
    const c = conversations.find(x => x.id === cid);
    if (!c) return false;
    const total = c.messages.length;
    const s = Math.max(0, total - _INITIAL_RENDER);
    _lazyRenderedFrom = s; _lazyRenderedTo = total; _lazyConvId = c.id;
    let h = '<div id="_lazyLoadSentinel" class="lazy-sentinel">'
          + '<span class="_lazy-count">' + s + '</span></div>';
    for (let i = s; i < total; i++) h += renderMessage(c.messages[i], i);
    inner.innerHTML = h;
    _forceScrollToBottom(container, true);
    return true;
  }};

  function seedTail(conv) {
    const total = conv.messages.length;
    const START = Math.max(0, total - _INITIAL_RENDER);
    _lazyConvId = conv.id; _lazyRenderedFrom = START; _lazyRenderedTo = total;
    let html = '<div id="_lazyLoadSentinel" class="lazy-sentinel">'
             + '<span class="_lazy-count">' + START + '</span></div>';
    for (let i = START; i < total; i++) html += renderMessage(conv.messages[i], i);
    inner.innerHTML = html;
    _ensureLazyObserver();
  }
  /* Scroll up until the bounded window has evicted the tail. With N=300:
   * 6 up-loads → 60 hidden (pump boundary), 7 → 80 hidden (repaint path). */
  function scrollUp(times) {
    for (let k = 0; k < times; k++) { _loadingOlder = false; _loadOlderMessages(); }
  }

  const convA = conversations[0], TOTAL = convA.messages.length;
  const atBottom = () => container.scrollTop >= container.scrollHeight - 800;

  // ══ Case 1: few hidden (≤ 60) → guarded pump restores the tail ══
  seedTail(convA);
  scrollUp(6);
  const winTo1 = _lazyRenderedTo;
  check('window_is_capped', winTo1 < TOTAL, 'to=' + winTo1 + ' total=' + TOTAL);
  window.ConvView.calls = 0;
  scrollChatToBottom();
  check('tail_restored', _lazyRenderedTo === TOTAL, 'to=' + _lazyRenderedTo);
  check('bottom_sentinel_gone',
        !document.getElementById('_lazyLoadSentinelBottom'));
  check('landed_at_bottom', atBottom(),
        'scrollTop=' + container.scrollTop + ' scrollH=' + container.scrollHeight);
  check('pump_not_repaint', window.ConvView.calls === 0,
        'calls=' + window.ConvView.calls);

  // ══ Case 2: many hidden (> 60) → tail-window repaint, still true bottom ══
  seedTail(convA);
  scrollUp(7);
  check('large_gap_capped', _lazyRenderedTo < TOTAL, 'to=' + _lazyRenderedTo);
  window.ConvView.calls = 0;
  scrollChatToBottom();
  check('repaint_for_large_gap', window.ConvView.calls === 1,
        'calls=' + window.ConvView.calls);
  check('tail_restored_large', _lazyRenderedTo === TOTAL,
        'to=' + _lazyRenderedTo);
  check('landed_at_bottom_large', atBottom(),
        'scrollTop=' + container.scrollTop + ' scrollH=' + container.scrollHeight);

  // ══ Case 3: tail intact — a plain force scroll, no restore work at all ══
  seedTail(convA);
  container.scrollTop = 0;
  window.ConvView.calls = 0;
  scrollChatToBottom();
  check('intact_tail_no_repaint',
        window.ConvView.calls === 0 && _lazyRenderedTo === TOTAL);
  check('intact_tail_at_bottom', atBottom());

  // ══ Case 4: a live stream owns the tail — never pump/repaint under it ══
  seedTail(convA);
  scrollUp(6);
  activeStreams.set('cA', { taskId: 'x' });
  window.ConvView.calls = 0;
  const toBefore = _lazyRenderedTo;
  scrollChatToBottom();
  check('stream_tail_untouched',
        _lazyRenderedTo === toBefore && window.ConvView.calls === 0,
        'to=' + _lazyRenderedTo + ' calls=' + window.ConvView.calls);
  activeStreams.delete('cA');

  console.log(out.join('\n'));
})();
"""

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

// Deterministic geometry: every bubble 100px, viewport 800px.
const CLIENT_H = 800, H = 100;
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
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);
win.IntersectionObserver = global.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {}
};
win.renderMessage = global.renderMessage = (msg, i) =>
  '<div class="message" id="msg-' + i + '" data-msg-id="' + (msg && msg._msgId) + '">m' + i + '</div>';
win.activeStreams = global.activeStreams = new Map();
win._getChatContainer = global._getChatContainer = () => container;
win.chatInnerInsert = global.chatInnerInsert = undefined;
win._updateScrollToBottomBtn = global._updateScrollToBottomBtn = () => {};

const N = 300;
const convA = { id: 'cA', messages: [] };
for (let i = 0; i < N; i++) {
  convA.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'msg ' + i, _msgId: 'a' + i });
}
win.conversations = global.conversations = [convA];
win.activeConvId = global.activeConvId = 'cA';
win.getActiveConv = global.getActiveConv = () => convA;
global.__out = [];

let SR = fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8');
// Slice TWO blocks out of core.js: the explicit-bottom-latch listener arming
// (`_stbLatchListenersArmed` / `_armStbLatchClearListeners` — free vars that
// scrollChatToBottom calls on every click) AND scrollChatToBottom itself.
// Slicing only the latter leaves a bare ReferenceError in the one-eval scope.
const _CORE_SRC = fs.readFileSync(path.join(ROOT, 'static', 'js', 'core.js'), 'utf8');
const LATCH = _CORE_SRC.match(/let _stbLatchListenersArmed[\s\S]*?\n\}/)[0];
let CORE = _CORE_SRC.match(/function scrollChatToBottom\(\) \{[\s\S]*?\n\}/)[0];
if (NC === 'nc_restore') {
  // NC: disable the hidden-tail restore block — the button goes back to a
  // bare force-scroll that can only reach the bottom sentinel.
  const before = CORE;
  CORE = CORE.replace('if (_stbConv && !activeStreams.has(_stbConv.id)',
                      'if (false && _stbConv && !activeStreams.has(_stbConv.id)');
  if (CORE === before) { console.log('FAIL nc_pattern_applied'); process.exit(0); }
}
console.log('PASS nc_pattern_applied');

const DRIVER = __DRIVER__;
eval(SR + '\n' + LATCH + '\n' + CORE + '\n' + DRIVER);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_scrollbottom_{nc or "main"}.js')
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
def test_scroll_to_bottom_restores_hidden_tail():
    """With the tail evicted by the bounded window, one click restores it fully
    (pump for a small gap, tail repaint for a large one) and lands on the TRUE
    bottom — no observer drip-feed, no bounce back to the middle."""
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'scroll-to-bottom failures:\n' + output
    for needed in ('window_is_capped', 'tail_restored', 'bottom_sentinel_gone',
                   'landed_at_bottom', 'pump_not_repaint',
                   'large_gap_capped', 'repaint_for_large_gap',
                   'tail_restored_large', 'landed_at_bottom_large',
                   'intact_tail_no_repaint', 'intact_tail_at_bottom',
                   'stream_tail_untouched'):
        assert f'PASS {needed}' in output, f'missing PASS {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_without_tail_restore_the_button_strands_the_reader():
    """NC: disable the restore block → the click scrolls (still works) but the
    tail is NOT restored — the drip-feed / stranded-reader bug returns."""
    v = _verdicts(_run('nc_restore'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    assert v.get('tail_restored') == 'FAIL', (
        'Removing the restore block did NOT strand the tail — it is not '
        f'load-bearing: {v}')
    # The NC is surgical: the window was really capped and the scroll itself
    # still happened (to the sentinel's bottom — the wrong place, but a
    # scroll), so only the restore is responsible for reaching the tail.
    assert v.get('window_is_capped') == 'PASS', v
    assert v.get('landed_at_bottom') == 'PASS', v

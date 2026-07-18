"""jsdom regression for the BOUNDED RENDER WINDOW (long-conversation scroll jank).

WHY
---
Upward lazy-load (`_loadOlderMessages`) prepends 20 older bubbles each time the
reader scrolls near the top, but historically NEVER evicted anything. Scrolling
up through a 500-message conversation therefore accreted hundreds of heavy
`.message` nodes that were never reclaimed. `content-visibility:auto` only saves
PAINT — the browser still maintains a layout box for every node, every
`renderChat` surgical pass does `querySelectorAll('[id^="msg-"]')` over all of
them, and `updateActiveTurn` hit-tests grow with the count. That is the real
"the longer the conversation, the laggier scrolling gets" driver.

The fix caps the rendered span at `_MAX_RENDER_WINDOW`: every upward load evicts
from the TAIL (and every downward `_loadNewerMessages` evicts from the HEAD), so
the live DOM node count has a CONSTANT upper bound regardless of history length,
and a bottom sentinel (`_lazyLoadSentinelBottom`) lets the reader scroll back
down to re-render the evicted tail.

This harness evals the real streaming_render.js WITH the driver appended INSIDE
the same eval scope — required because the window bounds (`_lazyRenderedFrom` /
`_lazyRenderedTo`) are `let`-declared in that file, so a post-eval assignment
from the harness scope would write a DIFFERENT (global) binding the eval'd
functions never read. It builds a long conversation, drives repeated upward
loads, and asserts the DOM node count never exceeds the cap; then scrolls back
down via `_loadNewerMessages` and asserts the tail returns and the head is
evicted.

NEUTER: raise `_MAX_RENDER_WINDOW` to Infinity (disable eviction) and prove the
node count then grows unbounded — i.e. the cap is what bounds the DOM.
"""

from __future__ import annotations

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


# Driver JS appended to streaming_render.js and eval'd IN THE SAME SCOPE so it
# shares the `let`-declared window bounds (_lazyRenderedFrom / _lazyRenderedTo).
_DRIVER = r"""
;(function _driver() {
  const out = global.__bwOut;
  const N = global.__bwN;
  const inner = document.getElementById('chatInner');
  function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }
  function idxs() {
    const els = inner.querySelectorAll('[id^="msg-"]');
    const a = []; els.forEach((el) => { const m = el.id.match(/^msg-(\d+)$/); if (m) a.push(parseInt(m[1], 10)); });
    return a.sort((x, y) => x - y);
  }
  const count = () => idxs().length;

  // Seed the DOM as a full-render tail would (last 20 msgs + top sentinel), and
  // set the window bounds EXACTLY as renderChat sets them.
  const START = N - 20;
  _lazyConvId = 'c1';
  _lazyRenderedFrom = START;
  _lazyRenderedTo = N;
  let html = '<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="_lazy-count">' + START + '</span></div>';
  for (let i = START; i < N; i++) html += '<div class="message" id="msg-' + i + '">m' + i + '</div>';
  inner.innerHTML = html;
  _ensureLazyObserver();

  const CAP = 80, BATCH = 20;

  // ── Drive many upward loads (reader scrolling up through history). ──
  let maxSeen = count();
  for (let k = 0; k < 25; k++) { _loadingOlder = false; _loadOlderMessages(); maxSeen = Math.max(maxSeen, count()); }
  const up = idxs();
  check('reached_top', up.length > 0 && up[0] === 0, 'first=' + up[0]);
  check('node_count_bounded', maxSeen <= CAP + BATCH + 2, 'maxSeen=' + maxSeen + ' cap=' + CAP);
  check('bottom_sentinel_present', !!document.getElementById('_lazyLoadSentinelBottom'), 'to=' + _lazyRenderedTo);
  check('tail_evicted', _lazyRenderedTo < N, 'to=' + _lazyRenderedTo);

  // ── Scroll back DOWN: drive downward loads until the true tail returns. ──
  let maxDown = count();
  for (let k = 0; k < 60 && _lazyRenderedTo < N; k++) { _loadingNewer = false; _loadNewerMessages(); maxDown = Math.max(maxDown, count()); }
  const dn = idxs();
  check('tail_restored', _lazyRenderedTo >= N, 'to=' + _lazyRenderedTo);
  check('last_msg_present', dn.length > 0 && dn[dn.length - 1] === N - 1, 'last=' + dn[dn.length - 1]);
  check('node_count_bounded_after_down', maxDown <= CAP + BATCH + 2, 'maxDown=' + maxDown);
  check('head_evicted_on_down', dn[0] > 0, 'first=' + dn[0]);

  console.log('METRICS ' + JSON.stringify({ maxSeen, maxDown, firstUp: up[0], lastDown: dn[dn.length - 1] }));
  console.log(out.join('\n'));
})();
"""

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

// ── Deterministic geometry: every bubble 100px tall, viewport 800px. ──
const CLIENT_H = 800, H = 100;
let _scrollTop = 0;
function _renderedIndices() {
  const els = document.getElementById('chatInner').querySelectorAll('[id^="msg-"]');
  const out = [];
  els.forEach((el) => { const m = el.id.match(/^msg-(\d+)$/); if (m) out.push(parseInt(m[1], 10)); });
  return out.sort((a, b) => a - b);
}
function _scrollHeight() { return _renderedIndices().length * H; }
const container = document.getElementById('chatContainer');
Object.defineProperty(container, 'clientHeight', { get: () => CLIENT_H, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => _scrollHeight(), configurable: true });
Object.defineProperty(container, 'scrollTop', {
  get: () => _scrollTop,
  set: (v) => { const max = Math.max(0, _scrollHeight() - CLIENT_H); _scrollTop = Math.max(0, Math.min(v, max)); },
  configurable: true,
});
container.style = container.style || {};

// getBoundingClientRect positions each rendered bubble by its ORDER in the DOM.
win.Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'chatContainer') return { top: 0, bottom: CLIENT_H, height: CLIENT_H, left: 0, right: 0, width: 0 };
  const arr = _renderedIndices();
  const m = (this.id || '').match(/^msg-(\d+)$/);
  if (m) {
    const order = arr.indexOf(parseInt(m[1], 10));
    if (order < 0) return { top: 0, bottom: 0, height: H, left: 0, right: 0, width: 0 };
    const top = order * H - _scrollTop;
    return { top, bottom: top + H, height: H, left: 0, right: 0, width: 0 };
  }
  return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0 };
};

global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);

// IntersectionObserver stub — we drive _loadOlder/_loadNewer directly.
win.IntersectionObserver = global.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {}
};

win.renderMessage = global.renderMessage = (msg, i) => '<div class="message" id="msg-' + i + '">m' + i + '</div>';
win.buildTurnNav = global.buildTurnNav = () => {};
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c1';

// Long conversation.
const N = 300;
const conv = { id: 'c1', messages: [] };
for (let i = 0; i < N; i++) conv.messages.push({ role: i % 2 ? 'assistant' : 'user', content: 'x', _msgId: 'm' + i });
win.conversations = global.conversations = [conv];
global.__bwOut = [];
global.__bwN = N;

let src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_render.js'), 'utf8');
if (NEUTER === 'nocap') {
  const before = src;
  src = src.replace('const _MAX_RENDER_WINDOW = 80;', 'const _MAX_RENDER_WINDOW = Infinity;');
  if (src === before) { console.log('FAIL neuter_nocap_not_applied'); process.exit(0); }
}

// Append the driver so it runs IN THE SAME eval scope as the `let`-declared
// window bounds (_lazyRenderedFrom / _lazyRenderedTo). A post-eval assignment
// from here would hit a different (global) binding the functions never read.
const DRIVER = __DRIVER__;
eval(src + '\n' + DRIVER);
"""


def _build_harness() -> str:
    # Inline the driver as a JS string literal via JSON encoding.
    import json
    return _HARNESS.replace('__DRIVER__', json.dumps(_DRIVER))


def _run(neuter: str = 'none'):
    harness = os.path.join(HERE, '_bounded_window_harness.js')
    with open(harness, 'w') as f:
        f.write(_build_harness())
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
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_bounded_render_window_caps_dom_and_round_trips():
    """Repeated upward loads keep the DOM node count bounded, reach the top, and
    place a bottom sentinel; scrolling back down restores the tail and evicts the
    head — all while staying under the cap."""
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'bounded-window failures:\n' + output
    for ln in output.splitlines():
        if ln.startswith('METRICS'):
            print(ln)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_nocap_grows_unbounded():
    """Neuter the cap (_MAX_RENDER_WINDOW = Infinity → no eviction). The
    node-count-bounded assertion MUST then fail, proving the cap is load-bearing
    for bounding the DOM."""
    lines = _lines(_run('nocap'))
    assert lines.get('node_count_bounded') == 'FAIL', lines
    assert lines.get('reached_top') == 'PASS', lines

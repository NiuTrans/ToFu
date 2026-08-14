"""tests/test_frontend_follow_suspension.py — the streaming bottom-follow must
yield to the reader's upward scroll INTENT, not just their position.

WHY
---
Reported symptom: "at the bottom of a conversation that is rapidly updating
status, scrolling up constantly fights downward — the page shakes like it's
trembling."

The auto-follow gates were POSITIONAL only (`isNearBottom(80/200)`): while a
streaming tail grows at up to 30fps, a reader who starts scrolling up is
yanked back to the bottom on every update until their gesture crosses the
threshold — and each yank resets their progress, so the gesture and the
programmatic pin fight every frame inside the threshold window. On wheel
momentum / trackpad / touch drag this renders as a continuous tremble.

FIX (frontend/src/runtime/app-runtime.js, core.js section)
  1. `_followSuspended` latch + `_armFollowSuspensionListeners()` (armed at
     boot next to wireConvWindowScrollLoader): wheel-up, touch-drag-down, and
     any user-driven scroll event moving AWAY from the bottom suspend the
     follow; landing back at the bottom resumes it.
  2. `scrollToBottom()` gates on the latch at SCHEDULE time and re-checks it
     at WRITE time inside the rAF callback — a gesture between schedule and
     frame can no longer be overridden by the pending write.
  3. `_forceScrollToBottom` (ui/streaming_render.js) clears the latch: an
     explicit pin (scroll-to-bottom button, turn transitions) is a command to
     follow again.

NEUTER CONTROL
  NC: strip both `_followSuspended` terms from `scrollToBottom` (the schedule
  gate and the write-time re-check) → the pending write lands after the
  reader's wheel-up, subsequent updates keep scheduling, and the scrollbar
  drag inside the threshold window schedules too — the fight returns. The
  plain following behaviour (at-bottom pin, resume, explicit pin) stays green,
  proving the NC is surgical.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _fn_span(src: str, name: str) -> tuple[int, int]:
    """Brace-match the span of a top-level `function name(...) { ... }`."""
    start = src.index('function ' + name + '(')
    i = src.index('{', start)
    depth = 0
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise AssertionError('unbalanced braces extracting ' + name)


def _core_scroll_slice() -> str:
    """The scroll block of the migrated core.js section: container cache +
    isNearBottom + _withInstantScroll + the follow-suspension latch +
    scrollToBottom + scrollChatToBottom + _updateScrollToBottomBtn."""
    core = runtime_section('core.js', scope_prelude=False)
    start = core.index('let _chatContainerEl = null;')
    end = _fn_span(core, '_updateScrollToBottomBtn')[1]
    return core[start:end]


def _force_scroll_fn() -> str:
    sr = runtime_section('ui/streaming_render.js', scope_prelude=False)
    start, end = _fn_span(sr, '_forceScrollToBottom')
    return sr[start:end]


_DRIVER = r"""
;(function () {
  const out = global.__out;
  function check(name, cond, extra) {
    out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : ''));
  }
  _armFollowSuspensionListeners();

  // ── 1. Reader at the bottom: an update schedules the coalesced pin and it
  //    lands — plain following is unchanged.
  scrollToBottom();
  check('follow_schedules_at_bottom', rafQ.length === 1, 'q=' + rafQ.length);
  flushRaf();
  check('follow_pins_bottom', stNow() === bottom(), 'st=' + stNow());

  // ── 2. THE FIGHT: the tail grows 150px (still inside the 200px gate — the
  //    pre-fix combat zone). The update schedules its pin; the reader wheels
  //    up BEFORE the frame fires; the pending write must be cancelled and
  //    subsequent 30fps updates must stop scheduling entirely.
  grow(150);
  scrollToBottom();
  check('fight_zone_schedules', rafQ.length === 1, 'q=' + rafQ.length);
  wheelUp();
  flushRaf();
  check('write_respects_suspension', stNow() === bottom() - 150,
        'st=' + stNow() + ' want=' + (bottom() - 150));
  scrollToBottom(); scrollToBottom();
  check('updates_stop_scheduling', rafQ.length === 0, 'q=' + rafQ.length);
  check('reader_not_yanked', stNow() === bottom() - 150, 'st=' + stNow());

  // ── 3. Scrollbar drag (no wheel event at all): a 120px drag up — still
  //    inside the gate window — must suspend via the scroll listener alone.
  dragTo(bottom());
  dragTo(bottom() - 120);
  scrollToBottom();
  check('scroll_drag_suspends', rafQ.length === 0, 'q=' + rafQ.length);

  // ── 4. Landing back at the bottom resumes the follow.
  grow(200);
  dragTo(bottom());
  scrollToBottom();
  check('resume_refollows', rafQ.length === 1, 'q=' + rafQ.length);
  flushRaf();
  check('resume_pins', stNow() === bottom(), 'st=' + stNow());

  // ── 5. An explicit pin (_forceScrollToBottom: the scroll-to-bottom button,
  //    turn transitions) jumps AND resumes follow for later updates.
  wheelUp();
  grow(300);
  _forceScrollToBottom();
  check('explicit_pin_jumps', stNow() === bottom(), 'st=' + stNow());
  flushRaf(); flushRaf();  // drain the force path's nested double-rAF writes
  scrollToBottom();
  check('follow_after_pin', rafQ.length === 1, 'q=' + rafQ.length);
  flushRaf();
  check('follow_after_pin_lands', stNow() === bottom(), 'st=' + stNow());

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
global.__out = [];

// ── Deterministic geometry: viewport 800px, content height a driver-owned
//    variable (streaming growth = grow(px)). scrollTop clamps like a browser.
const CLIENT_H = 800;
let contentH = 5000;
let _st = 4200;   // start pinned at the bottom
const container = document.getElementById('chatContainer');
Object.defineProperty(container, 'clientHeight', { get: () => CLIENT_H, configurable: true });
Object.defineProperty(container, 'scrollHeight', { get: () => contentH, configurable: true });
Object.defineProperty(container, 'scrollTop', {
  get: () => _st,
  set: (v) => { const max = Math.max(0, contentH - CLIENT_H); _st = Math.max(0, Math.min(v, max)); },
  configurable: true,
});
const bottom = () => Math.max(0, contentH - CLIENT_H);
const stNow = () => container.scrollTop;
const grow = (px) => { contentH += px; };

// ── Controllable frame clock: rAF callbacks queue until flushed, so the
//    driver can interleave a reader gesture between schedule and write.
const rafQ = [];
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
global.setTimeout = win.setTimeout = () => 0;
function flushRaf() { const q = rafQ.splice(0, rafQ.length); q.forEach((fn) => fn()); }
function wheelUp() {
  const e = new win.Event('wheel');
  e.deltaY = -120;
  container.dispatchEvent(e);
}
/* jsdom fires no scroll event on a scrollTop assignment — dispatch it
 * manually, exactly what a real browser does for scrollbar drags. */
function dragTo(top) { container.scrollTop = top; container.dispatchEvent(new win.Event('scroll')); }

// ── Globals the sliced core block reads (scrollChatToBottom references).
const conv = { id: 'c1', messages: [] };
win.conversations = global.conversations = [conv];
win.activeConvId = global.activeConvId = 'c1';
win.getActiveConv = global.getActiveConv = () => conv;
win.activeStreams = global.activeStreams = new Map();

let CORE = __CORE_SLICE__;
const FORCE = __FORCE_FN__;
if (NC === 'nc_suspend') {
  const before = CORE;
  CORE = CORE.replace(
    'if (!force && (_followSuspended || !isNearBottom(200))) {',
    'if (!force && !isNearBottom(200)) {');
  CORE = CORE.replace('    if (_followSuspended) return;\n', '');
  if (CORE === before) { console.log('FAIL nc_pattern_applied'); process.exit(0); }
}
console.log('PASS nc_pattern_applied');

const DRIVER = __DRIVER__;
eval('var runtimeScope = window;\n' + CORE + '\n' + FORCE + '\n' + DRIVER);
"""


def _run(nc: str = '') -> str:
    core = _core_scroll_slice()
    force = _force_scroll_fn()
    assert '_armFollowSuspensionListeners' in core, 'latch missing from core slice'
    assert '_followSuspended' in core, 'latch state missing from core slice'
    harness = os.path.join(HERE, f'_followsuspend_{nc or "main"}.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(
            _HARNESS
            .replace('__CORE_SLICE__', json.dumps(core))
            .replace('__FORCE_FN__', json.dumps(force))
            .replace('__DRIVER__', json.dumps(_DRIVER)))
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
def test_scroll_up_suspends_streaming_follow():
    """A reader's upward gesture during rapid updates detaches the auto-follow
    (no yank, no tremble); landing back at the bottom or an explicit pin
    re-attaches it."""
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'follow-suspension failures:\n' + output
    for needed in ('follow_schedules_at_bottom', 'follow_pins_bottom',
                   'fight_zone_schedules', 'write_respects_suspension',
                   'updates_stop_scheduling', 'reader_not_yanked',
                   'scroll_drag_suspends', 'resume_refollows', 'resume_pins',
                   'explicit_pin_jumps', 'follow_after_pin',
                   'follow_after_pin_lands'):
        assert f'PASS {needed}' in output, f'missing PASS {needed} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_without_suspension_the_downward_fight_returns():
    """NC: strip the latch from scrollToBottom's schedule gate + write-time
    re-check → the pending write lands after the reader's wheel-up, further
    updates keep scheduling, and an in-window scrollbar drag schedules too —
    the reported tremble returns. Plain following stays green."""
    v = _verdicts(_run('nc_suspend'))
    assert v.get('nc_pattern_applied') == 'PASS', v
    for broken in ('write_respects_suspension', 'updates_stop_scheduling',
                   'reader_not_yanked', 'scroll_drag_suspends'):
        assert v.get(broken) == 'FAIL', (
            f'Removing the suspension latch did NOT reintroduce the fight at '
            f'{broken} — the latch is not load-bearing: {v}')
    for intact in ('follow_schedules_at_bottom', 'follow_pins_bottom',
                   'fight_zone_schedules', 'resume_refollows',
                   'explicit_pin_jumps'):
        assert v.get(intact) == 'PASS', (
            f'NC must be surgical — {intact} should still PASS: {v}')

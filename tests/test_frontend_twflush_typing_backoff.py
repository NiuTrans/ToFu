#!/usr/bin/env python3
"""Composer-typing render backoff — the "mid-generation input box is too laggy" fix.

WHY
---
`_twFlush` (static/js/core/health_stream_timer.js) re-renders the streaming
markdown tail on a rate cap. The cap was a hard 33ms constant (~30fps). While
the user is TYPING into the composer during a live stream (exactly when they
draft a mid-turn steer/queue message — the reported "每敲一个字都卡" case), a
full tail markdown re-render every 33ms competes with keystroke handling on the
single main thread and shows up as visible input lag.

THE FIX, asserted here end-to-end against the REAL shipped `_twMinInterval` +
`_twFlush` extracted from the source and eval'd in node:

  When the composer textarea (`#userInput`) is `document.activeElement`,
  `_twFlush` throttles against `_TW_TYPING_INTERVAL` (200ms, ~5fps) instead of
  `_TW_MIN_INTERVAL` (33ms) — freeing the main thread for keystrokes. It reverts
  to 33ms the instant the composer loses focus.

Asserts:
  A. `_twMinInterval()` returns 200 when #userInput is focused, 33 otherwise
     (blurred, or some OTHER element focused).
  B. `_twFlush` OBSERVABLE throttle: at 100ms since the last flush, a FOCUSED
     composer reschedules via rAF and does NOT render (100 < 200); a BLURRED
     composer renders (100 >= 33). This is the load-bearing behaviour.

NEUTER: revert `_twFlush`'s gate to the bare `_TW_MIN_INTERVAL` constant and
prove the focused case now RENDERS at 100ms (the typing backoff is defeated) —
i.e. routing the gate through `_twMinInterval()` is load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
TIMER_JS = os.path.join(ROOT, 'static', 'js', 'core', 'health_stream_timer.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


# Stubbed environment: no real DOM / rAF / clock. `performance`, `document`,
# `requestAnimationFrame` are assigned onto globalThis (statements, not lexical
# `const`, so they never collide with node's own globals). Everything else the
# two extracted functions close over is declared at module scope.
_PREAMBLE = r'''
let _NOW = 0;            // controllable monotonic clock (performance.now)
let _rafCount = 0;       // # of requestAnimationFrame reschedules
let _renderCount = 0;    // # of actual updateStreamingUI renders
let _focusedEl = null;   // what document.activeElement returns

globalThis.performance = { now: () => _NOW };
globalThis.requestAnimationFrame = (fn) => { _rafCount++; return 1; };
globalThis.cancelAnimationFrame = () => {};

const _inputEl = { id: 'userInput' };
globalThis.document = {
  getElementById(id) {
    if (id === 'userInput') return _inputEl;
    if (id === 'streaming-body') return {};   // truthy → _twFlush render branch
    return null;
  },
  get activeElement() { return _focusedEl; },
};

const streamBufs = new Map();
let activeConvId = null;
let _twPendingConvId = 'c1';
let _twRafId = null, _twTimeoutId = null, _twDirty = false, _twLastFlush = 0;
const _TW_MIN_INTERVAL = 33;
const _TW_TYPING_INTERVAL = 200;
function updateStreamingUI(arg) { _renderCount++; }
function _streamFrameArg(cid) { return { content: 'x' }; }  // non-null → render proceeds
'''


def _tw_fns(poison: str = '') -> str:
    src = _read(TIMER_JS)
    body = _extract_fn(src, '_twMinInterval') + '\n' + _extract_fn(src, '_twFlush')
    if poison == 'constant':
        # Neuter: revert _twFlush's gate to the bare constant so focus no longer
        # widens the interval — the typing backoff is defeated.
        body = body.replace(
            "(_now - _twLastFlush) < _twMinInterval()",
            "(_now - _twLastFlush) < _TW_MIN_INTERVAL")
        assert "< _twMinInterval()" not in body, 'constant poison did not apply'
    return body


def _run(extracted: str, driver: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    harness = f'''
{_PREAMBLE}
{extracted}
(async () => {{
{driver}
}})();
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=25)
        assert out.returncode == 0, f'node eval failed:\n{out.stderr}'
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


# ─────────────────────── A: _twMinInterval focus-aware ───────────────────────

def test_twmininterval_returns_typing_interval_only_when_composer_focused():
    """200ms while #userInput is focused; 33ms when blurred or another element
    holds focus."""
    # Guard: the fix must actually be wired (test not stale).
    src = _read(TIMER_JS)
    assert '_twMinInterval' in src and '_TW_TYPING_INTERVAL' in src, \
        'composer-typing backoff missing from health_stream_timer.js — test stale'
    driver = '''
const r = {};
_focusedEl = _inputEl;          // composer focused
r.focused = _twMinInterval();
_focusedEl = null;              // nothing focused
r.blurred = _twMinInterval();
_focusedEl = { id: 'somethingElse' };   // a DIFFERENT element focused
r.other = _twMinInterval();
console.log(JSON.stringify(r));
'''
    r = _run(_tw_fns(), driver)
    assert r['focused'] == 200, 'focused composer must use the 200ms typing interval'
    assert r['blurred'] == 33, 'blurred composer must use the 33ms default interval'
    assert r['other'] == 33, 'another focused element must NOT trigger the typing interval'


# ─────────────────── B: _twFlush observable throttle decision ───────────────────

def test_twflush_reschedules_without_render_while_composer_focused():
    """At 100ms since the last flush: FOCUSED composer reschedules (rAF) and does
    NOT render (100 < 200); BLURRED composer renders (100 >= 33)."""
    driver = '''
const r = {};
// FOCUSED: 100ms elapsed < 200ms typing interval → reschedule, no render.
_focusedEl = _inputEl; _NOW = 100; _twLastFlush = 0; _twDirty = true;
_rafCount = 0; _renderCount = 0;
_twFlush();
r.focused_render = _renderCount;
r.focused_raf = _rafCount;
// BLURRED: 100ms elapsed >= 33ms default interval → render.
_focusedEl = null; _NOW = 100; _twLastFlush = 0; _twDirty = true;
_rafCount = 0; _renderCount = 0;
_twFlush();
r.blurred_render = _renderCount;
console.log(JSON.stringify(r));
'''
    r = _run(_tw_fns(), driver)
    assert r['focused_render'] == 0, 'focused composer must NOT render mid-throttle (frees main thread)'
    assert r['focused_raf'] == 1, 'focused composer must reschedule via requestAnimationFrame'
    assert r['blurred_render'] == 1, 'blurred composer must render at 100ms (>= 33ms)'


def test_NC_constant_gate_ignores_composer_focus():
    """NEUTER: revert _twFlush's gate to the bare _TW_MIN_INTERVAL constant. The
    focused case now RENDERS at 100ms (100 >= 33) — the typing backoff is gone.
    Proves routing the gate through _twMinInterval() is load-bearing."""
    driver = '''
const r = {};
_focusedEl = _inputEl; _NOW = 100; _twLastFlush = 0; _twDirty = true;
_rafCount = 0; _renderCount = 0;
_twFlush();
r.focused_render = _renderCount;
console.log(JSON.stringify(r));
'''
    r = _run(_tw_fns(poison='constant'), driver)
    assert r['focused_render'] == 1, \
        'neutered constant gate renders even while composer focused (backoff defeated)'

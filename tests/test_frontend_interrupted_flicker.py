"""jsdom regression for the interrupted-turn content FLICKER (P1b).

WHY
---
Conv mrnee15nzqnoej: a turn interrupted by a server crash has NO persisted
terminal metadata. Several recovery writers then repaint the SAME trailing
assistant message from DIFFERENT fold sources — the SSE cold-replay `state`,
the `_pollFallback` loop, and startup Case-B — each computing content from the
event-log fold vs the 5 s checkpoint. When the two folds are similar length,
neither wins decisively and the bubble visibly SWAPS back and forth.

THE FIX (this suite locks it in): `pollWriteWouldClobberSettledTail(msg,
polledTaskId, data)` in core/conversations.js is the single source of truth for
"may this poll / Case-B snapshot overwrite the tail's content?". Once the tail
is SETTLED (carries a finishReason), a write is SUPPRESSED unless it strictly
GROWS the content, and any snapshot from a DIFFERENT task is rejected. A live
(not-yet-settled) tail is never suppressed — normal streaming flows through.
`_pollFallback` (sse_poll_fallback.js) and Case-B (main_init_tasks.js) both
consult it before adopting `data.content`.

This harness drives the REAL shipped predicate under jsdom.
NC mode neuters the settled-guard (treat as if it always returns false) → the
equal-length competing fold is adopted, proving the guard is what stops the
swap. Skips cleanly when node + jsdom aren't installed.
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


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] === 'NC';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'core', 'conversations.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

let fn = win.pollWriteWouldClobberSettledTail;
if (typeof fn !== 'function') { console.log('FAIL fn_exposed'); process.exit(0); }
check('fn_exposed', true);

// NC: neuter the guard so it never suppresses (proves the guard is load-bearing).
if (NC) fn = function () { return false; };

// A SETTLED interrupted tail owned by task T1, showing a 200-char content.
const settled = {
  role: 'assistant', _taskId: 'T1aaaaaa', finishReason: 'interrupted',
  content: 'x'.repeat(200),
};

// 1. Competing fold of EQUAL length for the SAME task → must be suppressed
//    (this is the exact swap: two 200-char folds alternating).
check('equal_len_same_task_suppressed',
      fn(settled, 'T1aaaaaa', { content: 'y'.repeat(200) }) === true);

// 2. Shorter competing fold → suppressed.
check('shorter_suppressed',
      fn(settled, 'T1aaaaaa', { content: 'y'.repeat(150) }) === true);

// 3. STRICTLY LONGER content → allowed (real extra content the checkpoint
//    missed; monotonic growth is legitimate).
check('strict_growth_allowed',
      fn(settled, 'T1aaaaaa', { content: 'y'.repeat(260) }) === false);

// 4. Snapshot for a DIFFERENT task → suppressed regardless of length.
check('foreign_task_suppressed',
      fn(settled, 'T2bbbbbb', { content: 'y'.repeat(999) }) === true);

// 5. A LIVE tail (no finishReason) is NEVER suppressed — normal streaming.
const live = { role: 'assistant', _taskId: 'T1aaaaaa', content: 'x'.repeat(200) };
check('live_tail_never_suppressed',
      fn(live, 'T1aaaaaa', { content: 'y'.repeat(100) }) === false);

// 6. No content in payload → nothing to clobber.
check('no_content_payload_ok',
      fn(settled, 'T1aaaaaa', { status: 'interrupted' }) === false);

console.log(out.join('\n'));
"""


def _run(nc: bool):
    harness = os.path.join(HERE, '_interrupted_flicker_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_settled_tail_suppresses_competing_fold():
    """A settled interrupted tail rejects equal/shorter/foreign writes and only
    adopts strict growth — so the two competing folds can't swap the text."""
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'flicker-guard failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_neutered_guard_adopts_competing_fold():
    """Negative control: with the guard neutered (always returns false), the
    equal-length competing fold is NOT suppressed — proving the real guard is
    what stops the flicker."""
    output = _run(nc=True)
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name):
                return ln.split(' ', 1)[0]
        return None

    # With the guard neutered, the equal-length + foreign-task suppressions
    # FLIP to FAIL (the swap would happen), proving the guard is load-bearing.
    assert _status('equal_len_same_task_suppressed') == 'FAIL', \
        'neutered guard must FAIL the equal-length suppression:\n' + output
    assert _status('foreign_task_suppressed') == 'FAIL', \
        'neutered guard must FAIL the foreign-task suppression:\n' + output

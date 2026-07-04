"""Regression: a reconnect `state` snapshot must NOT blank an in-progress
message by overwriting the accumulated buffer with EMPTY/SHORTER content.

WHY
---
On a flaky network, the SSE `state` snapshot replayed on reconnect can land
BEFORE the server has written a fresh 5s checkpoint (cold-replay race / wrong
replica), so its `content`/`thinking` can be empty or shorter than what the
client already accumulated from live deltas. The old handlers assigned it RAW:

    assistantMsg.content = ev.content || "";   // sse_pipeline.js:717 (and 4 more)

which BLANKS the message — the "sent and generating, later found completely
GONE" symptom. `updateStreamingUI`'s wait branch then paints "等待中…" over the
erased content.

The fix adds `_snapshotLonger(msg, ev, field)` — a snapshot may only GROW a
field, never shrink it (same invariant as `_pollFallback`'s merge and the
2026-05-31 content-regression detector) — applied at ALL FIVE state-snapshot
sites (plain assistant, endpoint critic, endpoint worker, and the two planner
sites).

Tests (drive the REAL shipped `_snapshotLonger` under jsdom):
  1. empty/shorter incoming snapshot → keeps the current (longer) message value.
  2. longer incoming snapshot → still adopts it (behaviour preservation — a
     legitimate fuller checkpoint must update).
  3. equal length → adopts incoming (idempotent).
  4. current empty + incoming non-empty → adopts incoming (fresh fill).
  ★ Double-neuter: revert the helper to `return (ev && ev[field]) || ""`
    (raw overwrite) → test #1 FAILS (message blanked). Plus source guards
    asserting all five call sites use `_snapshotLonger`, not raw `ev.content`.
"""

from __future__ import annotations

import os
import re
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
const ROOT = process.argv[2];
const SSE = process.argv[3];

// _snapshotLonger is a pure top-level function in sse_pipeline.js. We only
// need that helper, so extract + eval it in isolation (the rest of the file
// pulls in a large dependency graph we don't want under a unit harness).
const src = fs.readFileSync(SSE, 'utf8');
const m = src.match(/function _snapshotLonger\([\s\S]*?\n}/);
if (!m) { console.log('FAIL helper_not_found'); process.exit(0); }
eval(m[0]);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('helper_defined', typeof _snapshotLonger === 'function');

// 1. Empty incoming snapshot must NOT shrink a non-empty message. ★ THE FIX.
{
  const msg = { content: 'partial answer so far', thinking: 'reasoning' };
  const ev = { content: '', thinking: '' };
  check('empty_incoming_keeps_current',
    _snapshotLonger(msg, ev, 'content') === 'partial answer so far'
    && _snapshotLonger(msg, ev, 'thinking') === 'reasoning');
}

// 1b. Shorter incoming must NOT shrink either.
{
  const msg = { content: 'AAAAAAAAAA' };   // 10 chars
  const ev = { content: 'AAA' };            // 3 chars
  check('shorter_incoming_keeps_current',
    _snapshotLonger(msg, ev, 'content') === 'AAAAAAAAAA');
}

// 2. Longer incoming snapshot IS adopted (behaviour preservation).
{
  const msg = { content: 'short' };
  const ev = { content: 'a much longer and fuller checkpoint value' };
  check('longer_incoming_adopted',
    _snapshotLonger(msg, ev, 'content') === 'a much longer and fuller checkpoint value');
}

// 3. Equal length → adopt incoming (idempotent, >= comparison).
{
  const msg = { content: 'ABCDE' };
  const ev = { content: 'VWXYZ' };
  check('equal_len_adopts_incoming', _snapshotLonger(msg, ev, 'content') === 'VWXYZ');
}

// 4. Empty current + non-empty incoming → adopt incoming (fresh fill).
{
  const msg = { content: '' };
  const ev = { content: 'first checkpoint' };
  check('empty_current_adopts_incoming', _snapshotLonger(msg, ev, 'content') === 'first checkpoint');
}

// 5. Both empty / null-safe.
{
  check('null_safe', _snapshotLonger(null, null, 'content') === ''
    && _snapshotLonger({}, {}, 'content') === '');
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_state_snapshot_keep_longer_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'state-snapshot keep-longer failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'

    # ── Source guards: EVERY state-snapshot overwrite site must route through
    #    _snapshotLonger, not raw `= ev.content || ""`. ──
    with open(os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'), encoding='utf-8') as f:
        sse_src = f.read()

    # The helper itself must exist and implement keep-the-longer.
    assert 'function _snapshotLonger' in sse_src, '_snapshotLonger helper removed'
    assert 'incoming.length >= current.length' in sse_src, (
        'fix regression: _snapshotLonger no longer keeps-the-longer — a shorter '
        'snapshot could blank an in-progress message.')

    # Count call sites — there are 5 (plain assistant, critic, worker, 2 planner).
    call_sites = len(re.findall(r'_snapshotLonger\(\w', sse_src))
    assert call_sites >= 5, (
        f'expected >=5 _snapshotLonger call sites, found {call_sites} — a state '
        'snapshot site may have regressed to a raw `ev.content || ""` overwrite.')

    # No raw-overwrite of a message's content/thinking directly from ev in the
    # state-snapshot region should remain. Assert the specific old-shape lines
    # are gone for the guarded fields.
    for bad in ('assistantMsg.content = ev.content || ""',
                '_epCriticMsg.content = ev.content || ""',
                'workerMsg.content = ev.content || ""'):
        assert bad not in sse_src, (
            f'fix regression: raw state-snapshot overwrite reintroduced: {bad!r}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_state_snapshot_keep_longer():
    _run()


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        _run()
        print('PASS test_state_snapshot_keep_longer')

"""Pin the MONOTONIC KEEP-LONGER invariant that prevents the "generating then
GONE" mid-stream-reconnect bug.

WHY THIS TEST EXISTS
--------------------
On a poor network the SSE stream drops mid-answer and reconnects. The backend
replays a `state` snapshot (Last-Event-ID resume) and, at completion, a `done`
committedMessage. Either can briefly carry a COLD/empty or lagging checkpoint
whose content/thinking/toolRounds are SHORTER than what the client already
accumulated from live deltas. The client applies these via `_snapshotLonger`
(text) and `_snapshotLongerRounds` (tool rounds) in static/js/ui/sse_pipeline.js
at BOTH apply sites (the `state` reconnect snapshot ~:756 and the `done`
committedMessage ~:1557). Their invariant — **a snapshot may only GROW a field,
never SHRINK it** — is the SINGLE guarantee preventing a reconnect from wiping
the partial the user already watched stream in.

The audit flagged this as a latent trap: the invariant is load-bearing but had
NO dedicated regression test, so a future edit that assigns `msg.content =
ev.content` raw (dropping the guard) would silently reintroduce the partial-loss
bug. This test pins the contract against the REAL shipped functions.

NEUTER: replace `_snapshotLonger` with a raw `incoming` return (the naive
assignment) → the shrink case regresses → the test FAILS. Proves the guard does
the work.

Runs the REAL functions under node; skips cleanly without node.
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
SSE_PIPELINE = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# We can't eval the whole sse_pipeline.js (it references many globals at load),
# so we brace-extract just the two pure helper functions and eval those.
def _extract_fn(src: str, name: str) -> str:
    marker = f'function {name}('
    i = src.index(marker)
    # find the opening brace of the body
    b = src.index('{', i)
    depth = 0
    for j in range(b, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise ValueError(f'unbalanced braces extracting {name}')


_HARNESS = r"""
%s
%s

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── _snapshotLonger: content/thinking may only GROW ──
// 1. A SHORTER incoming snapshot must NOT shrink the accumulated field.
check('content_shrink_blocked',
  _snapshotLonger({content: 'the full streamed answer so far'}, {content: 'the'}, 'content')
    === 'the full streamed answer so far');
// 2. A LONGER incoming snapshot IS adopted (forward progress).
check('content_grow_adopted',
  _snapshotLonger({content: 'the'}, {content: 'the full answer'}, 'content') === 'the full answer');
// 3. Equal length → incoming (idempotent, fine).
check('content_equal_ok',
  _snapshotLonger({content: 'abcd'}, {content: 'wxyz'}, 'content') === 'wxyz');
// 4. Missing incoming field → keep current (empty snapshot can't wipe).
check('content_empty_snapshot_keeps',
  _snapshotLonger({content: 'kept'}, {}, 'content') === 'kept');
// 5. thinking field is handled identically.
check('thinking_shrink_blocked',
  _snapshotLonger({thinking: 'long reasoning trace'}, {thinking: 'lo'}, 'thinking')
    === 'long reasoning trace');

// ── _snapshotLongerRounds: toolRounds may only GROW ──
const cur3 = [{n:1},{n:2},{n:3}];
// 6. A shorter/cold reconnect rounds array must NOT collapse the panel.
check('rounds_shrink_blocked',
  _snapshotLongerRounds(cur3, [{n:1}]).length === 3);
// 7. A longer rounds array IS adopted.
check('rounds_grow_adopted',
  _snapshotLongerRounds([{n:1}], [{n:1},{n:2}]).length === 2);
// 8. Empty incoming → keep current.
check('rounds_empty_keeps',
  _snapshotLongerRounds(cur3, []).length === 3);

console.log(out.join('\n'));
"""


def _run_with_src(snapshot_longer_src: str, rounds_src: str) -> str:
    harness = os.path.join(HERE, '_keeplonger_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS % (snapshot_longer_src, rounds_src))
    try:
        proc = subprocess.run(['node', harness], capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_keeplonger_invariant_holds():
    with open(SSE_PIPELINE, encoding='utf-8') as f:
        src = f.read()
    sl = _extract_fn(src, '_snapshotLonger')
    slr = _extract_fn(src, '_snapshotLongerRounds')
    output = _run_with_src(sl, slr)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'keep-longer invariant violated:\n' + output
    assert output.count('PASS') == 8, f'expected 8 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_raw_assignment_reintroduces_partial_loss():
    """NEUTER: the naive 'assign incoming raw' version (what a careless future
    edit would write) must FAIL the shrink-blocked checks — proving the
    keep-longer guard is load-bearing, not incidental."""
    with open(SSE_PIPELINE, encoding='utf-8') as f:
        src = f.read()
    slr = _extract_fn(src, '_snapshotLongerRounds')
    # Naive replacement: return the incoming field verbatim (the bug).
    naive_sl = ("function _snapshotLonger(msg, ev, field) { "
                "return (ev && ev[field]) || ''; }")
    output = _run_with_src(naive_sl, slr)
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('content_shrink_blocked') is False, (
        'NEUTER did not bite: raw assignment still passed shrink-blocked — '
        'the test does not actually pin the guard.\n' + output)
    assert lines.get('thinking_shrink_blocked') is False, output
    # Forward-progress cases still pass under the naive version (it adopts incoming).
    assert lines.get('content_grow_adopted') is True, output

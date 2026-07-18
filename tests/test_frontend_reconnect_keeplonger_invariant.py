"""Pin the post-reorder TWO-TIER state-snapshot contract.

WHY THIS TEST EXISTS
--------------------
On a poor network the SSE stream drops mid-answer and reconnects; the backend
replays a `state` snapshot. Historically its content/thinking/toolRounds could
be SHORTER than what the client accumulated from live deltas, so the client kept
BOTH a `_snapshotLonger` (text) and a `_snapshotLongerRounds` (rounds)
keep-longer belt — "a snapshot may only GROW, never shrink."

THE CONTRACT CHANGED (2026-07-11) — TEXT is now backend-authoritative
---------------------------------------------------------------------
Two root fixes made the state snapshot's TEXT never trail the client, so the
text belt was RETIRED and the 5 state sites now project content/thinking
VERBATIM (`msg.content = ev.content || ""`):
  1. FOLD AT SOURCE — the 3 cold `content` paths (SSE gen_persisted, SSE
     gen_done, poll DB-fallback) fold the lossless per-delta `task_events` log
     (lib/tasks_pkg/event_fold.py). See tests/test_event_fold_cold_replay.py.
  2. DURABLE-BEFORE-VISIBLE — `manager.append_event` persists each event row
     BEFORE pushing the frame (lib/agent_core/task_runtime.py `before_push`
     hook), so the fold is never behind the client buffer. See
     tests/test_event_persist_before_push.py.

toolRounds is the REMAINING residual: on every cold path it is still sourced
from the 5s `task_results.tool_rounds` checkpoint / the conversation (NOT the
delta fold — reconstructing rounds needs the tool_start/tool_done choreography,
owned by segment-timeline epic pt_cb8f98b0cb9b47fb). So `_snapshotLongerRounds`
STAYS load-bearing and this test pins it.

This test now guards:
  A. TEXT belt REMOVED — `_snapshotLonger` is gone AND the 5 state sites assign
     content/thinking verbatim (no keep-longer call for text). Removal tripwire.
  B. ROUNDS belt HELD — `_snapshotLongerRounds` still refuses to shrink, with a
     biting neuter proving it's load-bearing.

Runs the REAL functions under node; skips cleanly without node.
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
SSE_PIPELINE = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    marker = f'function {name}('
    i = src.index(marker)
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


_ROUNDS_HARNESS = r"""
%s

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── _snapshotLongerRounds: toolRounds may only GROW (residual belt) ──
const cur3 = [{n:1},{n:2},{n:3}];
// 1. A shorter/cold reconnect rounds array must NOT collapse the panel.
check('rounds_shrink_blocked', _snapshotLongerRounds(cur3, [{n:1}]).length === 3);
// 2. A longer rounds array IS adopted.
check('rounds_grow_adopted', _snapshotLongerRounds([{n:1}], [{n:1},{n:2}]).length === 2);
// 3. Empty incoming → keep current.
check('rounds_empty_keeps', _snapshotLongerRounds(cur3, []).length === 3);

console.log(out.join('\n'));
"""


def _run_rounds(rounds_src: str) -> str:
    harness = os.path.join(HERE, '_keeplonger_harness.js')
    with open(harness, 'w') as f:
        f.write(_ROUNDS_HARNESS % rounds_src)
    try:
        proc = subprocess.run(['node', harness], capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_text_keeplonger_belt_removed():
    """A — the TEXT belt is gone and the state sites project text verbatim."""
    with open(SSE_PIPELINE, encoding='utf-8') as f:
        src = f.read()
    # 1. The _snapshotLonger (text) helper is removed entirely.
    assert 'function _snapshotLonger(' not in src, (
        '_snapshotLonger (text keep-longer belt) is still defined — it must be '
        'removed now that the server fold + persist-before-push make state-'
        'snapshot text backend-authoritative')
    assert '_snapshotLonger(' not in src, (
        'a _snapshotLonger(...) call site survives — every state site must '
        'assign content/thinking verbatim')
    # 2. The state sites assign content/thinking verbatim from the event.
    #    (At least the plain-assistant + worker + planner + critic sites.)
    verbatim_content = len(re.findall(r'\.content = ev\.content \|\| ""', src))
    verbatim_thinking = len(re.findall(r'\.thinking = ev\.thinking \|\| ""', src))
    assert verbatim_content >= 4, (
        f'expected >=4 verbatim content assignments at the state sites, '
        f'found {verbatim_content}')
    assert verbatim_thinking >= 4, (
        f'expected >=4 verbatim thinking assignments, found {verbatim_thinking}')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rounds_keeplonger_belt_holds():
    """B — the ROUNDS belt still refuses to shrink (residual, load-bearing)."""
    with open(SSE_PIPELINE, encoding='utf-8') as f:
        src = f.read()
    slr = _extract_fn(src, '_snapshotLongerRounds')
    output = _run_rounds(slr)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'rounds keep-longer invariant violated:\n' + output
    assert output.count('PASS') == 3, f'expected 3 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_rounds_raw_assignment_reintroduces_collapse():
    """NEUTER: a naive 'return incoming raw' rounds version must FAIL the
    shrink-blocked check — proving the rounds guard is load-bearing."""
    naive = ("function _snapshotLongerRounds(current, incoming) { "
             "return Array.isArray(incoming) ? incoming : []; }")
    output = _run_rounds(naive)
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('rounds_shrink_blocked') is False, (
        'NEUTER did not bite: raw rounds assignment still passed shrink-blocked '
        '— the test does not pin the rounds guard.\n' + output)
    assert lines.get('rounds_grow_adopted') is True, output

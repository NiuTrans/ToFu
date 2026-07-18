"""tests/test_frontend_prior_turn_reducer.py — regression for the UNIFIED
"assistant tail belongs to a prior turn?" reducer (`assistantTailIsPriorTurn`).

WHY
---
When a task's SSE stream connects (ui/sse_pipeline.js) or is reconnected on
startup (main/main_init_tasks.js Case A), the tail assistant message might be
the PREVIOUS, completed turn rather than an empty placeholder. Streaming into
it replays the old turn's content into the new bubble ("上一轮对话又重新流式
吐出"). The guard pushes a fresh placeholder — but ONLY when the tail is truly a
prior turn.

The decision reads BACKEND-ISSUED FACTS only: `_taskId` (task→msg bind from the
SSE `state`/poll payload; the backend keys segment recovery on it) and
`finishReason` (from the done/poll payload). It is a pure equality/presence
reducer over those facts — the front/back-contract invariant's PRESCRIBED shape
for placement (server-assigned id, not transient client state), NOT the retired
ghost-classifier inference.

FE inference-debt #2 verdict: the "backend-issued taskId→msgId bind" the epic
asked for already EXISTS (`_taskId`). The genuine remaining debt was that the
identical predicate was DUPLICATED at the two connect sites and could drift.
This test locks in ONE canonical reducer (core/conversations.js) that both call
sites use.

Slices the REAL shipped reducer verbatim, runs under node, drives the truth
table + a neuter proving the stale-taskId equality is load-bearing.
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
CONV_JS = os.path.join(ROOT, 'static', 'js', 'core', 'conversations.js')
SSE_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
INIT_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_init_tasks.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_reducer(src_text: str) -> str:
    """Slice the `assistantTailIsPriorTurn` fn body (through its closing brace)."""
    sig = 'function assistantTailIsPriorTurn(msg, activeTaskId) {\n'
    start = src_text.index(sig)
    end = src_text.index("\n}\n", start) + len("\n}\n")
    return src_text[start:end]


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const REDUCER = fs.readFileSync(process.argv[2], 'utf8');
eval(REDUCER);   // defines assistantTailIsPriorTurn in this scope

const f = assistantTailIsPriorTurn;

// ── truth table (activeTaskId = 'T2') ──
// 1. empty placeholder owned by the CURRENT task → NOT prior (stream into it)
check('current_task_empty_not_prior',
  f({ role: 'assistant', _taskId: 'T2', content: '' }, 'T2') === false);
// 2. tail owned by a DIFFERENT task → prior (push fresh)
check('different_task_is_prior',
  f({ role: 'assistant', _taskId: 'T1', content: 'old' }, 'T2') === true);
// 3. tail already has finishReason (completed) → prior even if same/no taskId
check('finishreason_is_prior',
  f({ role: 'assistant', finishReason: 'stop', content: 'done' }, 'T2') === true);
check('finishreason_same_task_is_prior',
  f({ role: 'assistant', _taskId: 'T2', finishReason: 'stop' }, 'T2') === true);
// 4. no _taskId, no finishReason (fresh in-flight placeholder) → NOT prior
check('no_facts_not_prior',
  f({ role: 'assistant', content: '' }, 'T2') === false);
// 5. non-assistant tail → NOT prior (never our target)
check('user_tail_not_prior',
  f({ role: 'user', content: 'hi', _taskId: 'T1' }, 'T2') === false);
// 6. undefined / null tail → NOT prior (defensive)
check('undefined_not_prior', f(undefined, 'T2') === false);
check('null_not_prior', f(null, 'T2') === false);
// 7. _taskId present but EQUAL to active → NOT prior (same live turn)
check('same_task_no_finish_not_prior',
  f({ role: 'assistant', _taskId: 'T2' }, 'T2') === false);

console.log(out.join('\n'));
"""


def _run(reducer_text: str, tag: str) -> str:
    red = os.path.join(HERE, f'_ptr_reducer_{tag}.js')
    harness = os.path.join(HERE, f'_ptr_harness_{tag}.js')
    with open(red, 'w', encoding='utf-8') as fh:
        fh.write(reducer_text)
    with open(harness, 'w', encoding='utf-8') as fh:
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, red],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (red, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_prior_turn_reducer_truth_table():
    with open(CONV_JS, encoding='utf-8') as f:
        reducer = _extract_reducer(f.read())
    out = _run(reducer, 'real')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'prior-turn reducer failures:\n' + out
    assert out.count('PASS') >= 9, f'expected >=9 PASS:\n{out}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_prior_turn_reducer_neuter_drops_stale_taskid():
    """NEUTER: force _staleTaskId to false → a tail owned by a DIFFERENT task is
    no longer recognized as prior. Proves the taskId equality is load-bearing
    (its removal re-introduces the old-content-replay bug)."""
    with open(CONV_JS, encoding='utf-8') as f:
        reducer = _extract_reducer(f.read())
    neutered = reducer.replace(
        "const _staleTaskId = !!(msg._taskId && msg._taskId !== activeTaskId);",
        "const _staleTaskId = false;", 1)
    assert neutered != reducer, 'neuter did not modify the reducer'
    out = _run(neutered, 'neuter')
    assert 'FAIL different_task_is_prior' in out, \
        'NC (stale-taskId disabled) should fail different_task_is_prior:\n' + out
    # finishReason path is unaffected by this neuter.
    assert 'PASS finishreason_is_prior' in out, out


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_both_call_sites_use_the_shared_reducer():
    """Lock in single-source-of-truth: both connect sites must call
    assistantTailIsPriorTurn and must NOT re-inline the old duplicated
    predicate (`msg._taskId !== ... || finishReason`)."""
    with open(SSE_JS, encoding='utf-8') as f:
        sse = f.read()
    with open(INIT_JS, encoding='utf-8') as f:
        init = f.read()
    assert 'assistantTailIsPriorTurn(assistantMsg, taskId)' in sse, \
        'sse_pipeline.js must call the shared reducer'
    assert 'assistantTailIsPriorTurn(_amA, conv.activeTaskId)' in init, \
        'main_init_tasks.js Case A must call the shared reducer'
    # The old duplicated inline predicate must be gone from BOTH connect sites.
    dup = re.compile(r"_taskId\s*&&\s*\w+\._taskId\s*!==\s*\w+\)\s*\|\|\s*!!\w+\.finishReason")
    assert not dup.search(sse), 'sse_pipeline.js still inlines the duplicated predicate'
    assert not dup.search(init), 'main_init_tasks.js still inlines the duplicated predicate'

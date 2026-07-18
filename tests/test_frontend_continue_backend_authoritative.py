"""Regression: the Continue resume point is BACKEND-authoritative.

WHY
---
`continueAssistant()` (static/js/main/main_regen_continue.js) used to RE-DERIVE
the resume checkpoint on the client by scanning `toolRounds` for
`status === 'done'`, grouping batches, and computing preserved/discarded
content + thinking — the EXACT logic the backend already runs in
`scan_continue_checkpoint()` (lib/chat/turn_builder.py), applies, and PERSISTS
in `POST /api/chat/continue`. That duplicated client-side lifecycle inference
violates the project invariant: a lifecycle/placement decision is computed by
the BACKEND and shipped to the frontend as a typed fact; the frontend is a pure
reducer over it (never infers it from transient client state).

THE FIX
-------
The endpoint now returns the authoritative anchor DATA in `data.checkpoint`
(`resumeMode`, `keptRounds` COUNT, `contentPrefix`, `priorContent`,
`priorThinking`). The frontend POSTs first, then REDUCES its local message over
that fact via the pure `_applyContinueCheckpoint(assistantMsg, allRounds, ckpt)`
— slicing rounds to the server-decided COUNT and adopting the server strings
verbatim. No `status === 'done'` scan remains.

This test drives the REAL shipped `_applyContinueCheckpoint` under node.
NEUTER: a fact that keeps only 1 of 3 rounds must roll the local message back
to exactly 1 round with the server's contentPrefix — proving the reducer OBEYS
the fact rather than keeping all local rounds.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
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
SRC_JS = os.path.join(JS_DIR, 'main', 'main_regen_continue.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Extract ONLY the pure reducer function from the shipped file (the rest of the
# file references many boot globals we don't want to stub). We eval just the
# self-contained _applyContinueCheckpoint + its window assignment.
def _extract_reducer(src: str) -> str:
    start = src.index('function _applyContinueCheckpoint(')
    end_marker = 'window._applyContinueCheckpoint = _applyContinueCheckpoint;'
    end = src.index(end_marker) + len(end_marker)
    return src[start:end]


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const reducerSrc = fs.readFileSync(process.argv[2], 'utf8');
eval(reducerSrc);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _applyContinueCheckpoint !== 'function') {
  console.log('FAIL fn_exposed _applyContinueCheckpoint missing');
  process.exit(0);
}
check('fn_exposed', true);

// ── Scenario A: checkpoint mode, server says keep 1 of 3 rounds ──
// Local message has 3 rounds + a longer content tail; the server fact decides
// the boundary. A pure reducer must SLICE to keptRounds=1 and adopt the server
// contentPrefix/priorContent verbatim — NOT keep all 3 local rounds.
const allRounds = [
  { toolCallId: 'a', toolName: 'web_search', status: 'done', roundNum: 1, llmRound: 0, assistantContent: 'R1' },
  { toolCallId: 'b', toolName: 'read_files', status: 'done', roundNum: 2, llmRound: 1, assistantContent: 'R2' },
  { toolCallId: 'c', toolName: 'apply_diff', status: 'running', roundNum: 3, llmRound: 2 },
];
const msgA = { role: 'assistant', content: 'R1 full answer tail that gets rolled back',
               thinking: 'live thinking tail', toolRounds: allRounds.slice(),
               finishReason: 'length', error: 'stale' };
const ckptA = {
  resumeMode: 'checkpoint', keptRounds: 1, discardedRounds: 2,
  contentPrefix: 'R1', priorContent: ' full answer tail that gets rolled back',
  priorThinking: 'live thinking tail',
  preservedContentLen: 2, discardedContentLen: 39, preservedThinkingChars: 0, discardedThinking: 18,
};
const keptA = _applyContinueCheckpoint(msgA, allRounds, ckptA);
check('A_sliced_to_server_count', msgA.toolRounds.length === 1);
check('A_kept_return_matches', Array.isArray(keptA) && keptA.length === 1);
check('A_content_is_server_prefix', msgA.content === 'R1');
check('A_thinking_cleared', msgA.thinking === '');
check('A_priorContent_from_fact', msgA.priorContent === ' full answer tail that gets rolled back');
check('A_priorThinking_from_fact', msgA.priorThinking === 'live thinking tail');
check('A_stale_meta_cleared', msgA.finishReason === undefined && msgA.error === undefined);
// Streaming-merge seed set from the server-decided kept rounds.
check('A_merge_seed_rounds', Array.isArray(msgA._continueToolRounds) && msgA._continueToolRounds.length === 1);
check('A_merge_seed_prefix', msgA._continueContentPrefix === 'R1');

// ── Scenario B: prefill mode keeps ALL rounds + full content, no priorContent ──
const roundsB = [
  { toolCallId: 'a', toolName: 'web_search', status: 'done', roundNum: 1, llmRound: 0 },
];
const msgB = { role: 'assistant', content: 'half-written answer', thinking: 'tail',
               toolRounds: roundsB.slice() };
const ckptB = {
  resumeMode: 'prefill', keptRounds: 0,
  contentPrefix: 'half-written answer', priorContent: '', priorThinking: 'tail',
  preservedContentLen: 19, discardedContentLen: 0,
};
_applyContinueCheckpoint(msgB, roundsB, ckptB);
check('B_prefill_keeps_rounds', msgB.toolRounds.length === 1);
check('B_prefill_full_content', msgB.content === 'half-written answer');
check('B_prefill_no_priorContent', msgB.priorContent === undefined);
check('B_prefill_priorThinking', msgB.priorThinking === 'tail');

console.log('KEPT_A=' + keptA.length);
console.log(out.join('\n'));
"""


def _run(reducer_path: str):
    harness = os.path.join(HERE, '_continue_reducer_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, reducer_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reducer_obeys_backend_checkpoint_fact(tmp_path):
    with open(SRC_JS, encoding='utf-8') as f:
        reducer = _extract_reducer(f.read())
    rfile = tmp_path / 'reducer.js'
    rfile.write_text(reducer, encoding='utf-8')
    proc = _run(str(rfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Continue reducer is not a faithful pure reducer:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_no_client_side_status_done_scan():
    """The client must NOT re-derive the checkpoint by scanning toolRounds for
    status==='done'. Guard against reintroducing the duplicated lifecycle scan
    inside continueAssistant (doc-comment mentions are allowed)."""
    with open(SRC_JS, encoding='utf-8') as f:
        src = f.read()
    fn_start = src.index('async function continueAssistant()')
    fn_body = src[fn_start:]
    # No executable status==='done' comparison, no batch-grouping vars.
    assert not re.search(r'status\s*===\s*["\']done["\']', fn_body), (
        'continueAssistant re-introduced a client-side status===done scan — the '
        'checkpoint must come from the backend fact (data.checkpoint).')
    for banned in ('lastCompleteIdx', 'roundBatchMap', 'batches.set('):
        assert banned not in fn_body, (
            f'continueAssistant re-introduced client checkpoint derivation ({banned}).')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reducer_neuter_keeps_all_rounds_fails(tmp_path):
    """NEUTER: replace the server-count slice with 'keep all local rounds'
    (the old behaviour of ignoring the fact) and prove Scenario A then FAILS —
    i.e. the test genuinely discriminates that the reducer OBEYS the fact."""
    with open(SRC_JS, encoding='utf-8') as f:
        reducer = _extract_reducer(f.read())
    # Neuter: force keptRounds to always be the full local list, ignoring the
    # server-decided count.
    anchor = "    : (allRounds || []).slice(0, keptCount);"
    assert anchor in reducer, 'reducer slice anchor not found — update the neuter target'
    neutered = reducer.replace(anchor, "    : (allRounds || []).slice();  // NEUTER: ignore server count")
    assert neutered != reducer
    rfile = tmp_path / 'reducer_neutered.js'
    rfile.write_text(neutered, encoding='utf-8')
    proc = _run(str(rfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('A_sliced_to_server_count') is False, (
        'NEUTER did not bite: ignoring the server keptRounds count still passed '
        'the slice check — the test does not discriminate backend authority.\n'
        + output)

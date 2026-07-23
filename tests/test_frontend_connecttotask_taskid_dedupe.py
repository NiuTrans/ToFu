"""tests/test_frontend_connecttotask_taskid_dedupe.py — regression for the
"one user → two assistant bubbles" duplicate-bubble class.

WHY
---
`connectToTask` (`static/js/ui/sse_pipeline.js`) resolved the assistant slot to
accumulate into by ARRAY POSITION (`conv.messages[length-1]`), then a
"stale-prior-turn guard" pushed a FRESH empty placeholder whenever the tail
carried a `finishReason`.  On a reconnect / poll-takeover to a task that had
ALREADY finished (its own assistant slot has `finishReason` + `_taskId`, and
`finishStream` already deleted the `activeStreams` entry so there is no stream
to re-target), that guard fired for the task's OWN completed reply and appended
a SECOND assistant bubble → a user followed by two agents.

THE FIX (identity-first, matched here against the shipped source):
  1. `_resolveAssistantByTaskId(conv, taskId)` — resolve the slot already
     BOUND to this taskId, tail-up, before the positional fallback.
  2. The stale-guard's completed-turn test is now
     `finishReason && _taskId && _taskId !== taskId` — a completed tail is
     stale ONLY when it belongs to a DIFFERENT task, never this task's own.
  3. `_taskId` is stamped on the slot at stream-bind time so any later
     reconnect resolves by identity.

This harness re-implements the REAL resolution + stale-guard decision (kept
byte-faithful to the source predicates) and drives the reconnect-to-finished
race, asserting NO second assistant is appended.  A NEUTER reverts predicate
(2) to the old `!!finishReason` and proves the duplicate returns.

Guards checked against the actual source text so the test rots with the code:
the harness asserts the shipped file contains `_resolveAssistantByTaskId(` and
the `_taskId !== taskId` completed-turn predicate.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SSE = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
CL = os.path.join(ROOT, 'static', 'js', 'ui', 'conversation_list.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Harness ──────────────────────────────────────────────────────────────
# `mode` selects the completed-turn predicate:
#   'fixed'  → finishReason && _taskId && _taskId !== taskId  (shipped fix)
#   'neuter' → !!finishReason                                  (old buggy)
# The rest of the resolution logic mirrors the shipped connectToTask verbatim.
_HARNESS = r"""
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Real resolver from conversation_list.js (behaviourally identical slice).
function _resolveAssistantByTaskId(conv, taskId) {
  if (!conv || !taskId || !Array.isArray(conv.messages)) return null;
  const msgs = conv.messages;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && m.role === 'assistant' && m._taskId === taskId) return m;
  }
  return null;
}

// Mirrors connectToTask's target-resolution + stale-guard decision.
// Returns {appended:bool, target:msg} — appended=true means a SECOND assistant
// placeholder was pushed (the duplicate-bubble bug).
function resolveTarget(conv, taskId, opts) {
  const mode = opts.mode;
  const hasActiveStream = opts.hasActiveStream;  // finishStream cleared it → false on reconnect
  let assistantMsg = null;

  // (existing-stream stable-id re-target — skipped when finishStream cleared it)
  if (hasActiveStream && opts.streamMsgId) {
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      if (conv.messages[i]._msgId === opts.streamMsgId) { assistantMsg = conv.messages[i]; break; }
    }
  }
  // ★ identity-first resolution (the fix)
  if (!assistantMsg && mode !== 'no_taskid_resolver') {
    const byTask = _resolveAssistantByTaskId(conv, taskId);
    if (byTask) assistantMsg = byTask;
  }
  if (!assistantMsg) assistantMsg = conv.messages[conv.messages.length - 1];

  let appended = false;
  // stale-prior-turn guard (non-endpoint)
  if (assistantMsg && assistantMsg.role === 'assistant'
      && !conv.messages.some(m => m._epIteration)) {
    const _staleTaskId = assistantMsg._taskId && assistantMsg._taskId !== taskId;
    let _isCompletedTurn;
    if (mode === 'neuter') {
      _isCompletedTurn = !!assistantMsg.finishReason;               // OLD buggy predicate
    } else if (mode === 'overnarrow') {
      // The rejected intermediate: required _taskId truthy → a reloaded
      // foreign completed tail (no _taskId, not persisted) wrongly REUSED.
      _isCompletedTurn = !!assistantMsg.finishReason
        && assistantMsg._taskId && assistantMsg._taskId !== taskId;
    } else {
      _isCompletedTurn = !!assistantMsg.finishReason
        && assistantMsg._taskId !== taskId;                        // shipped fix
    }
    if (_staleTaskId || _isCompletedTurn) {
      assistantMsg = { role: 'assistant', content: '', thinking: '', toolRounds: [], _fresh: true };
      conv.messages.push(assistantMsg);
      appended = true;
    }
  }
  return { appended, target: assistantMsg };
}

function assistantCount(conv) { return conv.messages.filter(m => m.role === 'assistant').length; }

// ── Scenario A: reconnect to a JUST-FINISHED task (the reported bug). ──
// conv tail = the task's own completed assistant (finishReason + _taskId=T),
// finishStream already cleared activeStreams (hasActiveStream=false).
function scenarioConv() {
  return { id: 'c1', messages: [
    { role: 'user', content: 'hi', _msgId: 'u1' },
    { role: 'assistant', content: 'done reply', finishReason: 'stop', _taskId: 'T', _msgId: 'a1' },
  ] };
}

(function () {
  const conv = scenarioConv();
  const r = resolveTarget(conv, 'T', { mode: 'fixed', hasActiveStream: false });
  check('fixed_reconnect_no_append', r.appended === false);
  check('fixed_reconnect_single_assistant', assistantCount(conv) === 1);
  check('fixed_reconnect_retargets_own_slot', r.target && r.target._msgId === 'a1');
})();

// ── Scenario B: a DIFFERENT completed turn precedes a new task → must append. ──
(function () {
  const conv = { id: 'c2', messages: [
    { role: 'user', content: 'q1', _msgId: 'u1' },
    { role: 'assistant', content: 'old', finishReason: 'stop', _taskId: 'T_OLD', _msgId: 'a_old' },
  ] };
  const r = resolveTarget(conv, 'T_NEW', { mode: 'fixed', hasActiveStream: false });
  check('fixed_different_task_appends', r.appended === true);
  check('fixed_different_task_two_assistants', assistantCount(conv) === 2);
})();

// ── Scenario C: fresh send — tail is a brand-new empty placeholder (no _taskId,
//    no finishReason). Must reuse it, never append. ──
(function () {
  const conv = { id: 'c3', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    { role: 'assistant', content: '', toolRounds: [], _msgId: 'a_new' },
  ] };
  const r = resolveTarget(conv, 'T', { mode: 'fixed', hasActiveStream: false });
  check('fixed_fresh_send_no_append', r.appended === false);
  check('fixed_fresh_send_single_assistant', assistantCount(conv) === 1);
})();

// ── Scenario D: PAGE-RELOAD foreign completed tail with NO _taskId (the
//    peer-FYI gap: _taskId is NOT persisted to the DB, so a reloaded completed
//    tail lacks it). A NEW task must push fresh, never replay the old turn. ──
function reloadConv() {
  return { id: 'c4', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    // DB-loaded prior completed turn: finishReason present, _taskId ABSENT.
    { role: 'assistant', content: 'old reply', finishReason: 'stop', _msgId: 'a_dbold' },
  ] };
}
(function () {
  const conv = reloadConv();
  const r = resolveTarget(conv, 'T_NEW', { mode: 'fixed', hasActiveStream: false });
  check('fixed_reload_no_taskid_appends_fresh', r.appended === true);
  check('fixed_reload_no_taskid_two_assistants', assistantCount(conv) === 2);
  check('fixed_reload_no_taskid_target_is_fresh', r.target && r.target._fresh === true);
})();

// ── NEUTER: old predicate on Scenario A → duplicate bubble returns. ──
(function () {
  const conv = scenarioConv();
  const r = resolveTarget(conv, 'T', { mode: 'neuter', hasActiveStream: false });
  check('neuter_reconnect_appends_duplicate', r.appended === true);
  check('neuter_reconnect_two_assistants', assistantCount(conv) === 2);
})();

// ── OVER-NARROW NEUTER: the rejected `_taskId &&` intermediate reuses a
//    reloaded foreign completed tail (no _taskId) → old turn replays, NOT
//    appended. Proves the `_taskId &&` clause was harmful for the reload case. ──
(function () {
  const conv = reloadConv();
  const r = resolveTarget(conv, 'T_NEW', { mode: 'overnarrow', hasActiveStream: false });
  check('overnarrow_reload_wrongly_reuses', r.appended === false);
  check('overnarrow_reload_target_is_old', r.target && r.target._msgId === 'a_dbold');
})();

console.log(out.join('\n'));
"""


def _run() -> str:
    harness = os.path.join(HERE, '_cttd_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_taskid_dedupe_collapses_duplicate_and_preserves_append():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dedupe decision failures:\n' + output
    # The NEUTER lines below prove the fixed predicate is load-bearing:
    assert 'PASS neuter_reconnect_appends_duplicate' in output, output
    assert 'PASS neuter_reconnect_two_assistants' in output, output
    # The FIX lines prove the duplicate is collapsed while a genuinely new
    # turn still appends:
    assert 'PASS fixed_reconnect_no_append' in output, output
    assert 'PASS fixed_reconnect_single_assistant' in output, output
    assert 'PASS fixed_different_task_appends' in output, output
    assert 'PASS fixed_fresh_send_no_append' in output, output
    # Scenario D (peer-FYI gap): a reloaded foreign completed tail with NO
    # _taskId must still push fresh — reusing it would replay the old turn.
    assert 'PASS fixed_reload_no_taskid_appends_fresh' in output, output
    assert 'PASS fixed_reload_no_taskid_two_assistants' in output, output
    # OVER-NARROW neuter proves the rejected `_taskId &&` clause regressed the
    # reload case (wrongly reused the old turn):
    assert 'PASS overnarrow_reload_wrongly_reuses' in output, output
    assert 'PASS overnarrow_reload_target_is_old' in output, output


def test_source_carries_identity_first_resolution():
    """The shipped source must actually contain the identity-first resolver
    and the DIFFERENT-task completed-turn predicate — so this regression rots
    with the code, not just with the harness copy."""
    with open(CL, encoding='utf-8') as f:
        cl_src = f.read()
    assert 'function _resolveAssistantByTaskId(' in cl_src, \
        '_resolveAssistantByTaskId resolver missing from conversation_list.js'

    with open(SSE, encoding='utf-8') as f:
        sse_src = f.read()
    assert '_resolveAssistantByTaskId(conv, taskId)' in sse_src, \
        'connectToTask no longer calls _resolveAssistantByTaskId (identity-first resolution removed)'
    # The completed-turn predicate must reuse ONLY this task's own slot
    # (_taskId === taskId); anything else (different OR absent _taskId) pushes
    # fresh. The `_taskId &&` truthiness clause must NOT be present (it regressed
    # the page-reload no-_taskId case).
    assert "assistantMsg.finishReason\n      && assistantMsg._taskId !== taskId" in sse_src, \
        'stale-guard completed-turn predicate is not the refined `_taskId !== taskId` form — reload-safe dedupe fix reverted'
    # Scoped to the COMPLETED-TURN predicate only (preceded by finishReason);
    # the separate `_staleTaskId` line legitimately keeps `_taskId && ... !== taskId`.
    assert "assistantMsg.finishReason\n      && assistantMsg._taskId && assistantMsg._taskId !== taskId" not in sse_src, \
        'the over-narrow `_taskId && ... !== taskId` clause is back on the completed-turn predicate — it wrongly reuses a reloaded foreign completed tail (no persisted _taskId)'
    assert 'if (assistantMsg && !assistantMsg._taskId) assistantMsg._taskId = taskId;' in sse_src, \
        'bind-time _taskId stamp missing — reconnect after finishStream cannot resolve by identity'

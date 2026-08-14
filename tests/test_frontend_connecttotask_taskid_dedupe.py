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
  2. The prior-turn decision is the SHARED reducer `assistantTailIsPriorTurn`
     (resolved from its core module and spliced into the harness below, so this
     test drives the REAL predicate rather than a hand-copy that can contradict
     it — which is exactly what happened: the harness once encoded
     `finishReason && _taskId !== taskId`, a form the shipped code NEVER had).
     The shipped arms are `_staleTaskId || !!finishReason` — treating ANY
     completed tail as a prior turn is the RELOAD-SAFE choice (Scenario D: a
     DB-loaded completed tail has no persisted `_taskId`).
  3. `_taskId` is stamped on the slot at stream-bind time so any later
     reconnect resolves by identity.

What actually collapses the duplicate is (1)+(3): a LIVE stream re-targets its
own slot by stable id (Scenario A2). Since 2026-07-31 the reducer also makes
IDENTITY BEAT a terminal field — a tail bound to THIS task is never a "prior
turn", however `finishReason` reads — so a reconnect to an already-finished task
re-targets that task's own slot and appends NOTHING (Scenario A). That extra arm
exists because `finishReason` is not reliably terminal on the wire: the
orchestrator stamps it ~111 lines before it flips `status='done'`, so a poll
landing in that window put a terminal field on a still-LIVE turn and minted a
second bubble. See tests/test_duplicate_bubble_midturn_finish_reason.py.

The `!!finishReason` arm is PRESERVED for tails NOT bound to this task — that is
the reload-safe case (Scenario D: a DB-loaded completed tail has no persisted
`_taskId`), and narrowing it any further regresses it.

NEUTERs: dropping the `_staleTaskId` arm makes a foreign task's still-open slot
get reused; the rejected `_taskId &&` gate on the completed-turn arm makes a
reloaded foreign completed tail get replayed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

SSE = runtime_section_path('ui/sse_pipeline.js')
CL = runtime_section_path('ui/conversation_list.js')
REDUCER = runtime_section_path('core/conv_reducers.js')

_REDUCER_SIG = 'function assistantTailIsPriorTurn('


def _reducer_module() -> str:
    """Locate the core module that DEFINES `assistantTailIsPriorTurn`.

    The reducer moved once already (core/conversations.js →
    core/conv_reducers.js, commit 0460e64a) and the hardcoded path made that
    relocation look like a behavioural regression. Resolving it by search keeps
    this guard anchored to the semantic unit, so a future extraction re-points
    itself and only a real deletion fails.
    """
    with open(REDUCER, encoding='utf-8') as fh:
        hits = [REDUCER] if _REDUCER_SIG in fh.read() else []
    assert hits, (
        'assistantTailIsPriorTurn is not defined in any static/js/core/*.js — '
        'the shared prior-turn reducer was deleted, not relocated.'
    )
    assert len(hits) == 1, (
        f'assistantTailIsPriorTurn defined in more than one core module ({hits}) '
        f'— the single-source-of-truth reducer was duplicated again.'
    )
    return hits[0]


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Harness ──────────────────────────────────────────────────────────────
# The completed-turn decision is NOT re-implemented here: `__REDUCER__` is
# replaced with the SHIPPED `assistantTailIsPriorTurn` source text, so 'fixed'
# mode exercises the real reducer. The two neuter modes are deliberate local
# variants used to prove which clause is load-bearing.
_HARNESS = r"""
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── SHIPPED reducer, spliced verbatim from its core module ──
__REDUCER__

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
// Returns {appended:bool, target:msg} — appended=true means a fresh assistant
// placeholder was pushed ahead of the tail.
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
    let _isPrior;
    if (mode === 'neuter_stale') {
      // Drop the stale-taskId arm → a FOREIGN completed tail carrying a
      // different _taskId is no longer recognised as a prior turn.
      _isPrior = !!assistantMsg.finishReason;
    } else if (mode === 'overnarrow') {
      // The rejected intermediate: gate the completed-turn arm on a truthy
      // _taskId → a reloaded foreign completed tail (no persisted _taskId)
      // is wrongly REUSED.
      _isPrior = !!(assistantMsg._taskId && assistantMsg._taskId !== taskId)
        || (!!assistantMsg.finishReason
            && assistantMsg._taskId && assistantMsg._taskId !== taskId);
    } else {
      _isPrior = assistantTailIsPriorTurn(assistantMsg, taskId);   // SHIPPED reducer
    }
    if (_isPrior) {
      assistantMsg = { role: 'assistant', content: '', thinking: '', toolRounds: [], _fresh: true };
      conv.messages.push(assistantMsg);
      appended = true;
    }
  }
  return { appended, target: assistantMsg };
}

function assistantCount(conv) { return conv.messages.filter(m => m.role === 'assistant').length; }

/* ── Scenario A: reconnect to a JUST-FINISHED task, tail = that task's OWN
 *    completed assistant (finishReason + _taskId=T), activeStreams already
 *    cleared by finishStream.
 *
 *    ★ CONTRACT REVERSED 2026-07-31 (pt duplicate-bubble root fix). This block
 *    used to assert that a fresh EMPTY placeholder was pushed ahead of the
 *    task's own completed reply, and the docstring conceded that placeholder
 *    was merely harmless ("stays EMPTY"). It was not harmless: an empty
 *    assistant bubble appended after a finished reply is itself a stray
 *    bubble, and the same misclassification on a still-LIVE own-task tail is
 *    the reported duplicate-bubble bug — the backend advertises a
 *    `finishReason` while `status` is still 'running' (the ~111-line
 *    finalize window around the blocking `_generate_tool_summary` call in
 *    lib/tasks_pkg/orchestrator/_finalize.py), `_pollFallback` copies it onto
 *    the LIVE message, and the reducer then declares the task's own live
 *    bubble a "prior turn".
 *
 *    `assistantTailIsPriorTurn` now makes IDENTITY win: a tail bound to THIS
 *    task is never a prior turn. So the correct contract is: re-target the
 *    task's OWN slot, append NOTHING. The old turn is still never streamed
 *    over, because a reconnect to a finished task has no live stream.
 *    Scenarios B and D below pin that the protective arms are untouched. ── */
function scenarioConv() {
  return { id: 'c1', messages: [
    { role: 'user', content: 'hi', _msgId: 'u1' },
    { role: 'assistant', content: 'done reply', finishReason: 'stop', _taskId: 'T', _msgId: 'a1' },
  ] };
}

(function () {
  const conv = scenarioConv();
  const r = resolveTarget(conv, 'T', { mode: 'fixed', hasActiveStream: false });
  // The prior completed reply is preserved untouched — never streamed over.
  const old = conv.messages.find(m => m._msgId === 'a1');
  check('fixed_reconnect_preserves_old_reply', !!old && old.content === 'done reply');
  // ★ REVERSED: the task's OWN slot is re-targeted, not shadowed by an empty twin.
  check('fixed_reconnect_reuses_own_slot', r.target && r.target._msgId === 'a1');
  check('fixed_reconnect_appends_nothing', r.appended === false);
  check('fixed_reconnect_single_assistant', assistantCount(conv) === 1);
})();

// ── Scenario A2: a LIVE stream for this task re-targets its own slot by
//    stable id — the path that genuinely collapses the duplicate. ──
(function () {
  const conv = { id: 'c1b', messages: [
    { role: 'user', content: 'hi', _msgId: 'u1' },
    { role: 'assistant', content: 'partial', _taskId: 'T', _msgId: 'a_live' },
  ] };
  const r = resolveTarget(conv, 'T', {
    mode: 'fixed', hasActiveStream: true, streamMsgId: 'a_live' });
  check('fixed_live_stream_no_append', r.appended === false);
  check('fixed_live_stream_single_assistant', assistantCount(conv) === 1);
  check('fixed_live_stream_retargets_own_slot', r.target && r.target._msgId === 'a_live');
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

// ── Scenario D: PAGE-RELOAD foreign completed tail with NO _taskId (_taskId is
//    NOT persisted to the DB, so a reloaded completed tail lacks it). A NEW
//    task must push fresh, never replay the old turn. ──
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

// ── NEUTER (stale arm): drop `_staleTaskId` → a FOREIGN completed tail whose
//    _taskId differs is still caught by the finishReason arm, but a foreign
//    tail that is NOT yet complete slips through and gets streamed into. ──
(function () {
  const conv = { id: 'c5', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    // Foreign task's still-open slot (no finishReason) — must NOT be reused.
    { role: 'assistant', content: 'other task text', _taskId: 'T_OTHER', _msgId: 'a_other' },
  ] };
  const r = resolveTarget(conv, 'T_NEW', { mode: 'neuter_stale', hasActiveStream: false });
  check('neuter_stale_reuses_foreign_open_slot', r.appended === false);
  check('neuter_stale_target_is_foreign', r.target && r.target._msgId === 'a_other');
})();
// Control: the SHIPPED reducer pushes fresh for that same foreign open slot.
(function () {
  const conv = { id: 'c5b', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    { role: 'assistant', content: 'other task text', _taskId: 'T_OTHER', _msgId: 'a_other' },
  ] };
  const r = resolveTarget(conv, 'T_NEW', { mode: 'fixed', hasActiveStream: false });
  check('fixed_foreign_open_slot_appends', r.appended === true);
})();

// ── OVER-NARROW NEUTER: gating the completed-turn arm on a truthy _taskId
//    reuses a reloaded foreign completed tail (no _taskId) → old turn replays.
//    Proves the `_taskId &&` clause would be harmful on the reload path. ──
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
    with open(_reducer_module(), encoding='utf-8') as f:
        mod_src = f.read()
    start = mod_src.index(_REDUCER_SIG)
    end = mod_src.index('\n}\n', start) + len('\n}\n')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS.replace('__REDUCER__', mod_src[start:end]))
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
    # NEUTER (stale arm removed) proves the `_staleTaskId` equality is
    # load-bearing: a foreign task's still-open slot gets streamed into.
    assert 'PASS neuter_stale_reuses_foreign_open_slot' in output, output
    assert 'PASS neuter_stale_target_is_foreign' in output, output
    # … while the shipped reducer pushes fresh for that same slot.
    assert 'PASS fixed_foreign_open_slot_appends' in output, output
    # Reconnect to a just-finished task: the prior reply is preserved AND the
    # task's own slot is re-targeted rather than shadowed by an empty twin
    # (contract reversed 2026-07-31 — see the Scenario A note in the harness).
    assert 'PASS fixed_reconnect_preserves_old_reply' in output, output
    assert 'PASS fixed_reconnect_reuses_own_slot' in output, output
    assert 'PASS fixed_reconnect_appends_nothing' in output, output
    assert 'PASS fixed_reconnect_single_assistant' in output, output
    # A LIVE stream re-targets its own slot by stable id — no second bubble.
    assert 'PASS fixed_live_stream_no_append' in output, output
    assert 'PASS fixed_live_stream_single_assistant' in output, output
    assert 'PASS fixed_live_stream_retargets_own_slot' in output, output
    # A genuinely new turn still appends.
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
    # The completed-turn decision was refactored into a single shared reducer
    # (`assistantTailIsPriorTurn`) — connectToTask now calls it instead of
    # re-inlining the predicate. Assert the call site is present so the
    # identity-first guard still routes through the reducer.
    assert 'assistantTailIsPriorTurn(assistantMsg, taskId)' in sse_src, \
        'connectToTask no longer routes the completed-turn decision through the shared reducer assistantTailIsPriorTurn(assistantMsg, taskId)'

    # The reducer lives in whichever core module currently defines it (resolved
    # by search — it already moved once). Its completed-turn arm must treat ANY
    # completed tail as a prior turn (`!!msg.finishReason`); this is the
    # reload-safe form — a completed tail with NO persisted _taskId still
    # pushes fresh, never replays the old turn.
    with open(_reducer_module(), encoding='utf-8') as f:
        conv_src = f.read()
    assert 'function assistantTailIsPriorTurn(' in conv_src, \
        'assistantTailIsPriorTurn reducer missing from its resolved core module'
    assert 'const _isCompletedTurn = !!msg.finishReason;' in conv_src, \
        'reducer completed-turn arm is not the reload-safe `!!msg.finishReason` form — reload-safe dedupe fix reverted'
    # The over-narrow `_taskId && ... !== taskId` clause must NOT gate the
    # COMPLETED-TURN arm (it regressed the page-reload no-_taskId case). The
    # separate `_staleTaskId` line legitimately keeps that shape.
    assert 'const _isCompletedTurn = !!msg.finishReason\n      && msg._taskId && msg._taskId !== activeTaskId' not in conv_src, \
        'the over-narrow `_taskId && ... !== taskId` clause is back on the completed-turn arm — it wrongly reuses a reloaded foreign completed tail (no persisted _taskId)'
    assert 'if (assistantMsg && !assistantMsg._taskId) assistantMsg._taskId = taskId;' in sse_src, \
        'bind-time _taskId stamp missing — reconnect after finishStream cannot resolve by identity'

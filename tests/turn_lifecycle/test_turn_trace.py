"""Turn Trace — the unified per-task timing fold (pytest suite).

Contract: docs/contracts/TURN_TRACE_CONTRACT.md. Verifies
``lib/tasks_pkg/turn_trace.py`` against REAL seeded ``task_events`` rows
(explicit ts_ms so durations are deterministic; unique task ids in the dev
DB, cleaned up after):

  1. Span tree: turn → round → llm/tool nests correctly; a tool child
     EXTENDS its closed authoring round's footprint (tools run between
     rounds); terminal status propagates.
  2. STRICT ACCOUNTING: summary buckets are a disjoint partition — they
     sum EXACTLY to totalMs; gaps + covered = the turn interval.
  3. TTFT: waiting_model → first delta; a tool_start settles a tokenless
     round's TTFT candidate.
  4. Retry coalescing: N ``retrying`` beats = ONE wait span (latest
     attempt kept); the wait bucket beats the llm bucket on overlap
     (429 time is wait, not model time).
  5. Budgets: an over-budget local tool is flagged; declared-unbounded
     spans (run_command / llm) never are; undeclared tools carry no
     budget key.
  6. Approval/spawn split: write_approval_request + execStartTs split the
     tool span into approval_wait / execution; an unexplained >500ms
     announce→spawn delay becomes a spawn_wait span.
  7. Compaction: phase-driven open + compaction_done close; event-only
     path also folds.
  8. Truncation & liveness: a missing round_end flags truncated; a
     running task (no done) reports status=running, tEnd=None, and spans
     bounded by the injected now_ms.
  9. Honesty: unknown/expired task → eventsAvailable:false; endpoint
     tasks → coverage:partial; legacy no-round-marker logs → partial.
 10. Drift guard: every chat-domain phase in the registry has a declared
     rule, and every rule key is a registered phase (the unified-interface
     ratchet, mirroring test_phase_registry.py).
 11. Route registration pins /api/v1/tasks/<id>/trace on the v1 blueprint.
 12. User-visible phase history and browser paint/transport receipts remain
     idempotent, bounded, content-free, and byte-capped.
 13. A failed event read is never an empty log: the reader raises, live
     folds cannot report "expired", and settlement freezes an explicit
     ``event-read-failed`` partial marker instead of a zero-round fold.

NEUTER: make ``_disjoint_summary`` attribute every segment to 'llmMs' →
the sum-to-total assertion flips red (proving the partition is
load-bearing).
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))

_T0 = 1_724_000_000_000
_SEEDED_ROWS = {}


def _seed(task_id, events):
    """Build deterministic authority-shaped rows for the pure timing fold."""
    _SEEDED_ROWS[task_id] = [
        {'event_id': event_id, 'type': event_type,
         'payload': payload | {'type': event_type}, 'ts_ms': _T0 + offset}
        for event_id, (event_type, offset, payload) in enumerate(events)
    ]
    return task_id


def _cleanup(*task_ids):
    for task_id in task_ids:
        _SEEDED_ROWS.pop(task_id, None)


def _tid():
    return 'trace-test-' + uuid.uuid4().hex[:12]


def _fold(task_id, now_ms=None):
    from lib.tasks_pkg import turn_trace

    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(
        _SEEDED_ROWS.get(selected, ()))
    try:
        return turn_trace.fold_task_trace(task_id, now_ms=now_ms)
    finally:
        turn_trace._read_trace_rows = original


def _spans(doc, kind=None):
    return [s for s in doc['spans'] if kind is None or s['kind'] == kind]


def _assert_strict_accounting(doc):
    """The contract's core invariant: disjoint buckets sum to totalMs and
    gaps + covered = the whole turn interval."""
    s = doc['summary']
    parts = [s['llmMs'], s['toolMs'], s['waitMs'], s['compactionMs'],
             s['approvalWaitMs'], s.get('flowStepMs', 0),
             s.get('orchestrationMs', 0), s['unattributedMs']]
    assert sum(parts) == s['totalMs'] == doc['totalMs'], (
        f'buckets {parts} must sum to totalMs {doc["totalMs"]}')
    gap_ms = sum(g['tEnd'] - g['tStart'] for g in doc['gaps'])
    assert gap_ms == s['unattributedMs'], (
        f'gaps {gap_ms} must equal unattributedMs {s["unattributedMs"]}')
    # Each split is the exact sum of its bucket (never a second account).
    assert (s.get('admissionWaitMs', 0) + s.get('retryWaitMs', 0)
            + s.get('spawnWaitMs', 0)) == s['waitMs']
    assert (s.get('startupMs', 0) + s.get('roundPrepMs', 0)
            + s.get('roundSettleMs', 0) + s.get('finalizeMs', 0)) \
        == s.get('orchestrationMs', 0)


# ── 1/2/3. The canonical two-round task: tree, strict accounting, TTFT ──

def test_fold_canonical_task():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'waiting_model', 'model': 'm-a'}),
        ('delta', 1500, {}),
        ('round_end', 8000, {'roundNum': 1, 'reason': 'tools'}),
        ('phase', 8100, {'phase': 'tool_exec', 'tools': ['read_files']}),
        ('tool_start', 8200, {'roundNum': 1, 'toolName': 'read_files',
                              'toolCallId': 'c1', 'query': 'a.py',
                              'tStart': _T0 + 8200}),
        ('tool_result', 10200, {'roundNum': 1, 'toolCallId': 'c1',
                                'status': 'done', 'tStart': _T0 + 8200,
                                'tEnd': _T0 + 10200}),
        ('round_start', 11000, {'roundNum': 2}),
        ('phase', 11100, {'phase': 'waiting_model'}),
        ('delta', 12000, {}),
        ('round_end', 20000, {'roundNum': 2, 'reason': 'final'}),
        ('round_usage', 20100, {'roundNum': 2, 'model': 'm-a', 'tag': 'R2',
                                'tokensIn': 10, 'tokensOut': 5,
                                'usage': {'trace_id': 'tr',
                                          'stream_elapsed_ms': 9000}}),
        ('done', 20100, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        assert doc['eventsAvailable'] is True
        assert doc['status'] == 'done' and doc['running'] is False
        assert doc['coverage'] == 'full'
        assert doc['tStart'] == _T0 and doc['tEnd'] == _T0 + 20100
        assert doc['totalMs'] == 20100

        rounds = _spans(doc, 'round')
        assert [r['name'] for r in rounds] == ['round 1', 'round 2']
        # Round 1's footprint EXTENDS past its round_end (8000) to cover
        # the tool it authored (10200).
        r1 = next(r for r in rounds if r['name'] == 'round 1')
        assert r1['tEnd'] == _T0 + 10200

        tool = _spans(doc, 'tool')[0]
        assert tool['parent'] == 'r1' and tool['depth'] == 2
        assert tool['tEnd'] - tool['tStart'] == 2000
        assert tool['status'] == 'done'

        ttfts = _spans(doc, 'llm_ttft')
        assert len(ttfts) == 2
        assert ttfts[0]['tEnd'] - ttfts[0]['tStart'] == 1400  # 100 → 1500

        llm2 = [s for s in _spans(doc, 'llm') if s['parent'] == 'r2'][0]
        assert llm2['attrs']['attempts'][0]['streamElapsedMs'] == 9000
        assert llm2['attrs']['model'] == 'm-a'

        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_semantic_timeout_attempt_retains_typed_stall_diagnostics():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1_200_000, {
            'roundNum': 1, 'reason': 'abnormal_stop'}),
        ('round_usage', 1_200_001, {
            'roundNum': 1,
            'model': 'kimi-k3',
            'usage': {
                'stream_elapsed_ms': 1_200_000,
                '_stream_state': 'semantic_progress_timeout',
                '_semantic_progress_timeout': True,
                '_no_actionable_timeout': True,
                '_semantic_idle_timeout_ms': 300_000,
                '_semantic_progress_idle_ms': 300_010,
                '_no_actionable_timeout_s': 300,
                '_no_actionable_stall_elapsed_s': 300.01,
                '_no_actionable_request_elapsed_s': 1_200.25,
                '_no_actionable_reasoning_chars': 98_765,
                '_no_actionable_reasoning_chunks': 4_321,
                '_chunks_received': 8_765,
            },
        }),
        ('error', 1_200_001, {'kind': 'abnormal_stop'}),
    ])
    try:
        doc = _fold(tid)
        llm = next(span for span in _spans(doc, 'llm')
                   if span['parent'] == 'r1')
        attempt = llm['attrs']['attempts'][0]
        assert attempt['streamState'] == 'semantic_progress_timeout'
        assert attempt['semanticProgressTimeout'] is True
        assert attempt['noActionableTimeout'] is True
        assert attempt['semanticStallWindowMs'] == 300_000
        assert attempt['lastSemanticProgressAgeMs'] == 300_010
        assert attempt['requestElapsedMs'] == 1_200_250
        assert attempt['reasoningChars'] == 98_765
        assert attempt['reasoningChunks'] == 4_321
        assert attempt['sseChunks'] == 8_765
    finally:
        _cleanup(tid)


# ── 4. Retry coalescing + wait-over-llm priority ──

def test_retry_beats_coalesce_and_outrank_llm():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'waiting_model'}),
        ('phase', 1000, {'phase': 'retrying', 'attempt': 1,
                         'statusCode': 429}),
        ('phase', 5000, {'phase': 'retrying', 'attempt': 2,
                         'statusCode': 429}),
        ('phase', 9000, {'phase': 'retrying', 'attempt': 3,
                         'statusCode': 429}),
        ('delta', 20000, {}),
        ('round_end', 25000, {'roundNum': 1, 'reason': 'final'}),
        ('done', 25000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        waits = _spans(doc, 'retry_wait')
        assert len(waits) == 1, 'N retrying beats must fold into ONE span'
        assert waits[0]['tStart'] == _T0 + 1000
        assert waits[0]['tEnd'] == _T0 + 20000  # first delta ends the wait
        assert waits[0]['attrs']['attempt'] == 3  # latest beat kept
        s = doc['summary']
        assert s['waitMs'] == 19000
        # The llm window [0, 25000] minus the 19s wait = 6s of model time:
        # a 429 stall is NEVER billed to the model bucket.
        assert s['llmMs'] == 6000
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_model_request_attempts_expose_route_and_failure_stage():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('model_request_start', 100, {
            'spanId': 'model:attempt:1', 'roundNum': 1,
            'requestTag': 'R1', 'model': 'kimi-k3'}),
        ('phase', 150, {'phase': 'waiting_model', 'model': 'kimi-k3'}),
        ('model_request_complete', 5100, {
            'spanId': 'model:attempt:1', 'roundNum': 1,
            'requestTag': 'R1', 'model': 'kimi-k3', 'status': 'failed',
            'durationMs': 5000, 'errorKind': 'PrematureStreamClose',
            'routeId': 'direct:configured-bypass', 'routeMode': 'direct',
            'routeDecision': 'configured_bypass',
            'failureStage': 'midstream_close'}),
        ('phase', 5200, {'phase': 'retrying', 'attempt': 1,
                         'bucket': 'classic', 'backoff_s': 0.8}),
        ('model_request_start', 6000, {
            'spanId': 'model:attempt:2', 'roundNum': 1,
            'requestTag': 'R1', 'model': 'kimi-k3'}),
        ('phase', 6100, {'phase': 'waiting_model', 'model': 'kimi-k3'}),
        ('delta', 6500, {}),
        ('model_request_complete', 9000, {
            'spanId': 'model:attempt:2', 'roundNum': 1,
            'requestTag': 'R1', 'model': 'kimi-k3', 'status': 'succeeded',
            'durationMs': 3000, 'routeId': 'pool:hk',
            'routeMode': 'proxy', 'routeDecision': 'proxy_pool'}),
        ('round_end', 9100, {'roundNum': 1, 'reason': 'final'}),
        ('done', 9100, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        attempts = [span for span in _spans(doc, 'llm')
                    if span['attrs'].get('requestLevel') == 'logical']
        assert len(attempts) == 2
        assert all(not span['attrs'].get('attemptLevel') for span in attempts)
        assert [span['status'] for span in attempts] == ['error', 'done']
        assert all(span['parent'] == 'r1.llm' and span['depth'] == 3
                   for span in attempts)
        assert attempts[0]['attrs']['routeId'] == \
            'direct:configured-bypass'
        assert attempts[0]['attrs']['failureStage'] == 'midstream_close'
        assert attempts[1]['attrs']['routeId'] == 'pool:hk'
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 5. Budget declarations ──

def test_budgets_flag_only_declared_local_tools():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        # 30s write_file — over its declared 15s budget.
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'write_file',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        ('tool_result', 32000, {'roundNum': 1, 'toolCallId': 'c1',
                                'status': 'done', 'tEnd': _T0 + 32000}),
        # 60s run_command — declared UNBOUNDED: never flagged.
        ('tool_start', 33000, {'roundNum': 1, 'toolName': 'run_command',
                               'toolCallId': 'c2', 'tStart': _T0 + 33000}),
        ('tool_result', 93000, {'roundNum': 1, 'toolCallId': 'c2',
                                'status': 'done', 'tEnd': _T0 + 93000}),
        # 60s made_up_tool — undeclared: no budget key at all.
        ('tool_start', 94000, {'roundNum': 1, 'toolName': 'made_up_tool',
                               'toolCallId': 'c3', 'tStart': _T0 + 94000}),
        ('tool_result', 154000, {'roundNum': 1, 'toolCallId': 'c3',
                                 'status': 'done', 'tEnd': _T0 + 154000}),
        ('round_start', 155000, {'roundNum': 2}),
        ('round_end', 160000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 160000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        by_name = {s['name']: s for s in _spans(doc, 'tool')}
        assert by_name['write_file'].get('overBudget') is True
        assert by_name['write_file']['budgetMs'] == 2000
        assert 'overBudget' not in by_name['run_command']
        assert 'budgetMs' not in by_name['run_command']
        assert 'budgetMs' not in by_name['made_up_tool']
        over = doc['summary']['overBudget']
        assert [o['name'] for o in over] == ['write_file']
        assert over[0]['elapsedMs'] == 30000
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 6. Approval wait + spawn wait split ──

def test_approval_and_spawn_wait_split():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'write_file',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        ('write_approval_request', 2500, {'toolName': 'write_file',
                                          'toolCallId': 'c1'}),
        ('tool_progress', 32000, {'roundNum': 1, 'toolCallId': 'c1',
                                  'execStartTs': _T0 + 32000}),
        ('tool_result', 35000, {'roundNum': 1, 'toolCallId': 'c1',
                                'status': 'done', 'tEnd': _T0 + 35000}),
        # A second tool with NO approval but a >500ms dispatch delay.
        ('tool_start', 40000, {'roundNum': 1, 'toolName': 'read_files',
                               'toolCallId': 'c2', 'tStart': _T0 + 40000}),
        ('tool_progress', 45000, {'roundNum': 1, 'toolCallId': 'c2',
                                  'execStartTs': _T0 + 45000}),
        ('tool_result', 46000, {'roundNum': 1, 'toolCallId': 'c2',
                                'status': 'done', 'tEnd': _T0 + 46000}),
        ('round_start', 50000, {'roundNum': 2}),
        ('round_end', 55000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 55000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        appr = _spans(doc, 'approval_wait')
        assert len(appr) == 1
        assert appr[0]['tStart'] == _T0 + 2500
        assert appr[0]['tEnd'] == _T0 + 32000  # real spawn ends the wait
        spawn = _spans(doc, 'spawn_wait')
        assert len(spawn) == 1
        assert spawn[0]['tEnd'] - spawn[0]['tStart'] == 5000
        s = doc['summary']
        assert s['approvalWaitMs'] == 29500
        assert s['waitMs'] == 5000
        # Approval/spawn time outranks tool time: the tool bucket holds
        # only true execution — c1's pre-approval sliver + post-spawn run,
        # plus c2's post-spawn run.
        assert s['toolMs'] == (2500 - 2000) + (35000 - 32000) + (46000 - 45000)
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 7. Compaction, both paths ──

def test_compaction_phase_and_event_paths():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 5000, {'roundNum': 1, 'reason': 'tools'}),
        ('phase', 6000, {'phase': 'compacting'}),
        ('compaction', 6100, {'detail': 'archiving'}),
        ('compaction_done', 20000, {'archived': 10}),
        ('round_start', 21000, {'roundNum': 2}),
        # Event-only path (no phase): a second compaction.
        ('round_end', 25000, {'roundNum': 2, 'reason': 'tools'}),
        ('compaction', 26000, {'detail': 'again'}),
        ('compaction_done', 30000, {'archived': 4}),
        ('round_start', 31000, {'roundNum': 3}),
        ('round_end', 35000, {'roundNum': 3, 'reason': 'final'}),
        ('done', 35000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        comps = _spans(doc, 'compaction')
        assert len(comps) == 2
        assert comps[0]['tEnd'] - comps[0]['tStart'] == 14000
        assert comps[1]['tEnd'] - comps[1]['tStart'] == 4000
        assert doc['summary']['compactionMs'] == 18000
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 8. Truncation + running-task liveness ──

def test_truncated_round_and_running_task():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'waiting_model'}),
        ('delta', 1000, {}),
        # No round_end, no done — the task is still live.
    ])
    try:
        doc = _fold(tid, now_ms=_T0 + 9000)
        assert doc['status'] == 'running' and doc['running'] is True
        assert doc['tEnd'] is None
        assert doc['totalMs'] == 9000
        llm = _spans(doc, 'llm')[0]
        assert llm['tEnd'] == _T0 + 9000  # bounded by the injected now
        assert llm['status'] == 'running'
        ttft = _spans(doc, 'llm_ttft')[0]
        assert ttft['tEnd'] - ttft['tStart'] == 900
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_missing_round_end_flags_truncated():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_start', 5000, {'roundNum': 2}),  # r1 never closed properly
        ('round_end', 9000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 9000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        r1 = next(s for s in _spans(doc, 'round') if s['name'] == 'round 1')
        assert r1.get('truncated') is True
        assert r1['status'] == 'unknown'
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_recycled_tool_id_does_not_overwrite_unsettled_prior_round_span():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 1200, {'roundNum': 1, 'toolName': 'read_files',
                              'toolCallId': 'call_0',
                              'tStart': _T0 + 1200}),
        # The result event is lost, but the next round proves the tool stopped.
        ('round_start', 3000, {'roundNum': 2}),
        ('round_end', 4000, {'roundNum': 2, 'reason': 'tools'}),
        ('tool_start', 4200, {'roundNum': 2, 'toolName': 'write_file',
                              'toolCallId': 'call_0',
                              'tStart': _T0 + 4200}),
        ('tool_result', 5000, {'roundNum': 2, 'toolCallId': 'call_0',
                               'status': 'done', 'tEnd': _T0 + 5000}),
        ('done', 5100, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        tools = _spans(doc, 'tool')
        assert len(tools) == 2
        assert tools[0]['name'] == 'read_files'
        assert tools[0]['status'] == 'unknown'
        assert tools[0]['tEnd'] == _T0 + 3000
        assert tools[0].get('truncated') is True
        assert tools[1]['name'] == 'write_file'
        assert tools[1]['status'] == 'done'
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_duplicate_tool_id_in_one_round_is_paired_by_occurrence():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 1200, {'roundNum': 1, 'toolName': 'read_files',
                              'toolCallId': 'same', 'tStart': _T0 + 1200}),
        ('tool_start', 1300, {'roundNum': 1, 'toolName': 'grep_search',
                              'toolCallId': 'same', 'tStart': _T0 + 1300}),
        ('tool_result', 2000, {'roundNum': 1, 'toolCallId': 'same',
                               'status': 'done', 'tEnd': _T0 + 2000}),
        ('tool_result', 2300, {'roundNum': 1, 'toolCallId': 'same',
                               'status': 'done', 'tEnd': _T0 + 2300}),
        ('done', 2400, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        tools = _spans(doc, 'tool')
        assert [tool['name'] for tool in tools] == [
            'read_files', 'grep_search']
        assert [tool['status'] for tool in tools] == ['done', 'done']
        assert len({tool['spanId'] for tool in tools}) == 2
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_stale_approval_does_not_attach_to_recycled_id_in_later_round():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('write_approval_request', 500, {'roundNum': 1,
                                          'toolCallId': 'call_0'}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'error'}),
        ('round_start', 2000, {'roundNum': 2}),
        ('round_end', 3000, {'roundNum': 2, 'reason': 'tools'}),
        ('tool_start', 3200, {'roundNum': 2, 'toolName': 'write_file',
                              'toolCallId': 'call_0',
                              'tStart': _T0 + 3200}),
        ('tool_result', 3800, {'roundNum': 2, 'toolCallId': 'call_0',
                               'status': 'done', 'tEnd': _T0 + 3800}),
        ('done', 3900, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        assert _spans(doc, 'approval_wait') == []
        assert _spans(doc, 'tool')[0]['status'] == 'done'
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_unknown_result_id_never_settles_an_unrelated_open_tool():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 1200, {'roundNum': 1, 'toolName': 'read_files',
                              'toolCallId': 'real', 'tStart': _T0 + 1200}),
        ('tool_result', 1500, {'roundNum': 1, 'toolCallId': 'orphan',
                               'status': 'error', 'tEnd': _T0 + 1500}),
        ('tool_result', 2100, {'roundNum': 1, 'toolCallId': 'real',
                               'status': 'done', 'tEnd': _T0 + 2100}),
        ('done', 2200, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        tools = _spans(doc, 'tool')
        assert len(tools) == 1
        assert tools[0]['status'] == 'done'
        assert tools[0]['tEnd'] == _T0 + 2100
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 9. Honesty: empty / Flow / legacy coverage ──

def test_unknown_task_is_honestly_empty():
    doc = _fold(_tid())
    assert doc['eventsAvailable'] is False
    assert doc['version'] == 1


def test_flow_task_marks_partial_coverage():
    tid = _tid()
    _seed(tid, [
        ('flow_iteration', 0, {'iteration': 1, 'phase': 'planning'}),
        ('round_start', 100, {'roundNum': 1}),
        ('round_end', 5000, {'roundNum': 1, 'reason': 'final'}),
        ('done', 5000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        assert doc['coverage'] == 'partial'
        assert doc['coverageReason'] == 'flow'
    finally:
        _cleanup(tid)


def test_flow_steps_fold_as_spans_and_claim_only_the_remainder():
    """Planner/Critic node executions fold as flow_step spans.

    The llm/tool work dispatched inside a step keeps its own attribution;
    the step bucket only claims the uncovered remainder (dispatch,
    coordination) — the strict partition still holds, and a foldable step
    vocabulary lifts the blanket partial/flow coverage.
    """
    tid = _tid()
    _seed(tid, [
        ('flow_start', 0, {'name': 'fixture-flow', 'nodes': 2}),
        ('step_start', 10, {'node_id': 'planner', 'role': 'planner',
                            'name': 'Planner'}),
        ('round_start', 100, {'roundNum': 1}),
        ('round_end', 5000, {'roundNum': 1, 'reason': 'tool_calls'}),
        ('step_complete', 6000, {'node_id': 'planner', 'role': 'planner',
                                 'status': 'completed'}),
        ('step_start', 7000, {'node_id': 'critic', 'role': 'critic',
                              'name': 'Critic'}),
        ('step_complete', 9000, {'node_id': 'critic', 'role': 'critic',
                                 'status': 'failed', 'error': 'nope'}),
        ('flow_complete', 9500, {'status': 'failed'}),
        ('done', 9500, {'finishReason': 'error'}),
    ])
    try:
        doc = _fold(tid, now_ms=9500)
        assert doc['coverage'] == 'full', doc.get('coverageReason')
        steps = _spans(doc, 'flow_step')
        assert [(s['name'], s['status']) for s in steps] == [
            ('Planner', 'done'), ('Critic', 'error')]
        planner, critic = steps
        assert (planner['tStart'], planner['tEnd']) == (_T0 + 10, _T0 + 6000)
        assert (critic['tStart'], critic['tEnd']) == (_T0 + 7000, _T0 + 9000)
        assert planner['attrs']['nodeId'] == 'planner'
        s = doc['summary']
        # Round 1's llm window covers [100, 5000] inside the planner step;
        # the step only claims the remainder of its own interval.
        assert s['llmMs'] == 4900
        assert s['flowStepMs'] == (6000 - 10 - 4900) + (9000 - 7000)
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_flow_step_left_open_by_crash_settles_truncated():
    tid = _tid()
    _seed(tid, [
        ('step_start', 100, {'node_id': 'planner', 'role': 'planner',
                             'name': 'Planner'}),
        ('step_start', 5000, {'node_id': 'critic', 'role': 'critic',
                              'name': 'Critic'}),
        ('done', 9000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid, now_ms=9000)
        steps = _spans(doc, 'flow_step')
        # The sequential walk proves the first step's completion was lost:
        # it closes truncated at the next step_start, never overlapping.
        assert [(s['name'], s['status']) for s in steps] == [
            ('Planner', 'unknown'), ('Critic', 'unknown')]
        assert steps[0]['truncated'] and steps[1]['truncated']
        assert (steps[0]['tStart'], steps[0]['tEnd']) == (_T0 + 100, _T0 + 5000)
        assert (steps[1]['tStart'], steps[1]['tEnd']) == (_T0 + 5000, _T0 + 9000)
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_legacy_log_without_round_markers_folds_flat_and_honest():
    tid = _tid()
    _seed(tid, [
        ('phase', 0, {'phase': 'working', 'detail': 'Working…'}),
        ('tool_start', 1000, {'roundNum': 1, 'toolName': 'read_files',
                              'toolCallId': 'c1', 'tStart': _T0 + 1000}),
        ('tool_result', 3000, {'roundNum': 1, 'toolCallId': 'c1',
                               'status': 'done', 'tEnd': _T0 + 3000}),
        ('done', 4000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        assert doc['coverage'] == 'partial'
        assert doc['coverageReason'] == 'no-round-markers'
        # Tools still fold (flat, parented to the turn).
        tool = _spans(doc, 'tool')[0]
        assert tool['parent'] == 'turn' and tool['depth'] == 1
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


# ── 10. Drift guard: the phase rule table tracks the registry ──

def test_phase_rule_table_covers_the_chat_registry():
    from lib.agent_core.events import all_phase_specs, phase_values
    from lib.tasks_pkg.turn_trace import _PHASE_TRACE_RULE
    chat_phases = {s.phase for s in all_phase_specs() if 'chat' in s.domains}
    rules = set(_PHASE_TRACE_RULE)
    missing = chat_phases - rules
    assert not missing, (
        f'registered chat phases with NO trace rule: {sorted(missing)} — '
        'add a rule to _PHASE_TRACE_RULE in lib/tasks_pkg/turn_trace.py '
        '(ttft | retry_wait | compaction | covered | ignore | startup)')
    dead = rules - phase_values()
    assert not dead, f'trace rules for unregistered phases: {sorted(dead)}'
    legal = {'ttft', 'retry_wait', 'compaction', 'covered', 'ignore', 'startup'}
    assert set(_PHASE_TRACE_RULE.values()) <= legal


def test_budget_tables_are_well_formed():
    from lib.tasks_pkg.turn_trace import _KIND_BUDGETS_MS, _TOOL_BUDGETS_MS
    for table in (_TOOL_BUDGETS_MS, _KIND_BUDGETS_MS):
        for name, budget in table.items():
            assert budget is None or (isinstance(budget, int) and budget > 0), (
                f'{name}: budget must be None (declared unbounded) or a '
                f'positive ms int, got {budget!r}')


# ── 11. Durable prompt/perception evidence and resource bounds ──

def test_user_visible_status_history_coalesces_heartbeats_and_marks_stall():
    from lib.tasks_pkg.turn_trace import (
        observe_task_trace_event,
        task_status_history,
    )

    task = {'id': 'status-task'}
    observe_task_trace_event(task, {
        'type': 'phase', 'phase': 'waiting_model',
        'detailKey': 'status.waitingModel', 'detailArgs': {'model': 'm-a'},
    }, observed_at_ms=1_000)
    observe_task_trace_event(task, {
        'type': 'phase', 'phase': 'waiting_model',
        'detailKey': 'status.waitingModel', 'detailArgs': {'model': 'm-a'},
    }, observed_at_ms=2_000)
    observe_task_trace_event(task, {
        'type': 'phase', 'phase': 'stream_stalled',
        'detailKey': 'status.streamStalled',
    }, observed_at_ms=3_000)
    observe_task_trace_event(
        task, {'type': 'done'}, observed_at_ms=7_000)

    history, dropped = task_status_history(task)
    assert dropped == 0
    assert [row['phase'] for row in history] == [
        'waiting_model', 'stream_stalled']
    assert history[0]['count'] == 2
    assert history[0]['tStart'] == 1_000
    assert history[0]['lastObservedAt'] == 2_000
    assert history[0]['tEnd'] == 3_000
    assert history[1]['attention'] == 'stall'
    assert history[1]['tEnd'] == 7_000
    assert history[1]['terminalBoundary'] is True


def test_status_history_is_bounded_and_reports_eviction():
    from lib.tasks_pkg.turn_trace import (
        TRACE_MAX_STATUS_ENTRIES,
        observe_task_trace_event,
        task_status_history,
    )

    task = {'id': 'bounded-status-task'}
    for index in range(TRACE_MAX_STATUS_ENTRIES + 9):
        observe_task_trace_event(task, {
            'type': 'phase', 'phase': 'working',
            'detailKey': f'status.synthetic.{index}',
        }, observed_at_ms=index + 1)
    history, dropped = task_status_history(task)
    assert len(history) == TRACE_MAX_STATUS_ENTRIES
    assert dropped == 9
    assert history[0]['detailKey'] == 'status.synthetic.9'


def test_terminal_pending_event_freezes_status_history():
    from lib.tasks_pkg import turn_trace

    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'waiting_model',
                        'detailKey': 'status.waitingModel'}),
        ('round_end', 2_000, {'roundNum': 1, 'reason': 'final'}),
    ])
    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(
        _SEEDED_ROWS.get(selected, ()))
    try:
        doc = turn_trace.fold_task_trace(
            tid,
            now_ms=_T0 + 2_100,
            pending_event={'type': 'done', 'finishReason': 'stop'},
            pending_sequence=3,
        )
        assert doc['status'] == 'done'
        assert doc['running'] is False
        assert doc['tEnd'] == _T0 + 2_100
        assert doc['eventLogAvailable'] is False
        assert doc['statusHistory'][0]['detailKey'] == 'status.waitingModel'
        assert doc['statusHistory'][0]['terminalBoundary'] is True
    finally:
        turn_trace._read_trace_rows = original
        _cleanup(tid)


def test_client_perception_receipts_are_idempotent_derived_and_bounded():
    from lib.tasks_pkg.turn_trace import (
        TRACE_MAX_CLIENT_OBSERVATIONS,
        append_client_trace_observation,
    )

    trace = {'version': 1, 'taskId': 'task-a', 'source': 'turn-snapshot'}
    observation = {
        'observationId': 'paint:1',
        'kind': 'phase_painted',
        'attemptId': 'attempt-a',
        'clientId': 'page-a',
        'phase': 'waiting_model',
        'detailKey': 'status.waitingModel',
        'serverEmittedAt': 1_000,
        'receivedAt': 1_125,
        'paintedAt': 1_160,
    }
    trace, applied = append_client_trace_observation(
        trace, observation, task_id='task-a', attempt_id='attempt-a',
        recorded_at_ms=1_200,
    )
    assert applied is True
    row = trace['clientObservations'][0]
    assert row['renderMs'] == 35
    assert row['transportMs'] == 125
    assert 'content' not in row and 'detail' not in row

    replay, applied = append_client_trace_observation(
        trace, observation, task_id='task-a', attempt_id='attempt-a',
        recorded_at_ms=1_300,
    )
    assert applied is False
    assert replay['clientObservations'] == trace['clientObservations']

    with pytest.raises(ValueError, match='unsupported perception fields'):
        append_client_trace_observation(
            trace, {**observation, 'observationId': 'paint:content',
                    'content': 'forbidden'},
            task_id='task-a', attempt_id='attempt-a', recorded_at_ms=1_400,
        )
    with pytest.raises(ValueError, match='authoritative attempt'):
        append_client_trace_observation(
            trace, {**observation, 'observationId': 'paint:mismatch',
                    'attemptId': 'attempt-b'},
            task_id='task-a', attempt_id='attempt-a', recorded_at_ms=1_400,
        )

    for index in range(1, TRACE_MAX_CLIENT_OBSERVATIONS + 5):
        trace, applied = append_client_trace_observation(
            trace, {
                **observation,
                'observationId': f'paint:{index + 1}',
                'receivedAt': 2_000 + index,
                'paintedAt': 2_010 + index,
            },
            task_id='task-a', attempt_id='attempt-a',
            recorded_at_ms=3_000 + index,
        )
        assert applied is True
    assert len(trace['clientObservations']) == TRACE_MAX_CLIENT_OBSERVATIONS
    assert trace['clientObservationDroppedCount'] == 5


def test_attempt_receipt_lane_overlays_server_trace_without_cross_task_leakage():
    from lib.tasks_pkg.turn_trace import merge_client_trace_evidence

    server = {
        'version': 1, 'taskId': 'task-a', 'source': 'turn-snapshot',
        'eventsAvailable': True, 'summary': {'totalMs': 10},
        'clientObservations': [{'observationId': 'old'}],
    }
    receipts = {
        'version': 1, 'taskId': 'task-a',
        'clientObservations': [{'observationId': 'new'}],
        'clientObservationDroppedCount': 3,
    }
    merged = merge_client_trace_evidence(server, receipts, task_id='task-a')
    assert merged['summary'] == {'totalMs': 10}
    assert merged['clientObservations'] == [{'observationId': 'new'}]
    assert merged['clientObservationDroppedCount'] == 3

    mismatched = merge_client_trace_evidence(
        server, {**receipts, 'taskId': 'task-b'}, task_id='task-a')
    assert mismatched['clientObservations'] == [{'observationId': 'old'}]


def test_new_attempt_drops_previous_attempt_live_trace_before_first_phase():
    from lib.tasks_pkg.turn_trace import project_running_trace_status

    projected = project_running_trace_status({
        'content': '',
        'timingTrace': {
            'version': 1, 'taskId': 'old-task', 'running': False,
        },
    }, {'id': 'new-task'})
    assert 'timingTrace' not in projected


def test_persisted_trace_has_a_hard_byte_budget():
    import json

    from lib.tasks_pkg.turn_trace import (
        TRACE_MAX_PERSISTED_BYTES,
        compact_trace_document,
    )

    noisy = {
        'version': 1,
        'taskId': 'task-budget',
        'summary': {'totalMs': 1, 'llmMs': 0, 'toolMs': 0, 'waitMs': 0,
                    'compactionMs': 0, 'approvalWaitMs': 0,
                    'unattributedMs': 1, 'ttftMs': 0, 'overBudget': [{
                        'spanId': f'over-{index}', 'kind': 'tool',
                        'name': 'o' * 400, 'elapsedMs': 2, 'budgetMs': 1,
                    } for index in range(256)]},
        'spans': [{
            'spanId': f'span-{index}', 'parent': 'turn', 'depth': 2,
            'kind': 'tool', 'name': 'x' * 400, 'tStart': index,
            'tEnd': index + 1, 'status': 'done',
            'attrs': {f'field-{child}': 'y' * 400 for child in range(32)},
        } for index in range(600)],
        'statusHistory': [{
            'id': f'status-{index}', 'phase': 'working',
            'attention': 'progress', 'tStart': index,
            'lastObservedAt': index, 'count': 1,
            'detail': 'z' * 400, 'detailKey': 'status.synthetic',
            'detailArgs': {f'arg-{child}': 'v' * 200 for child in range(16)},
        } for index in range(300)],
        'clientObservations': [{
            'observationId': f'paint-{index}',
            'kind': 'phase_painted', 'taskId': 'task-budget',
            'attemptId': 'attempt-budget', 'clientId': 'c' * 64,
            'recordedAt': index, 'phase': 'p' * 80,
            'detailKey': 'd' * 160, 'reason': 'r' * 160,
            'receivedAt': index, 'paintedAt': index + 1,
        } for index in range(100)],
    }
    compacted = compact_trace_document(noisy, source='turn-snapshot')
    size = len(json.dumps(
        compacted, ensure_ascii=False, separators=(',', ':')).encode())
    assert size <= TRACE_MAX_PERSISTED_BYTES
    assert compacted['compacted'] is True
    assert compacted['droppedSpans'] > 0
    assert compacted['statusDroppedCount'] > 0
    assert compacted['overBudgetDroppedCount'] > 0
    from lib.conversation_sync.validation import decode
    decode('TurnTimingTrace', compacted)


def test_trace_within_budget_is_measured_once_and_keeps_compaction_markers(monkeypatch):
    """A live trace below the cap is encoded once, not once per byte guard."""
    import json

    import lib.turn_trace_contract as contract

    calls = []
    real_dumps = json.dumps

    def counting_dumps(*args, **kwargs):
        calls.append(1)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(contract.json, 'dumps', counting_dumps)
    small = {
        'version': 1, 'taskId': 'task-small', 'running': True, 'status': 'running',
        'statusHistory': [{'id': f'status-{index}', 'phase': 'working', 'tStart': index}
                          for index in range(TRACE_STATUS_ROWS)],
        'statusDroppedCount': 3,
    }
    compacted = contract.compact_trace_document(small, source='live-projection')
    assert len(calls) == 1
    assert compacted['statusDroppedCount'] == 3
    assert compacted['compacted'] is True
    assert compacted['statusHistory'] == small['statusHistory']

    calls.clear()
    plain = contract.compact_trace_document({'version': 1, 'taskId': 'task-plain'})
    assert len(calls) == 1
    assert 'compacted' not in plain

    # Only the final marker pass derives ``compacted`` from ``detailCompacted``.
    detail_only = contract.compact_trace_document(
        {'version': 1, 'taskId': 'task-detail', 'detailCompacted': True})
    assert detail_only['detailCompacted'] is True
    assert detail_only['compacted'] is True


TRACE_STATUS_ROWS = 12


def test_projection_normalizer_retains_timing_trace():
    from lib.turn_projection_patch import normalize_projection_document

    timing_trace = {
        'version': 1, 'taskId': 'task-a', 'source': 'turn-snapshot',
        'status': 'done', 'running': False,
    }
    normalized = normalize_projection_document({
        'content': 'settled', 'timingTrace': timing_trace,
    })
    assert normalized['timingTrace'] == timing_trace


def test_perception_storage_lane_avoids_receipt_and_sync_write_amplification():
    from lib.storage_authority_sql.operation_domains.turns import OPERATIONS

    operation = OPERATIONS['turn.perception.record']
    assert operation.kind == 'command'
    assert operation.receipt_required is False
    assert operation.after is None


# ── 12b. Workspace admission waits (lease-owner evidence) ──

def _admission(offset_start, offset_end, **fields):
    payload = {'waitStartedAtMs': _T0 + offset_start, 'waitEndedAtMs': _T0 + offset_end,
               'waitedMs': offset_end - offset_start, 'mode': 'write',
               'lockDomain': 'workspace', 'lockRoot': 'repo', 'outcome': 'admitted',
               'holderTool': 'run_command', 'holderConversation': 'conv-holder',
               'holderHeldMs': 61_000}
    payload.update(fields)
    return payload


def test_admission_wait_is_a_tool_child_that_ends_at_admission():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'write_file',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        # Evidence is appended when the wait ENDS (admission at 62s).
        ('workspace_admission_wait', 62_010, _admission(
            2_100, 62_000, roundNum=1, toolCallId='c1', toolName='write_file')),
        ('tool_result', 63_000, {'roundNum': 1, 'toolCallId': 'c1',
                                 'status': 'done', 'tEnd': _T0 + 63_000}),
        ('round_start', 64_000, {'roundNum': 2}),
        ('round_end', 65_000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 65_000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        tool = _spans(doc, 'tool')[0]
        (admission,) = _spans(doc, 'admission_wait')
        assert admission['parent'] == tool['spanId']
        assert admission['depth'] == tool['depth'] + 1
        assert (admission['tStart'], admission['tEnd']) == (_T0 + 2_100, _T0 + 62_000)
        assert admission['tEnd'] < tool['tEnd']  # admission, not tool completion
        assert admission['status'] == 'done'
        # The waiter occurrence and the holder/lock facts identify the wait.
        assert admission['attrs'] == {
            'waiterTool': 'write_file', 'holderTool': 'run_command',
            'holderConversation': 'conv-holder', 'heldMs': 61_000, 'mode': 'write',
            'lock': 'workspace:repo',
            'waiterCallId': 'c1', 'waiterRoundKey': '1', 'waiterQuery': ''}
        assert 'budgetMs' not in admission  # declared unbounded
        s = doc['summary']
        assert s['waitMs'] == 59_900
        assert (s['admissionWaitMs'], s['retryWaitMs'], s['spawnWaitMs']) == (59_900, 0, 0)
        assert s['toolMs'] == 100 + 1000  # announce sliver + post-admission run
        assert set(s) == {'totalMs', 'llmMs', 'toolMs', 'waitMs', 'compactionMs',
                          'approvalWaitMs', 'flowStepMs', 'orchestrationMs',
                          'unattributedMs', 'ttftMs', 'overBudget',
                          'admissionWaitMs', 'retryWaitMs', 'spawnWaitMs',
                          'startupMs', 'roundPrepMs', 'roundSettleMs', 'finalizeMs'}
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_admission_wait_span_names_both_requests():
    """One span diagnoses a stall: the waiter's claims and the holder's request."""
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'run_command',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        ('workspace_admission_wait', 9_010, _admission(
            2_100, 9_000, roundNum=1, toolCallId='c1', toolName='run_command',
            lockDomain='tree', lockRoot='tofu', holderTask='task-holder',
            waiterClaims='tree:tofu[git-uncatalogued],metadata:tofu[git-uncatalogued]',
            holderMode='file-write', holderClaims='tree:bughunt[unattributed-effect]',
            holderCommand='cd bughunt && pytest -n 6')),
        ('tool_result', 10_000, {'roundNum': 1, 'toolCallId': 'c1',
                                 'status': 'done', 'tEnd': _T0 + 10_000}),
        ('done', 10_000, {'finishReason': 'stop'}),
    ])
    try:
        (admission,) = _spans(_fold(tid), 'admission_wait')
        assert admission['attrs'] == {
            'waiterTool': 'run_command', 'holderTool': 'run_command',
            'holderConversation': 'conv-holder', 'heldMs': 61_000, 'mode': 'write',
            'lock': 'tree:tofu',
            'waiterClaims': 'tree:tofu[git-uncatalogued],metadata:tofu[git-uncatalogued]',
            'holderTask': 'task-holder', 'holderMode': 'file-write',
            'holderClaims': 'tree:bughunt[unattributed-effect]',
            'holderCommand': 'cd bughunt && pytest -n 6',
            'waiterCallId': 'c1', 'waiterRoundKey': '1', 'waiterQuery': ''}
    finally:
        _cleanup(tid)


def test_admission_wait_outranks_spawn_and_approval_spans_that_enclose_it():
    from lib.tasks_pkg import turn_trace

    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        # c1: approved at ~3s, then queued 10s for the lease, then spawned.
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'run_command',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        ('write_approval_request', 2500, {'roundNum': 1, 'toolCallId': 'c1'}),
        ('workspace_admission_wait', 13_000, _admission(
            3_000, 13_000, roundNum=1, toolCallId='c1')),
        ('tool_progress', 13_100, {'roundNum': 1, 'toolCallId': 'c1',
                                   'execStartTs': _T0 + 13_100}),
        ('tool_result', 14_000, {'roundNum': 1, 'toolCallId': 'c1',
                                 'status': 'done', 'tEnd': _T0 + 14_000}),
        # c2: no approval; its whole announce->spawn delay was a lease wait.
        ('tool_start', 20_000, {'roundNum': 1, 'toolName': 'run_command',
                                'toolCallId': 'c2', 'tStart': _T0 + 20_000}),
        ('workspace_admission_wait', 30_000, _admission(
            20_050, 30_000, roundNum=1, toolCallId='c2')),
        ('tool_progress', 30_200, {'roundNum': 1, 'toolCallId': 'c2',
                                   'execStartTs': _T0 + 30_200}),
        ('tool_result', 31_000, {'roundNum': 1, 'toolCallId': 'c2',
                                 'status': 'done', 'tEnd': _T0 + 31_000}),
        ('done', 31_000, {'finishReason': 'stop'}),
    ])
    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(_SEEDED_ROWS.get(selected, ()))
    try:
        ledger = {}
        doc = turn_trace.fold_task_trace(tid, ledger_out=ledger)
        assert len(_spans(doc, 'spawn_wait')) == 1  # c2's announce->spawn delay
        assert len(_spans(doc, 'approval_wait')) == 1
        kinds = ledger['kindMs']
        assert kinds['admission_wait'] == 10_000 + 9_950
        assert kinds['approval_wait'] == (3_000 - 2_500) + (13_100 - 13_000)
        assert kinds['spawn_wait'] == 50 + 200  # only the unexplained remainder
        s = doc['summary']
        assert s['approvalWaitMs'] == kinds['approval_wait']
        assert s['waitMs'] == kinds['admission_wait'] + kinds['spawn_wait'] + kinds['retry_wait']
        assert ledger['rounds'] == 1
        # Readers of the bounded document re-derive the same split.
        assert turn_trace.trace_kind_attribution(doc) == kinds
        _assert_strict_accounting(doc)
    finally:
        turn_trace._read_trace_rows = original
        _cleanup(tid)


def test_admission_wait_without_tool_is_turn_level_and_backgrounded_wait_is_clipped():
    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('workspace_admission_wait', 1500, _admission(1_100, 1_400, outcome='refused')),
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'run_command',
                              'toolCallId': 'bg', 'tStart': _T0 + 2000}),
        # Handed off to the background at 5s while still queued for the lease.
        ('tool_result', 5000, {'roundNum': 1, 'toolCallId': 'bg',
                               'status': 'done', 'tEnd': _T0 + 5000}),
        ('round_start', 6000, {'roundNum': 2}),
        ('workspace_admission_wait', 9000, _admission(
            2_500, 9_000, roundNum=1, toolCallId='bg', outcome='cancelled')),
        ('round_end', 10_000, {'roundNum': 2, 'reason': 'final'}),
        ('done', 10_000, {'finishReason': 'stop'}),
    ])
    try:
        doc = _fold(tid)
        flat, child = sorted(_spans(doc, 'admission_wait'), key=lambda s: s['depth'])
        assert (flat['parent'], flat['depth'], flat['status']) == ('turn', 1, 'error')
        assert (flat['tStart'], flat['tEnd']) == (_T0 + 1_100, _T0 + 1_400)
        tool = _spans(doc, 'tool')[0]
        assert child['parent'] == tool['spanId'] and child['status'] == 'aborted'
        # The model round that ran after the handoff stays model time.
        assert (child['tStart'], child['tEnd']) == (_T0 + 2_500, _T0 + 5_000)
        assert doc['summary']['waitMs'] == 300 + 2_500
        _assert_strict_accounting(doc)
    finally:
        _cleanup(tid)


def test_admission_wait_is_a_declared_trace_payload_and_bucket():
    from lib.agent_core.events import EventType, event_types
    from lib.tasks_pkg import turn_trace

    assert EventType.WORKSPACE_ADMISSION_WAIT in event_types()
    assert EventType.WORKSPACE_ADMISSION_WAIT in turn_trace._TRACE_PAYLOAD_TYPES
    assert dict(turn_trace._BUCKET_PRIORITY)['admission_wait'] == 'waitMs'
    kinds = [kind for kind, _ in turn_trace._BUCKET_PRIORITY]
    assert kinds.index('admission_wait') < kinds.index('tool')
    assert kinds.index('admission_wait') < kinds.index('spawn_wait')


def test_terminal_settlement_logs_one_summary_line_from_the_frozen_fold(caplog):
    import logging
    from lib.tasks_pkg import turn_trace

    tid = _tid()
    _seed(tid, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 100, {'phase': 'retrying', 'attempt': 1}),
        ('delta', 400, {}),  # first output ends the backoff
        ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'}),
        ('tool_start', 2000, {'roundNum': 1, 'toolName': 'run_command',
                              'toolCallId': 'c1', 'tStart': _T0 + 2000}),
        ('workspace_admission_wait', 7_000, _admission(
            2_000, 7_000, roundNum=1, toolCallId='c1')),
        ('tool_result', 8_000, {'roundNum': 1, 'toolCallId': 'c1',
                                'status': 'done', 'tEnd': _T0 + 8_000}),
        ('round_start', 9_000, {'roundNum': 2}),
        ('round_end', 10_000, {'roundNum': 2, 'reason': 'final'}),
    ])
    task = {'id': tid, 'convId': 'conv-summary'}
    original = turn_trace._read_trace_rows
    turn_trace._read_trace_rows = lambda selected: list(_SEEDED_ROWS.get(selected, ()))
    try:
        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.turn_trace'):
            for _ in range(2):  # CAS rebase / retained-receipt retry rebuild
                projection = turn_trace.finalize_trace_projection(
                    {}, task, {'type': 'done', 'finishReason': 'stop',
                               'executionAtMs': _T0 + 10_000},
                    now_ms=_T0 + 10_000, pending_sequence=10)
        lines = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[TurnTraceSummary]')]
        assert len(lines) == 1, lines
        fields = dict(part.split('=', 1) for part in lines[0].split()[1:])
        summary = projection['timingTrace']['summary']
        assert fields['task'] == tid and fields['conv'] == 'conv-summary'
        assert fields['status'] == 'done' and fields['rounds'] == '2'
        for key in ('totalMs', 'llmMs', 'toolMs', 'waitMs', 'compactionMs',
                    'approvalWaitMs', 'unattributedMs', 'ttftMs', 'flowStepMs',
                    'admissionWaitMs', 'retryWaitMs', 'spawnWaitMs'):
            assert int(fields[key]) == summary[key], key
        assert int(fields['admissionWaitMs']) == 5_000
        assert int(fields['retryWaitMs']) == 300
        assert int(fields['admissionWaitMs']) + int(fields['retryWaitMs']) \
            + int(fields['spawnWaitMs']) == summary['waitMs']
        assert list(fields)[:16] == [
            'task', 'conv', 'status', 'totalMs', 'llmMs', 'toolMs', 'waitMs',
            'admissionWaitMs', 'retryWaitMs', 'compactionMs', 'approvalWaitMs',
            'unattributedMs', 'ttftMs', 'rounds', 'spawnWaitMs', 'flowStepMs']
    finally:
        turn_trace._read_trace_rows = original
        _cleanup(tid)


def test_wait_split_stays_exact_when_the_bounded_document_drops_spans():
    """Long tasks exceed the span bound; only the persisted split stays exact."""
    from lib.tasks_pkg import turn_trace

    tid = _tid()
    events = [('round_start', 0, {'roundNum': 1}),
              ('round_end', 1000, {'roundNum': 1, 'reason': 'tools'})]
    calls = 200
    for index in range(calls):
        base = 2_000 + index * 1_000
        events += [
            ('tool_start', base, {'roundNum': 1, 'toolName': 'run_command',
                                  'toolCallId': f'c{index}', 'tStart': _T0 + base}),
            ('workspace_admission_wait', base + 600, _admission(
                base + 100, base + 600, roundNum=1, toolCallId=f'c{index}')),
            ('tool_result', base + 900, {'roundNum': 1, 'toolCallId': f'c{index}',
                                         'status': 'done', 'tEnd': _T0 + base + 900}),
        ]
    end = 2_000 + calls * 1_000
    events += [('round_start', end, {'roundNum': 2}),
               ('round_end', end + 1_000, {'roundNum': 2, 'reason': 'final'}),
               ('done', end + 1_000, {'finishReason': 'stop'})]
    _seed(tid, events)
    try:
        doc = _fold(tid)
        s = doc['summary']
        assert doc['droppedSpans'] > 0
        assert s['admissionWaitMs'] == calls * 500
        assert s['admissionWaitMs'] + s['retryWaitMs'] + s['spawnWaitMs'] == s['waitMs']
        # The stored spans no longer carry every wait: recomputing from them
        # is only a lower bound, which is why the split is persisted.
        assert turn_trace.trace_kind_attribution(doc)['admission_wait'] < s['admissionWaitMs']
        # Gaps are bounded too, so only the bucket partition stays exact here.
        assert sum(s[key] for key in (
            'llmMs', 'toolMs', 'waitMs', 'compactionMs', 'approvalWaitMs',
            'flowStepMs', 'orchestrationMs', 'unattributedMs')) == s['totalMs'] == doc['totalMs']
    finally:
        _cleanup(tid)


def test_absolute_byte_fail_safe_keeps_every_aggregate_summary_field(monkeypatch):
    import lib.turn_trace_contract as contract

    summary = {'totalMs': 13, 'llmMs': 1, 'toolMs': 1, 'waitMs': 3, 'compactionMs': 1,
               'approvalWaitMs': 1, 'flowStepMs': 1, 'orchestrationMs': 4,
               'unattributedMs': 1, 'ttftMs': 1,
               'admissionWaitMs': 1, 'retryWaitMs': 1, 'spawnWaitMs': 1,
               'startupMs': 1, 'roundPrepMs': 1, 'roundSettleMs': 1, 'finalizeMs': 1,
               'overBudget': [{'spanId': 's', 'kind': 'tool', 'name': 'n',
                               'elapsedMs': 2, 'budgetMs': 1}]}
    monkeypatch.setattr(contract, 'TRACE_MAX_PERSISTED_BYTES', 1)
    compacted = contract.compact_trace_document(
        {'version': 1, 'taskId': 'task-fail-safe', 'summary': dict(summary)})
    assert compacted['detailCompacted'] is True
    assert compacted['summary'] == {**summary, 'overBudget': []}


def test_summary_lines_of_distinct_tasks_are_never_coalesced():
    import logging
    from lib.logging_pkg.log_rate_limit import DuplicateCoalescingFilter

    coalescer = DuplicateCoalescingFilter()
    records = [
        logging.LogRecord('lib.tasks_pkg.turn_trace', logging.INFO, __file__, 1,
                          '[TurnTraceSummary] task=%s conv=c status=done totalMs=%d',
                          (f'task-{index}', 1000), None)
        for index in range(64)]
    assert all(coalescer.filter(record) for record in records)
    assert {record.tofu_occurrence_delta for record in records} == {1}


# ── 13. Event read failure is not an empty log ──

def test_event_read_failure_raises_with_full_task_id_and_bounded_detail(
        monkeypatch, caplog):
    import logging
    import lib.storage
    from lib.storage.errors import StorageError
    from lib.tasks_pkg import turn_trace

    tid = _tid() + '-' + uuid.uuid4().hex
    message = 'storage.v2 codec failure: ' + 'x' * 5_000

    class Client:
        def query(self, operation, payload):
            raise StorageError('database_protocol_error', message)

    class Recovered:
        def query(self, operation, payload):
            if operation != 'event.list' or payload['after_sequence'] >= 0:
                return []
            return [{'sequence': 0, 'created_at_ms': _T0,
                     'event': {'type': 'round_start', 'roundNum': 1}}]

    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: Client())
    with caplog.at_level(logging.WARNING, logger='lib.tasks_pkg.turn_trace'):
        with pytest.raises(turn_trace.TurnTraceEventReadError):
            turn_trace._read_trace_rows(tid)
        # A live fold must not present the failure as an unknown/expired task.
        with pytest.raises(turn_trace.TurnTraceEventReadError):
            turn_trace.fold_task_trace(tid, now_ms=_T0)
    # The failure was not cached as an empty log: the next read sees the rows.
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: Recovered())
    assert [row['type'] for row in turn_trace._read_trace_rows(tid)] == ['round_start']
    lines = [r.getMessage() for r in caplog.records
             if 'event read failed' in r.getMessage()]
    assert lines, caplog.records
    assert f'task={tid} ' in lines[0]
    assert 'code=database_protocol_error' in lines[0]
    assert "message='storage.v2 codec failure: xxx" in lines[0]
    assert len(lines[0]) < 1_000



@pytest.mark.parametrize('code', [
    'storage_payload_too_large',
    # Engines built before the stream point-read bound was classified report
    # it as a protocol refusal whose sanitized message is only its code.
    'database_protocol_error',
])
def test_event_page_size_refusal_halves_the_page_at_the_same_cursor(monkeypatch, code):
    import lib.storage
    from lib.storage.errors import StorageError
    from lib.tasks_pkg import turn_trace

    events = [{'sequence': sequence, 'created_at_ms': _T0 + sequence,
               'event': {'type': 'round_start', 'roundNum': sequence + 1}}
              for sequence in range(3)]
    calls = []

    class Client:
        def query(self, operation, payload):
            assert operation == 'event.list'
            calls.append((payload['after_sequence'], payload['limit']))
            if payload['limit'] > 1:
                raise StorageError(code, code)
            return [row for row in events
                    if row['sequence'] > payload['after_sequence']][:payload['limit']]

    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: Client())
    rows = turn_trace._read_trace_rows(_tid())
    assert [(row['event_id'], row['payload']['roundNum']) for row in rows] == [
        (0, 1), (1, 2), (2, 3)]
    assert calls[:10] == [(-1, limit) for limit in [
        1000, 500, 250, 125, 62, 31, 15, 7, 3, 1]]

def test_settlement_read_failure_freezes_marked_partial_trace_not_zero_round_fold(
        monkeypatch, caplog):
    """2026-10-27: a failed read at settlement froze a 10,586 s, 217-round run
    as one unattributed gap with zero rounds, and the API served it forever."""
    import logging
    from debug.turn_trace_audit import parse_summary_line
    from lib.conversation_sync.validation import decode
    from lib.tasks_pkg import turn_trace

    tid = _tid()
    end = _T0 + 10_586_000
    clock = {'executionClockId': 'clock-a', 'executionOriginAtMs': _T0}
    terminal = {'type': 'done', 'finishReason': 'stop',
                'executionAtMs': end, **clock}
    task = {'id': tid, 'convId': 'conv-read-failure'}
    turn_trace.observe_task_trace_event(
        task, {'type': 'phase', 'phase': 'waiting_model'},
        observed_at_ms=_T0 + 5_000)
    previous = {'timingTrace': {'version': 1, 'taskId': tid, 'clientObservations': [{
        'observationId': 'paint:1', 'kind': 'phase_painted', 'taskId': tid,
        'attemptId': 'attempt-1', 'clientId': 'page-a', 'recordedAt': _T0 + 6_000}]}}

    def failed_read(selected):
        raise turn_trace.TurnTraceEventReadError(f'read failed task={selected}')

    def summary_lines():
        return [r.getMessage() for r in caplog.records
                if r.getMessage().startswith('[TurnTraceSummary]')]

    monkeypatch.setattr(turn_trace, '_read_trace_rows', failed_read)
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.turn_trace'):
        projection = turn_trace.finalize_trace_projection(
            previous, task, terminal, now_ms=end, pending_sequence=500)
        # A CAS-rebase rebuild whose read still fails neither changes the
        # document nor repeats the summary line.
        rebuilt = turn_trace.finalize_trace_projection(
            previous, task, terminal, now_ms=end, pending_sequence=500)
    trace = projection['timingTrace']
    assert rebuilt['timingTrace'] == trace
    assert (trace['coverage'], trace['coverageReason']) == ('partial', 'event-read-failed')
    assert (trace['status'], trace['running'], trace['source']) == (
        'done', False, 'turn-snapshot')
    assert trace['eventsAvailable'] is True and trace['eventLogAvailable'] is False
    # Nothing the failed read would have had to prove is fabricated ...
    assert not {'summary', 'spans', 'gaps'} & set(trace)
    # ... while evidence that never came from the event log survives.
    assert (trace['tStart'], trace['tEnd'], trace['totalMs']) == (_T0, end, 10_586_000)
    assert trace['clockSource'] == 'server-monotonic'
    assert trace['statusHistory'][0]['phase'] == 'waiting_model'
    assert trace['statusHistory'][-1]['tEnd'] == end
    assert [o['observationId'] for o in trace['clientObservations']] == ['paint:1']
    decode('TurnProjection', projection)
    lines = summary_lines()
    assert len(lines) == 1, lines
    fields = dict(part.split('=', 1) for part in lines[0].split()[1:])
    assert fields == {'task': tid, 'conv': 'conv-read-failure', 'status': 'done',
                      'coverage': 'partial', 'coverageReason': 'event-read-failed'}
    assert parse_summary_line(lines[0]) is None  # no bucket an aggregator could sum

    # A successful read (e.g. the next rebuild) is the unchanged full fold and
    # still logs its one real summary line.
    _seed(tid, [
        ('round_start', 1_000, {'roundNum': 1, **clock}),
        ('round_end', 9_000, {'roundNum': 1, 'reason': 'final', **clock}),
    ])
    monkeypatch.setattr(turn_trace, '_read_trace_rows',
                        lambda selected: list(_SEEDED_ROWS.get(selected, ())))
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.turn_trace'):
            for _ in range(2):
                healed = turn_trace.finalize_trace_projection(
                    previous, task, terminal, now_ms=end, pending_sequence=2)
    finally:
        _cleanup(tid)
    healed_trace = healed['timingTrace']
    assert healed_trace['coverage'] == 'full' and 'coverageReason' not in healed_trace
    assert healed_trace['eventLogAvailable'] is True
    assert len([s for s in healed_trace['spans'] if s['kind'] == 'round']) == 1
    _assert_strict_accounting(healed_trace)
    lines = summary_lines()
    assert len(lines) == 1, lines
    record = parse_summary_line(lines[0])
    assert record['rounds'] == 1 and record['coverage'] == 'full'
    assert record['totalMs'] == healed_trace['summary']['totalMs'] == 10_586_000


# ── 12. Route registration ──

def test_trace_route_registered_on_v1_blueprint():
    from quart import Quart
    from werkzeug.datastructures import ImmutableDict

    from routes.api_v1.tasks import api_v1_tasks_bp
    # Quart 0.19's defaults predate the Flask 3.1 key read by
    # ``add_url_rule`` (same compatibility default as the inspector suite).
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = ImmutableDict({**Quart.default_config,
                                              'PROVIDE_AUTOMATIC_OPTIONS': True})
    app = Quart(__name__)
    app.register_blueprint(api_v1_tasks_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/v1/tasks/<task_id>/trace' in rules


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))

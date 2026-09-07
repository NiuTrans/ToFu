"""Turn Trace — the unified per-task timing fold (pytest suite).

Contract: docs/TURN_TRACE_CONTRACT.md. Verifies
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
ROOT = os.path.normpath(os.path.join(HERE, '..'))

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
             s['approvalWaitMs'], s['unattributedMs']]
    assert sum(parts) == s['totalMs'] == doc['totalMs'], (
        f'buckets {parts} must sum to totalMs {doc["totalMs"]}')
    gap_ms = sum(g['tEnd'] - g['tStart'] for g in doc['gaps'])
    assert gap_ms == s['unattributedMs'], (
        f'gaps {gap_ms} must equal unattributedMs {s["unattributedMs"]}')


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
                    if span['attrs'].get('attemptLevel')]
        assert len(attempts) == 2
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
        assert by_name['write_file']['budgetMs'] == 15000
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
        '(ttft | retry_wait | compaction | covered | ignore)')
    dead = rules - phase_values()
    assert not dead, f'trace rules for unregistered phases: {sorted(dead)}'
    legal = {'ttft', 'retry_wait', 'compaction', 'covered', 'ignore'}
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
    from lib.storage_sidecar.operation_domains.turns import OPERATIONS

    operation = OPERATIONS['turn.perception.record']
    assert operation.kind == 'command'
    assert operation.receipt_required is False
    assert operation.after is None


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

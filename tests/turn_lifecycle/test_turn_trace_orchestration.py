"""Turn Trace orchestration windows: every startup/round/settlement ms is named.

Contract: docs/contracts/TURN_TRACE_CONTRACT.md (startup, round_prep,
round_settle, finalize; round identity of requests, usage and tools). Rows are
synthetic and carry the server execution clock, so the fold starts at the
execution origin like a real HTTP turn.
"""

import pytest

from lib.tasks_pkg import turn_trace as trace

pytestmark = pytest.mark.unit

_ORIGIN = 1_000_000


def _fold(monkeypatch, events, *, now=None):
    rows = []
    for index, (kind, offset, payload) in enumerate(events):
        body = {'type': kind, 'executionClockId': 'clock',
                'executionOriginAtMs': _ORIGIN, 'executionAtMs': _ORIGIN + offset,
                **payload}
        rows.append({'event_id': index, 'type': kind, 'ts_ms': _ORIGIN + offset,
                     'payload': body})
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    return trace.fold_task_trace('orchestration', now_ms=_ORIGIN + (now or 0))


def _working(key):
    return {'phase': 'working', 'detailKey': key}


def _named(doc, kind):
    return {(s['name'], s['tStart'] - _ORIGIN, s['tEnd'] - _ORIGIN)
            for s in doc['spans'] if s['kind'] == kind and s['depth'] == 2}


def _accounted(summary):
    buckets = ('llmMs', 'toolMs', 'waitMs', 'compactionMs', 'approvalWaitMs',
               'flowStepMs', 'orchestrationMs', 'unattributedMs')
    splits = ('startupMs', 'roundPrepMs', 'roundSettleMs', 'finalizeMs')
    assert sum(summary[key] for key in buckets) == summary['totalMs']
    assert sum(summary[key] for key in splits) == summary['orchestrationMs']


def test_startup_is_split_into_named_stages_and_the_queue_is_not_a_retry(monkeypatch):
    doc = _fold(monkeypatch, [
        ('phase', 3_000, {'phase': 'executor_queued', 'queuePosition': 1}),
        ('phase', 4_000, _working('stream.phase.workerStarting')),
        ('phase', 5_000, _working('translationWait.turn')),
        ('phase', 6_000, _working('stream.phase.startupConfig')),
        ('phase', 6_100, _working('stream.phase.startupTools')),
        ('phase', 7_000, _working('stream.phase.startupContext')),
        ('memory_prefetch', 17_000, {'phase': 'started', 'total_memories': 1879}),
        ('memory_prefetch', 17_100, {'phase': 'done', 'total_ms': 10_100}),
        ('round_start', 18_000, {'roundNum': 0}),
        ('round_end', 20_000, {'roundNum': 0}),
        ('done', 20_000, {}),
    ])
    assert _named(doc, 'startup') == {
        ('dispatch', 0, 3_000), ('executor_queue', 3_000, 4_000),
        ('worker_start', 4_000, 5_000), ('translation', 5_000, 6_000),
        ('config', 6_000, 6_100), ('tools', 6_100, 7_000),
        ('memory_load', 7_000, 17_000), ('memory_select', 17_000, 17_100),
        ('context', 17_100, 18_000)}
    (parent,) = [s for s in doc['spans'] if s['kind'] == 'startup' and s['depth'] == 1]
    assert (parent['tStart'] - _ORIGIN, parent['tEnd'] - _ORIGIN) == (0, 18_000)
    s = doc['summary']
    assert s['startupMs'] == 18_000 and s['retryWaitMs'] == s['waitMs'] == 0
    assert s['unattributedMs'] == 0
    _accounted(s)


def test_only_a_memory_store_load_is_named_memory_load(monkeypatch):
    skipped = _fold(monkeypatch, [
        ('phase', 1_000, _working('stream.phase.startupContext')),
        ('memory_prefetch', 1_200, {'phase': 'skipped', 'reason': 'disabled'}),
        ('round_start', 3_000, {'roundNum': 0}),
        ('round_end', 3_500, {'roundNum': 0}),
        ('done', 3_500, {}),
    ])
    assert _named(skipped, 'startup') == {('dispatch', 0, 1_000), ('context', 1_000, 3_000)}
    empty = _fold(monkeypatch, [
        ('phase', 1_000, _working('stream.phase.startupContext')),
        ('memory_prefetch', 2_000, {'phase': 'skipped', 'reason': 'no_memories'}),
        ('round_start', 3_000, {'roundNum': 0}),
        ('round_end', 3_500, {'roundNum': 0}),
        ('done', 3_500, {}),
    ])
    assert _named(empty, 'startup') == {
        ('dispatch', 0, 1_000), ('memory_load', 1_000, 2_000), ('context', 2_000, 3_000)}


def test_startup_ends_at_the_first_execution_boundary_or_terminal(monkeypatch):
    failed = _fold(monkeypatch, [
        ('phase', 1_000, _working('stream.phase.startupConfig')),
        ('error', 4_000, {'error': 'config'}),
    ])
    stages = [s for s in failed['spans'] if s['kind'] == 'startup']
    assert {(s['name'], s['status']) for s in stages} == {
        ('startup', 'error'), ('dispatch', 'done'), ('config', 'error')}
    assert failed['summary']['startupMs'] == 4_000

    running = _fold(monkeypatch, [
        ('phase', 1_000, _working('stream.phase.startupContext')),
    ], now=9_000)
    assert _named(running, 'startup') == {('dispatch', 0, 1_000), ('context', 1_000, 9_000)}
    assert {s['status'] for s in running['spans'] if s['kind'] == 'startup'} == {
        'done', 'running'}
    # A notice after the first round never reopens startup.
    late = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 0}),
        ('phase', 500, _working('stream.phase.startupConfig')),
        ('round_end', 1_000, {'roundNum': 0}),
        ('done', 1_000, {}),
    ])
    assert not [s for s in late['spans'] if s['kind'] == 'startup']


def test_round_window_is_carved_around_its_logical_requests(monkeypatch):
    # Request/usage roundNum is the 1-based tag; tool roundNum is the tool-call
    # allocator. Ownership follows the round open at each event.
    doc = _fold(monkeypatch, [
        ('round_start', 1_000, {'roundNum': 0}),
        ('model_request_start', 1_400, {'spanId': 'm1', 'roundNum': 1,
                                        'requestLevel': 'logical'}),
        ('model_request_start', 1_450, {'spanId': 'm1.a1', 'roundNum': 1,
                                        'requestLevel': 'provider_attempt',
                                        'parentSpanId': 'm1'}),
        ('model_request_complete', 4_000, {'spanId': 'm1.a1', 'status': 'succeeded'}),
        ('model_request_complete', 4_000, {'spanId': 'm1', 'status': 'succeeded'}),
        ('tool_start', 4_100, {'roundNum': 7, 'toolCallId': 'c1', 'toolName': 'read_files',
                               'tStart': _ORIGIN + 4_100}),
        ('round_usage', 4_150, {'roundNum': 1, 'model': 'm', 'usage': {}}),
        ('tool_result', 5_000, {'roundNum': 7, 'toolCallId': 'c1', 'status': 'done',
                                'tEnd': _ORIGIN + 5_000}),
        ('round_end', 5_300, {'roundNum': 0}),
        ('round_start', 5_400, {'roundNum': 1}),
        ('model_request_start', 5_500, {'spanId': 'm2', 'roundNum': 2,
                                        'requestLevel': 'logical'}),
        ('model_request_complete', 7_000, {'spanId': 'm2', 'status': 'succeeded'}),
        ('round_end', 7_200, {'roundNum': 1}),
        ('done', 7_500, {}),
    ])
    spans = {s['spanId']: s for s in doc['spans']}
    assert spans['m1']['parent'] == 'r0.llm' and spans['m2']['parent'] == 'r1.llm'
    assert spans['m1.a1']['parent'] == 'm1'
    (tool,) = [s for s in doc['spans'] if s['kind'] == 'tool']
    assert tool['parent'] == 'r0' and tool['attrs']['roundKey'] == '7'
    assert [a['model'] for a in spans['r0.llm']['attrs']['attempts']] == ['m']
    assert 'attempts' not in spans['r1.llm']['attrs']
    assert _named(doc, 'round_prep') == {('prep', 1_000, 1_400), ('prep', 5_400, 5_500)}
    # Round 0 settles until the next round starts; the last round until its end.
    assert _named(doc, 'round_settle') == {('settle', 4_000, 5_400),
                                           ('settle', 7_000, 7_200)}
    (final,) = [s for s in doc['spans'] if s['kind'] == 'finalize']
    assert (final['tStart'] - _ORIGIN, final['tEnd'] - _ORIGIN) == (7_200, 7_500)
    s = doc['summary']
    assert s['llmMs'] == 2_600 + 1_500
    assert s['toolMs'] == 900
    assert s['roundPrepMs'] == 500
    assert s['roundSettleMs'] == (5_400 - 4_000 - 900) + 200
    assert (s['startupMs'], s['finalizeMs'], s['unattributedMs']) == (1_000, 300, 0)
    _accounted(s)


def test_a_round_without_a_started_request_is_still_preparing(monkeypatch):
    doc = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 0}),
        ('model_request_start', 100, {'spanId': 'm1', 'requestLevel': 'logical'}),
        ('model_request_complete', 900, {'spanId': 'm1', 'status': 'succeeded'}),
        ('round_end', 1_000, {'roundNum': 0}),
        ('round_start', 1_000, {'roundNum': 1}),
    ], now=4_000)
    assert ('prep', 1_000, 4_000) in _named(doc, 'round_prep')
    assert {s['status'] for s in doc['spans']
            if s['kind'] == 'round_prep' and s['tEnd'] - _ORIGIN == 4_000} == {'running'}
    assert not [s for s in doc['spans'] if s['kind'] == 'finalize']  # still running


def test_spawn_wait_names_its_command_without_its_parent_span(monkeypatch):
    doc = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 0}),
        ('tool_start', 100, {'roundNum': 3, 'toolCallId': 'c9', 'toolName': 'run_command',
                             'query': 'pytest -q', 'tStart': _ORIGIN + 100}),
        ('tool_progress', 2_000, {'roundNum': 3, 'toolCallId': 'c9',
                                  'execStartTs': _ORIGIN + 1_900}),
        ('tool_result', 3_000, {'roundNum': 3, 'toolCallId': 'c9', 'status': 'done',
                                'tEnd': _ORIGIN + 3_000}),
        ('round_end', 3_000, {'roundNum': 0}),
        ('done', 3_000, {}),
    ])
    (spawn,) = [s for s in doc['spans'] if s['kind'] == 'spawn_wait']
    assert spawn['attrs'] == {'toolName': 'run_command', 'toolCallId': 'c9',
                              'roundKey': '3', 'query': 'pytest -q'}
    assert doc['summary']['spawnWaitMs'] == 1_800


def test_admission_wait_names_its_waiting_call_and_both_lease_sides(monkeypatch):
    doc = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 0}),
        ('tool_start', 50, {'roundNum': 5, 'toolCallId': 'c5', 'toolName': 'run_command',
                            'query': 'git log --oneline', 'tStart': _ORIGIN + 50}),
        ('workspace_admission_wait', 2_000, {
            'roundNum': 5, 'toolCallId': 'c5', 'toolName': 'run_command',
            'waitStartedAtMs': _ORIGIN + 100, 'waitEndedAtMs': _ORIGIN + 1_900,
            'outcome': 'admitted', 'mode': 'write', 'lockDomain': 'workspace',
            'lockRoot': 'tofu', 'waiterClaims': 'tree:tofu', 'holderTool': 'run_command',
            'holderTask': 'task-holder', 'holderMode': 'write', 'holderClaims': 'tree:tofu',
            'holderCommand': 'pytest -q', 'holderHeldMs': 9_000}),
        ('tool_result', 3_000, {'roundNum': 5, 'toolCallId': 'c5', 'status': 'done',
                                'tEnd': _ORIGIN + 3_000}),
        ('round_end', 3_000, {'roundNum': 0}),
        ('done', 3_000, {}),
    ])
    (admission,) = [s for s in doc['spans'] if s['kind'] == 'admission_wait']
    attrs = admission['attrs']
    assert {key: attrs[key] for key in (
        'waiterTool', 'waiterCallId', 'waiterRoundKey', 'waiterQuery', 'waiterClaims')} == {
        'waiterTool': 'run_command', 'waiterCallId': 'c5', 'waiterRoundKey': '5',
        'waiterQuery': 'git log --oneline', 'waiterClaims': 'tree:tofu'}
    assert {key: attrs[key] for key in (
        'holderTool', 'holderTask', 'holderMode', 'holderClaims', 'holderCommand',
        'heldMs', 'lock')} == {
        'holderTool': 'run_command', 'holderTask': 'task-holder', 'holderMode': 'write',
        'holderClaims': 'tree:tofu', 'holderCommand': 'pytest -q', 'heldMs': 9_000,
        'lock': 'workspace:tofu'}

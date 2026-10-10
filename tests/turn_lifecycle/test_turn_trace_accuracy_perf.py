"""Synthetic trace accuracy and algorithmic budget tests; no live providers/stores."""
import random
import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import turn_trace as trace


def test_randomized_priority_reference():
    rng = random.Random(1234)
    kinds = [kind for kind, _ in trace._BUCKET_PRIORITY] + ['round']
    for _ in range(200):
        spans = [{'kind': rng.choice(kinds), 'tStart': rng.randrange(-10, 50),
                  'tEnd': rng.randrange(-10, 60)} for _ in range(80)]
        actual = trace._disjoint_summary(spans, 0, 50)
        expected = {bucket: 0 for _, bucket in trace._BUCKET_PRIORITY}
        expected['unattributedMs'] = 0
        for tick in range(50):
            bucket = next((bucket for kind, bucket in trace._BUCKET_PRIORITY
                           if any(s['kind'] == kind and s['tStart'] <= tick
                                  and s['tEnd'] >= tick + 1 for s in spans)),
                          'unattributedMs')
            expected[bucket] += 1
        assert actual == expected


def test_ledger_reads_each_span_once():
    class Counted(dict):
        reads = 0
        def __getitem__(self, key):
            type(self).reads += 1
            return super().__getitem__(key)
        def get(self, key, default=None):
            type(self).reads += 1
            return super().get(key, default)
    for size in (100, 1000, 10000):
        Counted.reads = 0
        spans = [Counted(kind='llm', tStart=i, tEnd=i + 7) for i in range(size)]
        result = trace._disjoint_summary(spans, 0, size + 7)
        assert result['llmMs'] == size + 6
        assert Counted.reads <= size * 3


def _fold(monkeypatch, events, now=100):
    rows = [{'event_id': i, 'type': kind, 'ts_ms': timestamp,
             'payload': {'type': kind, **payload}}
            for i, (kind, timestamp, payload) in enumerate(events)]
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    return trace.fold_task_trace('synthetic', now_ms=now)


def test_heartbeats_do_not_split_wait_or_ttft(monkeypatch):
    result = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 1}),
        ('phase', 1, {'phase': 'waiting_model'}),
        ('phase', 2, {'phase': 'waiting_model'}),
        ('phase', 3, {'phase': 'retrying', 'attempt': 2}),
        ('phase', 4, {'phase': 'stream_stalled'}),
        ('phase', 5, {'phase': 'waiting_model'}),
        ('delta', 8, {}), ('round_end', 9, {'roundNum': 1}),
        ('done', 10, {}),
    ])
    waits = [s for s in result['spans'] if s['kind'] == 'retry_wait']
    ttft = [s for s in result['spans'] if s['kind'] == 'llm_ttft']
    assert len(waits) == len(ttft) == 1
    assert (waits[0]['tStart'], waits[0]['tEnd']) == (3, 8)
    assert (ttft[0]['tStart'], ttft[0]['tEnd']) == (1, 8)


def test_backward_clock_is_partial_and_clamped(monkeypatch):
    result = _fold(monkeypatch, [('round_start', 10, {'roundNum': 1}),
                                ('delta', 30, {}), ('done', 20, {})])
    assert result['coverageReason'] == 'clock-regression'
    assert result['coverage'] == 'partial'
    assert result['tEnd'] == 30
    assert result['totalMs'] == 20
    assert not result['running']


def test_physical_child_and_legacy_logical_evidence(monkeypatch):
    result = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 1}),
        ('model_request_start', 1, {'spanId': 'logical'}),
        ('model_request_start', 2, {'spanId': 'physical', 'requestLevel': 'provider_attempt',
                                    'parentSpanId': 'logical', 'attempt': 1,
                                    'attemptScope': 'dispatcher'}),
        ('model_request_complete', 4, {'spanId': 'physical', 'status': 'failed'}),
        ('model_request_complete', 5, {'spanId': 'logical', 'status': 'failed'}),
        ('done', 6, {}),
    ])
    spans = {s['spanId']: s for s in result['spans']}
    assert spans['logical']['attrs']['requestLevel'] == 'logical'
    assert not spans['logical']['attrs'].get('attemptLevel')
    assert spans['physical']['parent'] == 'logical'
    assert spans['physical']['depth'] == spans['logical']['depth'] + 1
    assert spans['physical']['attrs']['attemptLevel'] is True

    assert spans['physical']['attrs']['attemptScope'] == 'dispatcher'
    assert spans['physical']['status'] == 'error'


def test_capped_read_retains_terminal_beyond_prefix(monkeypatch):
    import lib.storage
    prefix = [{'sequence': i, 'created_at_ms': i,
               'event': {'type': 'round_start', 'roundNum': 1} if i == 0 else {'type': 'delta'}}
              for i in range(1000)]
    terminal = {'sequence': 1200, 'created_at_ms': 1200, 'event': {'type': 'done'}}
    latest = {'sequence': 1201, 'created_at_ms': 1201, 'event': {'type': 'round_usage'}}
    class Client:
        def query(self, operation, payload):
            if operation == 'event.latest':
                return latest
            if payload.get('types'):
                return [terminal]
            assert 'delta' not in payload['payload_types']
            return prefix
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: Client())
    monkeypatch.setattr(trace, 'TRACE_MAX_EVENT_ROWS', 1000)
    rows = trace._read_trace_rows_uncached('bounded')
    assert rows.truncated
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    result = trace.fold_task_trace('bounded', now_ms=1300)
    assert result['coverageReason'] == 'event-limit'
    assert result['status'] == 'done'
    assert result['running'] is False
    assert result['tEnd'] == 1200


@pytest.mark.parametrize('receipt,partial', [
    ({'deferredEvents': 20, 'deferredCheckpoints': 4}, False),
    ({'droppedEvents': 2}, True),
    ({'deliveryFailures': 1}, True),
    ({'flushIncomplete': True}, True),
    ({'diagnosticFailures': 1}, True),
])
def test_observer_loss_is_partial_not_repaired_by_cumulative_content(
        monkeypatch, receipt, partial):
    result = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 1}),
        ('model_request_start', 1, {'spanId': 'logical'}),
        ('model_request_complete', 5, {'spanId': 'logical',
                                      'status': 'succeeded',
                                      'observerIsolation': receipt}),
        ('done', 6, {}),
    ])
    assert result['coverage'] == ('partial' if partial else 'full')
    if partial:
        assert result['coverageReason'] == 'observer-gap'
    assert result['status'] == 'done'
    logical = next(s for s in result['spans'] if s['spanId'] == 'logical')
    assert logical['attrs']['observerIsolation'] == receipt


def test_fold_timing_observer_cannot_mask_original_failure(monkeypatch):
    failure = ValueError('synthetic fold failure')
    def fail(*args, **kwargs):
        raise failure
    def broken_log(*args, **kwargs):
        raise RuntimeError('synthetic log failure')
    monkeypatch.setattr(trace, '_fold_task_trace', fail)
    monkeypatch.setattr(trace.logger, 'info', broken_log)
    with pytest.raises(ValueError) as raised:
        trace.fold_task_trace('synthetic')
    assert raised.value is failure


@pytest.mark.parametrize('boundary', ['next-round', 'round-close', 'terminal', 'live'])
def test_open_approval_follows_tool_settlement(monkeypatch, boundary):
    events = [('round_start', 0, {'roundNum': 1})]
    if boundary != 'round-close':
        events.append(('round_end', 1, {'roundNum': 1}))
    events += [
        ('tool_start', 2, {'roundNum': 1, 'toolCallId': 'c'}),
        ('write_approval_request', 3, {'roundNum': 1, 'toolCallId': 'c'}),
    ]
    if boundary == 'next-round':
        events.append(('round_start', 10, {'roundNum': 2}))
    elif boundary == 'round-close':
        events.append(('round_end', 10, {'roundNum': 1}))
    if boundary != 'live':
        events.append(('done', 30, {}))
    result = _fold(monkeypatch, events, now=30)
    tool = next(s for s in result['spans'] if s['kind'] == 'tool')
    approval = next(s for s in result['spans'] if s['kind'] == 'approval_wait')
    end = 10 if boundary in ('next-round', 'round-close') else 30
    status = 'running' if boundary == 'live' else 'unknown'
    assert (tool['tStart'], tool['tEnd'], tool['status']) == (2, end, status)
    assert (approval['tStart'], approval['tEnd'], approval['status']) == (3, end, status)
    assert approval.get('truncated', False) == tool.get('truncated', False) == (boundary != 'live')
    assert approval['parent'] == tool['spanId']
    expected_llm = 21 if boundary == 'next-round' else 2 if boundary == 'round-close' else 1
    summary = result['summary']
    assert {k: summary[k] for k in ('totalMs', 'llmMs', 'toolMs', 'approvalWaitMs', 'unattributedMs')} == {
        'totalMs': 30, 'llmMs': expected_llm, 'toolMs': 1,
        'approvalWaitMs': end - 3, 'unattributedMs': 30 - expected_llm - 1 - (end - 3)}
    assert summary['waitMs'] == summary['compactionMs'] == 0


def test_repeated_requests_keep_occurrence_parents_and_latest_completion(monkeypatch):
    child = {'spanId': 'provider', 'requestLevel': 'provider_attempt', 'parentSpanId': 'logical'}
    result = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 1}),
        ('model_request_start', 1, {'spanId': 'logical'}),
        ('model_request_start', 2, child),
        ('model_request_start', 3, {'spanId': 'logical'}),
        ('model_request_start', 4, child),
        ('model_request_complete', 5, {'spanId': 'provider', 'status': 'succeeded'}),
        ('model_request_complete', 6, {'spanId': 'logical', 'status': 'succeeded'}),
        ('model_request_complete', 7, {'spanId': 'logical', 'status': 'failed'}),
        ('done', 8, {}),
    ])
    requests = sorted(
        (s for s in result['spans'] if 'requestLevel' in s['attrs']),
        key=lambda s: s['tStart'])
    first, old_child, second, new_child, orphan = requests
    assert len({s['spanId'] for s in result['spans']}) == len(result['spans'])
    assert first['spanId'] == 'logical'
    assert old_child['parent'] == first['spanId']
    assert new_child['parent'] == second['spanId']
    assert [(s['tEnd'], s['status']) for s in requests] == [
        (3, 'unknown'), (4, 'unknown'), (6, 'done'), (5, 'done'), (7, 'error')]
    assert first['truncated'] and old_child['truncated']
    assert orphan['spanId'] != second['spanId']


def test_request_ids_cannot_collide_with_generated_or_structural_names(monkeypatch):
    tokens = ['x', 'x', 'x.occ2', 'x.occ2', 'turn', 'r1', 'r1.llm', 'r2.llm']
    events = [('round_start', 0, {'roundNum': 1})]
    events += [('model_request_start', i, {'spanId': token})
               for i, token in enumerate(tokens, 1)]
    events += [('round_start', 10, {'roundNum': 2}),
               ('model_request_start', 11, {'spanId': 'later', 'roundNum': 2}),
               ('done', 12, {})]
    result = _fold(monkeypatch, events)
    spans = result['spans']
    assert len({s['spanId'] for s in spans}) == len(spans)
    later = next(s for s in spans if s['spanId'] == 'later')
    parent = next(s for s in spans if s['spanId'] == later['parent'])
    assert parent['name'] == 'llm' and parent['tStart'] == 10


def test_closed_approval_is_not_rewritten_by_tool_settlement(monkeypatch):
    result = _fold(monkeypatch, [
        ('round_start', 0, {'roundNum': 1}),
        ('round_end', 1, {'roundNum': 1}),
        ('tool_start', 2, {'roundNum': 1, 'toolCallId': 'c'}),
        ('write_approval_request', 3, {'roundNum': 1, 'toolCallId': 'c'}),
        ('tool_progress', 5, {'roundNum': 1, 'toolCallId': 'c', 'execStartTs': 5}),
        ('done', 30, {}),
    ])
    approval = next(s for s in result['spans'] if s['kind'] == 'approval_wait')
    assert (approval['tStart'], approval['tEnd'], approval['status']) == (3, 5, 'done')
    assert not approval.get('truncated')
    assert result['summary']['approvalWaitMs'] == 2
    assert result['summary']['toolMs'] == 26

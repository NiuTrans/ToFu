"""Backend traces include pre-event residence and retain monotonic clock provenance."""

import pytest

from lib.tasks_pkg import turn_trace as trace

pytestmark = pytest.mark.unit


def _rows(clock_id='clock'):
    origin = 1_000_000
    return [{'event_id': index, 'type': event_type, 'ts_ms': origin + offset,
             'payload': {'type': event_type, 'roundNum': 1,
                         'executionClockId': clock_id, 'executionOriginAtMs': origin,
                         'executionAtMs': origin + offset}}
            for index, (event_type, offset) in enumerate([
                ('round_start', 2000), ('round_end', 5000), ('done', 5100)])]


def test_terminal_trace_includes_acceptance_to_first_event_without_observer_delay(monkeypatch):
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: _rows())
    result = trace.fold_task_trace('clock-task', now_ms=2_000_000)
    assert result['clockSource'] == 'server-monotonic'
    assert result['tStart'] == 1_000_000
    assert result['tEnd'] == 1_005_100
    assert result['totalMs'] == 5100
    assert result['summary']['llmMs'] == 3000
    assert result['summary']['unattributedMs'] == 2100
    assert result['coverage'] == 'full'


def test_terminal_pending_event_uses_capture_instead_of_delayed_settlement_time(monkeypatch):
    rows = _rows()
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows[:-1])
    result = trace.fold_task_trace('clock-task', now_ms=2_000_000,
                                 pending_event=rows[-1]['payload'], pending_sequence=2)
    assert result['totalMs'] == 5100
    assert result['clockSource'] == 'server-monotonic'


def test_clock_domain_change_is_partial_evidence(monkeypatch):
    rows = _rows()
    rows[-1]['payload']['executionClockId'] = 'recovered-clock'
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    result = trace.fold_task_trace('clock-task', now_ms=2_000_000)
    assert result['clockSource'] == 'mixed'
    assert result['coverage'] == 'partial'
    assert result['coverageReason'] == 'clock-domain-change'


def test_legacy_evidence_does_not_claim_monotonic_provenance(monkeypatch):
    rows = _rows()
    for row in rows:
        row['payload'] = {'type': row['type'], 'roundNum': 1}
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: rows)
    result = trace.fold_task_trace('clock-task', now_ms=2_000_000)
    assert result['clockSource'] == 'legacy-wall'
    assert result['tStart'] == 1_002_000


def test_cold_running_trace_never_extends_monotonic_time_with_wall_clock(monkeypatch):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    monkeypatch.setattr(trace, '_read_trace_rows', lambda _: _rows()[:-1])
    monkeypatch.setattr(chat_task_runtime, 'get', lambda _: None)
    monkeypatch.setattr(trace.time, 'time', lambda: 9_000_000.0)
    result = trace.fold_task_trace('clock-task')
    assert result['running']
    assert result['totalMs'] == 5000
    assert result['coverage'] == 'partial'
    assert result['coverageReason'] == 'clock-unavailable'

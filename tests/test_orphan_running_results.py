"""Task-result orphan audit over the semantic storage projection."""

from __future__ import annotations

import time

import pytest


pytestmark = pytest.mark.unit


class _SummaryClient:
    def __init__(self, records=(), *, capped=False, error=None):
        self.records = list(records)
        self.capped = capped
        self.error = error
        self.queries = []

    def query(self, operation, payload, **kwargs):
        self.queries.append((operation, dict(payload), dict(kwargs)))
        if self.error is not None:
            raise self.error
        assert operation == 'task_results.summary_list'
        return {'records': list(self.records), 'capped': self.capped}


@pytest.fixture
def maintenance(monkeypatch):
    import lib.tasks_pkg.manager._maintenance as module

    monkeypatch.setattr(module.chat_task_runtime, 'task_ids', lambda: set())
    return module


def _record(task_id, conv_id, age_seconds):
    return {
        'key': task_id,
        'task_id': task_id,
        'conv_id': conv_id,
        'completed_at': int((time.time() - age_seconds) * 1000),
    }


def _install(monkeypatch, client):
    import lib.storage

    monkeypatch.setattr(
        lib.storage, 'get_storage_client',
        lambda write=False: client, raising=True)


def test_scan_uses_bounded_stale_summary_and_excludes_live_registry(
        monkeypatch, maintenance):
    client = _SummaryClient([
        _record('newer', 'conv-new', 4_000),
        _record('live', 'conv-live', 20_000),
        _record('oldest', 'conv-old', 90_000),
    ])
    _install(monkeypatch, client)
    monkeypatch.setattr(
        maintenance.chat_task_runtime, 'task_ids', lambda: {'live'})

    result = maintenance.find_orphan_running_results(limit=2)

    assert [item['task_id'] for item in result] == ['oldest', 'newer']
    operation, payload, options = client.queries[0]
    assert operation == 'task_results.summary_list'
    assert payload['status'] == 'running'
    assert payload['completed_before_ms'] > 0
    assert payload['scan_limit'] == 10_000
    assert options == {'deadline': 30}


def test_disabled_audit_does_not_touch_storage(monkeypatch, maintenance):
    client = _SummaryClient([_record('orphan', 'conv', 90_000)])
    _install(monkeypatch, client)
    monkeypatch.setenv('TOFU_ORPHAN_RESULT_MAX_AGE_SECS', '0')

    assert maintenance.find_orphan_running_results() == []
    assert client.queries == []


def test_storage_failure_is_explicit(monkeypatch, maintenance):
    _install(monkeypatch, _SummaryClient(error=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError, match='unavailable'):
        maintenance.find_orphan_running_results()


def test_reporter_counts_and_warns(monkeypatch, maintenance, caplog):
    _install(monkeypatch, _SummaryClient([
        _record('carrier-1', 'conv-a', 9_000),
        _record('carrier-2', 'conv-b', 8_000),
    ]))
    with caplog.at_level('WARNING'):
        assert maintenance.report_orphan_running_results() == 2
    assert 'orphaned at status=running' in caplog.text


def test_reporter_silent_when_clean(monkeypatch, maintenance, caplog):
    _install(monkeypatch, _SummaryClient())
    with caplog.at_level('WARNING'):
        assert maintenance.report_orphan_running_results() == 0
    assert 'orphaned at status=running' not in caplog.text


def test_orphan_report_has_independent_success_and_failure_cadence(monkeypatch):
    import lib.tasks_pkg.manager._maintenance as module

    monkeypatch.setattr(module, '_next_orphan_result_report_monotonic', 0.0)
    assert module._claim_orphan_result_report(100.0) is True
    assert module._claim_orphan_result_report(159.9) is False
    assert module._claim_orphan_result_report(160.0) is True
    module._finish_orphan_result_report(160.0)
    assert module._claim_orphan_result_report(1059.9) is False
    assert module._claim_orphan_result_report(1060.0) is True


def test_cleanup_throttles_audit_but_not_liveness_reaper(monkeypatch):
    import lib.tasks_pkg.manager._maintenance as module

    calls = {'reaper': 0, 'orphan': 0}
    monkeypatch.setattr(module, '_next_orphan_result_report_monotonic', 0.0)
    monkeypatch.setattr(
        module, 'reap_stuck_running_tasks',
        lambda: calls.__setitem__('reaper', calls['reaper'] + 1))
    monkeypatch.setattr(
        module, 'report_orphan_running_results',
        lambda: calls.__setitem__('orphan', calls['orphan'] + 1))

    module.cleanup_old_tasks()
    module.cleanup_old_tasks()
    assert calls == {'reaper': 2, 'orphan': 1}

"""Completion diagnostics must never change the terminal receiver contract."""
import json

import pytest

from lib import observability

pytestmark = pytest.mark.unit


@pytest.fixture
def timing_sinks(monkeypatch):
    records, samples = [], []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append((message, json.loads(payload))))
    monkeypatch.setattr(observability._STORE, 'observe',
                        lambda name, duration, buckets, **labels: samples.append((name, labels)))
    return records, samples


@pytest.mark.parametrize('result,outcome', [(True, 'success'), (False, 'deferred'),
                                           (None, 'success'), ({'roots': []}, 'success')])
def test_receiver_preserves_return_and_bounded_outcome(timing_sinks, result, outcome):
    records, samples = timing_sinks
    @observability.observe_task_completion('persistence')
    def receiver(task, *, flag):
        assert flag == 'owned'
        return result
    assert receiver({'id': 'task-a', 'convId': 'conv-a', 'content': 'secret'}, flag='owned') is result
    assert [row['boundary'] for _, row in records] == ['start', 'complete']
    assert records[-1][1]['outcome'] == outcome
    assert all(row['task_id'] == 'task-a' and 'secret' not in str(row) for _, row in records)
    assert samples == [('tofu_task_completion_stage_seconds',
                        {'stage': 'persistence', 'outcome': outcome})]


def test_nested_completion_identity_and_exception_restore(timing_sinks):
    records, samples = timing_sinks
    original = RuntimeError('original')
    with pytest.raises(RuntimeError) as raised:
        with observability.measure_task_completion('persistence', identity={'task_id': 'task-a'}):
            with observability.measure_task_completion('workspace_delivery'):
                raise original
    assert raised.value is original
    assert all(row['task_id'] == 'task-a' for _, row in records)
    assert [row['outcome'] for _, row in records if row['boundary'] == 'complete'] == ['error', 'error']
    assert observability._STARTUP_IDENTITY.get() is None
    assert len(samples) == 2


def test_completion_sinks_cannot_change_receiver(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(observability.logger, 'info', broken)
    monkeypatch.setattr(observability._STORE, 'observe', broken)
    @observability.observe_task_completion('persistence')
    def deferred(task):
        return False
    assert deferred({'id': 'task-a'}) is False


def test_completion_stages_do_not_leak_into_startup_family(timing_sinks):
    records, samples = timing_sinks
    with observability.measure_task_completion('unknown-task-id', identity={'task_id': 'task-a'}):
        pass
    assert samples == [('tofu_task_completion_stage_seconds', {'stage': 'other', 'outcome': 'success'})]
    assert all(message.startswith('[TaskCompletionTiming]') for message, _ in records)

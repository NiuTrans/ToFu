"""Startup diagnostics preserve failures and never admit identity labels."""
import pytest

from lib import observability

pytestmark = pytest.mark.unit


def test_startup_stage_metrics_are_closed_and_record_failure(monkeypatch):
    samples = []
    monkeypatch.setattr(
        observability._STORE, 'observe',
        lambda name, duration, buckets, **labels: samples.append((name, duration, labels)),
    )
    with observability.measure_task_startup('registration'):
        pass
    with pytest.raises(RuntimeError, match='original startup failure'):
        with observability.measure_task_startup('private-conversation-id'):
            raise RuntimeError('original startup failure')
    assert [labels for _, _, labels in samples] == [
        {'stage': 'registration', 'outcome': 'success'},
        {'stage': 'other', 'outcome': 'error'},
    ]
    assert all(duration >= 0 for _, duration, _ in samples)


def test_failed_diagnostic_observer_cannot_break_worker_entry(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('diagnostic observer unavailable')
    monkeypatch.setattr(observability._STORE, 'observe', unavailable)
    with observability.measure_task_startup('worker_entry'):
        pass
    with pytest.raises(ValueError, match='worker rejected'):
        with observability.measure_task_startup('worker_entry'):
            raise ValueError('worker rejected')

"""A diagnostic snapshot must retain its actual process and authority lifetime."""
import os
from types import SimpleNamespace

import pytest

from benchmarks.conversation_concurrency import capture_storage_metrics

pytestmark = pytest.mark.unit


def test_snapshot_retains_metrics_and_distinguishes_authority_incarnation():
    metrics = {'requestProfile': {'selected': 10}, 'maintenance': {'rounds': 3}}
    engine = SimpleNamespace(pid=os.getpid(), _client=SimpleNamespace(metrics=lambda **_: metrics))
    first, before = capture_storage_metrics(
        engine, incarnation=0, boundary='before-restart', quiescent=True)
    second, after = capture_storage_metrics(
        engine, incarnation=1, boundary='after-restart', quiescent=False)
    assert first is second is metrics
    assert before['process_id'] == after['process_id'] == os.getpid()
    assert before['process_started_unix_seconds'] == after['process_started_unix_seconds'] > 0
    assert before['authority_incarnation'] == 0 and after['authority_incarnation'] == 1
    assert before['benchmark_calls_quiescent'] is True
    assert after['benchmark_calls_quiescent'] is False
    first_start, first_finish = before['observed_monotonic_seconds']
    next_start, next_finish = after['observed_monotonic_seconds']
    assert first_start <= first_finish <= next_start <= next_finish


def test_metrics_rpc_crossing_process_replacement_is_refused():
    engine = SimpleNamespace(pid=os.getpid())

    def replaced(**_):
        engine.pid += 1
        return {}

    engine._client = SimpleNamespace(metrics=replaced)
    with pytest.raises(RuntimeError, match='process restart'):
        capture_storage_metrics(engine, incarnation=0, boundary='snapshot', quiescent=True)


def test_same_pid_with_a_new_birth_time_is_not_the_same_process(monkeypatch):
    import psutil

    times = iter([100.0, 101.0])
    monkeypatch.setattr(psutil, 'Process', lambda _: SimpleNamespace(create_time=lambda: next(times)))
    engine = SimpleNamespace(pid=17, _client=SimpleNamespace(metrics=lambda **_: {}))
    with pytest.raises(RuntimeError, match='process restart'):
        capture_storage_metrics(engine, incarnation=0, boundary='snapshot', quiescent=True)

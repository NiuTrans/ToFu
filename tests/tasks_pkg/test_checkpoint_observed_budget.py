"""The nonterminal task checkpoint budgets its own writes from the authority.

The native authority charges its FIFO queue wait against the caller's
deadline, so a checkpoint whose budget is a constant smaller than the
authority's observed service tail is refused with ``deadline_elapsed``
(reported as ``database_timeout``) and the refusal repeats inside one
congestion episode. These tests pin that the checkpoint asks for the budget
the authority has been observed to need, that a slow witness read cannot eat
the durable write's own budget, and that the budget stays bounded.
"""
from __future__ import annotations

import time

import pytest

from lib.storage.deadline_policy import (
    observe_storage_service,
    reset_storage_service_observations,
)
from lib.task_replay.task_result_checkpoint_contract import (
    TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
)
from lib.tasks_pkg.manager._persist import _upsert_task_row

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_observations():
    reset_storage_service_observations()
    yield
    reset_storage_service_observations()


def _clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(time, 'sleep', lambda amount: now.__setitem__(0, now[0] + amount))
    return now


def _observe(kind, operation, seconds, count=16):
    for _ in range(count):
        observe_storage_service(kind, operation, seconds)


def _client(monkeypatch, calls, now=None):
    class Client:
        def query(self, operation, payload, **options):
            calls.append((operation, options.get('deadline')))
            if now is not None:
                now[0] += options.get('deadline') or 0.0
            return None

        def command(self, operation, payload, command_id, **options):
            calls.append((operation, options.get('deadline')))
            return {'owned': True, 'version': 2,
                    'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: Client())


def _running_task():
    return {
        'id': 'checkpoint-task',
        'convId': '',
        '_userId': 7,
        '_taskResultVersion': 1,
        '_taskResultCheckpointGuard': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    }


def _write(task, status='running'):
    return _upsert_task_row(task, '', content='', thinking='', status=status,
                             error_json=None, tr_json=None, meta_json=None)


def test_the_checkpoint_budget_follows_the_observed_service_time(monkeypatch):
    calls = []
    _client(monkeypatch, calls)
    _observe('command', 'task_results.checkpoint', 0.5)
    assert _write(_running_task())
    assert calls == [('task_results.checkpoint', 2.0)]


def test_a_slow_witness_read_does_not_consume_the_writes_budget(monkeypatch):
    now = _clock(monkeypatch)
    calls = []
    _client(monkeypatch, calls, now)
    _observe('command', 'task_results.checkpoint', 0.5)
    task = _running_task()
    task.pop('_taskResultCheckpointGuard')
    task.pop('_taskResultVersion')
    assert _write(task)
    assert calls[0] == ('task_results.replay_get', 0.25)
    assert calls[1] == ('task_results.checkpoint', 2.0)


def test_the_checkpoint_budget_stays_bounded(monkeypatch):
    calls = []
    _client(monkeypatch, calls)
    _observe('command', 'task_results.checkpoint', 5.0)
    assert _write(_running_task())
    assert calls == [('task_results.checkpoint', 4.0)]


def test_the_terminal_checkpoint_keeps_the_client_default_budget(monkeypatch):
    calls = []
    _client(monkeypatch, calls)
    _observe('command', 'task_results.checkpoint', 0.5)
    assert _write(_running_task(), status='done')
    assert calls == [('task_results.checkpoint', None)]

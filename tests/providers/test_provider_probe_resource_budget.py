"""Demand probe calls, full matrices, cancellation and idle reuse."""

from __future__ import annotations

import pytest

from lib.provider_probe import build_probe_work
from lib.provider_probe_policy import (
    resolve_provider_probe_budget,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('workers', [1, 4, 8, 999999])
def test_probe_worker_policy_is_idle_reuse_without_active_count_quota(workers):
    budget = resolve_provider_probe_budget({
        'TOOL_MAX_PARALLEL_WORKERS': str(workers),
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': '60'})
    assert budget.task_workers == workers
    assert budget.cell_workers_per_task == workers
    assert budget.queue_capacity == budget.total_cell_workers == 0
    assert budget.idle_seconds == 60


def test_provider_probe_budget_honors_explicit_resident_worker_override():
    budget = resolve_provider_probe_budget({
        'TOOL_MAX_PARALLEL_WORKERS': '4',
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': '0'})
    assert budget.idle_seconds == 0


def test_matrix_construction_keeps_cells_beyond_retired_quota():
    wire_ids = [f'model-{index}' for index in range(5000)]
    work = build_probe_work(
        {'base_url': 'https://provider.invalid/v1', 'protocol': 'openai'},
        [{'model_id': 'logical', 'request_ids': wire_ids}], ['test-key'])
    assert len(work) == len(wire_ids)
    assert work[-1][3] == wire_ids[-1]


def test_explicit_connection_work_honors_cell_budget():
    work = build_probe_work(
        {'base_url': 'https://fallback.invalid', 'protocol': 'openai'},
        [{'model_id': 'root', 'request_ids': ['wire']}],
        ['key'] * 100,
        maximum_cells=3,
        request_contexts={'wire': {
            'base_url': 'https://actual.invalid', 'protocol': 'responses',
            'extra_headers': {}, 'adapter': {}}},
        credential_contexts=[{'oauth': '', 'extra_headers': {}}] * 100,
    )
    assert len(work) == 3
    assert [item[0] for item in work] == [0, 1, 2]
    assert all(item[5:7] == ('https://actual.invalid', 'responses') for item in work)


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_or_cancelled_probe_never_publishes_success(monkeypatch, cancelled):
    from lib import provider_probe as probe
    completed = []
    persisted = []
    task = dict(provider_id='unit-fault', status='running', started_at=0,
                finished_at=None, total=1, done_count=0, cells={},
                summary={'ok': 0, 'disable': 0}, error=None, attempts=1,
                _base_url='https://example.invalid', _extra_headers={},
                _protocol='openai', _abort=cancelled)
    def fail(*args, **kwargs):
        raise OSError('injected transport worker failure')
    monkeypatch.setattr(probe, 'probe_cell_multi', fail)
    monkeypatch.setattr(probe, 'persist_probe_task',
                        lambda value: persisted.append(probe.public_probe_snapshot(value)) or True)
    probe.run_cell_probe_task(task, [(0, 'key', 'root', 'wire', ['text'])], 1,
                              on_complete=lambda value: completed.append(value))
    assert task['status'] == 'error'
    assert task['done_count'] == 0
    assert task['error'] is not None
    assert task['finished_at'] is not None
    assert not completed
    assert persisted[-1]['status'] == 'error'


def test_cell_calls_start_beyond_idle_target_and_settle_without_live_threads(monkeypatch):
    import threading
    from dataclasses import replace
    from lib import provider_probe as probe
    count = 24
    barrier = threading.Barrier(count)
    started = []
    task = dict(provider_id='demand-burst', status='running', started_at=0,
                finished_at=None, total=count, done_count=0, cells={},
                summary={}, error=None, attempts=1, _abort=False,
                _base_url='https://example.invalid', _extra_headers={},
                _cell_workers=1, _protocol='openai')
    def cell(*args, **kwargs):
        started.append(threading.current_thread())
        barrier.wait(5)
        return 'ok', 'controlled probe'
    monkeypatch.setattr(probe, '_PROBE_BUDGET', replace(
        probe._PROBE_BUDGET, cell_workers_per_task=1))
    monkeypatch.setattr(probe, 'probe_cell_multi', cell)
    monkeypatch.setattr(probe, 'persist_probe_task', lambda value: True)
    work = [(0, 'test-key', f'model-{i}', f'wire-{i}', ['text']) for i in range(count)]
    probe.run_cell_probe_task(task, work, 5)
    assert task['status'] == 'done' and task['done_count'] == count
    assert len({thread.ident for thread in started}) == count
    assert all(not thread.is_alive() for thread in started)


def test_unsaved_public_results_are_not_evicted_by_new_terminal_results(monkeypatch):
    from collections import OrderedDict
    from lib import provider_probe as probe
    registry = OrderedDict()
    monkeypatch.setattr(probe, 'CELL_PROBE_TASKS', registry)
    for i in range(40):
        task = dict(provider_id=f'unsaved-{i}', status='done', started_at=0,
                    finished_at=1, total=1, done_count=1, cells={},
                    summary={}, error=None, _base_url='private', attempts=1)
        registry[task['provider_id']] = task
        probe._retire_registered_probe_task(task, persisted=False)
    assert len(registry) == 40
    assert all('_base_url' not in task for task in registry.values())

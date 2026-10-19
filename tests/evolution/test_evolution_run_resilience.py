"""Run-path resilience against transient storage authority stalls.

Fault-injection coverage for the 2026-09-17 incident: one
``deadline_elapsed`` inside the unprotected read path wedged runs in
``executing`` forever. These tests pin the fail-soft seams and the sweep
backstop so a transient stall can never again strand a run.
"""
from __future__ import annotations

import time

import pytest

from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

USER = 7


def _stall() -> StorageError:
    # The authority marks deadline_elapsed non-retryable (the flag gates
    # command replay); read paths must still treat it as transient.
    return StorageError('database_timeout', 'deadline_elapsed', retryable=False)


@pytest.fixture
def store(monkeypatch, _chat_sidecar_runtime):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def test_records_reads_retry_transient_stall_by_code(store, monkeypatch):
    from lib.evolution import records
    attempts = {'count': 0}
    real_query = store.query

    def flaky(operation, payload):
        attempts['count'] += 1
        if attempts['count'] == 1:
            raise _stall()
        return real_query(operation, payload)

    monkeypatch.setattr(store, 'query', flaky)
    assert records.get_record('evolution.runs.v1', f'{USER}:missing') is None
    assert attempts['count'] == 2


def test_records_reads_do_not_retry_durable_failures(store, monkeypatch):
    from lib.evolution import records
    attempts = {'count': 0}

    def conflict(operation, payload):
        attempts['count'] += 1
        raise StorageError('database_conflict', 'version conflict')

    monkeypatch.setattr(store, 'query', conflict)
    with pytest.raises(StorageError):
        records.get_record('evolution.runs.v1', f'{USER}:missing')
    assert attempts['count'] == 1


def test_inject_updates_is_fail_soft_on_storage_stall(store, monkeypatch):
    from lib.evolution import coordination, records

    def stalled(**kwargs):
        raise _stall()

    monkeypatch.setattr(records, '_storage', stalled)
    task = {'id': 'task-1', '_userId': USER,
            'config': {'evolution': {'run_id': 'run-1', 'stage': 'executing'}}}
    messages = [{'role': 'user', 'content': 'objective'}]
    # Must return quietly: coordination context is informational and a stall
    # may never kill the in-flight agent round.
    coordination.inject_updates(task, messages, 1)
    assert messages == [{'role': 'user', 'content': 'objective'}]


def test_terminal_listener_defers_run_advance_on_storage_stall(store, monkeypatch):
    from lib.evolution import automation, runs

    def stalled_advance(run_id, *, user_id, terminal_task=None):
        raise _stall()

    monkeypatch.setattr(runs, 'advance_run', stalled_advance)
    task = {'id': 'task-1', '_userId': USER, 'status': 'done',
            'config': {'evolution': {'run_id': 'run-1', 'stage': 'executing'}}}
    # The engine's terminal path must not see the stall; the sweep re-drives.
    automation.on_task_terminal(task)


def _seed_run(store, run_id, *, status, updated_at, task_id='task-1'):
    from lib.evolution import records
    run = dict(schema_version=1, run_id=run_id, objective='probe',
               status=status, goal_id=run_id, plan_id=run_id, episode_id='',
               task_id=task_id, attempt=1,
               budget={'max_tokens': 10, 'max_wall_clock_seconds': 10,
                       'max_tool_calls': 1},
               actuals={'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
               receipts=[], result='', review=None, error='', learning=None,
               created_at=updated_at, updated_at=updated_at)
    records.put_record(records.NAMESPACE_RUNS, f'{USER}:{run_id}', run,
                       kind='run')
    return run


def test_sweep_marks_orphaned_run_needs_attention(store, monkeypatch):
    from lib.evolution import runs
    monkeypatch.setattr(runs, 'default_task_loader',
                        lambda task_id, *, user_id: None)
    stale = int(time.time()) - 3600
    _seed_run(store, 'run-stale', status='executing', updated_at=stale)
    transitions = runs.sweep_stale_runs(user_id=USER)
    assert transitions == [
        {'run_id': 'run-stale', 'from': 'executing', 'to': 'needs_attention'}]
    after = runs.get_run('run-stale', user_id=USER)
    assert after['status'] == 'needs_attention'
    assert 'task_unavailable' in after['error']


def test_sweep_leaves_fresh_runs_alone(store, monkeypatch):
    from lib.evolution import runs
    monkeypatch.setattr(runs, 'default_task_loader',
                        lambda task_id, *, user_id: None)
    _seed_run(store, 'run-fresh', status='executing',
              updated_at=int(time.time()))
    assert runs.sweep_stale_runs(user_id=USER) == []
    assert runs.get_run('run-fresh', user_id=USER)['status'] == 'executing'


def test_sweep_leaves_live_tasks_alone(store, monkeypatch):
    from lib.evolution import runs
    live = {'id': 'task-1', '_userId': USER, 'status': 'running',
            'config': {'evolution': {'run_id': 'run-live', 'stage': 'executing'}}}
    monkeypatch.setattr(runs, 'default_task_loader',
                        lambda task_id, *, user_id: live)
    _seed_run(store, 'run-live', status='executing',
              updated_at=int(time.time()) - 3600)
    assert runs.sweep_stale_runs(user_id=USER) == []
    assert runs.get_run('run-live', user_id=USER)['status'] == 'executing'


def test_sweep_is_fail_soft_per_run(store, monkeypatch):
    from lib.evolution import runs
    stale = int(time.time()) - 3600
    _seed_run(store, 'run-a', status='executing', updated_at=stale)
    _seed_run(store, 'run-b', status='executing', updated_at=stale)
    real_advance = runs.advance_run

    def advance(run_id, *, user_id, terminal_task=None):
        if run_id == 'run-a':
            raise _stall()
        return real_advance(run_id, user_id=user_id,
                            terminal_task=terminal_task)

    monkeypatch.setattr(runs, 'advance_run', advance)
    monkeypatch.setattr(runs, 'default_task_loader',
                        lambda task_id, *, user_id: None)
    transitions = runs.sweep_stale_runs(user_id=USER)
    assert transitions == [
        {'run_id': 'run-b', 'from': 'executing', 'to': 'needs_attention'}]


def test_sweep_reaches_stale_runs_after_full_history_pages_and_isolates_owner(store, monkeypatch):
    from lib.evolution import runs, records
    for index in range(573):
        _seed_run(store, f'run-history-{index:04d}', status='completed', updated_at=1)
    for index in range(7):
        _seed_run(store, f'run-z-stale-{index}', status='executing', updated_at=1)
    _seed_run(store, 'run-z-fresh', status='executing', updated_at=10000)
    own = _seed_run(store, 'run-z-foreign', status='executing', updated_at=1)
    records.put_record(records.NAMESPACE_RUNS, f'{USER+1}:run-z-foreign', own, kind='run')
    store.rows.pop((records.NAMESPACE_RUNS, f'{USER}:run-z-foreign'))
    reads = []
    query = store.query
    def read(operation, payload):
        if operation == 'record.list': reads.append(dict(payload))
        return query(operation, payload)
    monkeypatch.setattr(store, 'query', read)
    seen = []
    def advance(run_id, *, user_id):
        assert user_id == USER
        seen.append(run_id)
        return {'status': 'needs_attention'}
    monkeypatch.setattr(runs, 'advance_run', advance)
    transitions = runs.sweep_stale_runs(user_id=USER, now=10000)
    assert sorted(seen) == [f'run-z-stale-{index}' for index in range(7)]
    assert [item['run_id'] for item in transitions] == sorted(seen)
    assert len(reads) == 4 and all(read['limit'] == records.MAX_LIST_LIMIT for read in reads)
    assert reads[0]['after_key'] == '' and all(read['prefix'] == f'{USER}:' for read in reads)


def test_short_storage_pages_do_not_end_sweep_early(store, monkeypatch):
    from lib.evolution import runs
    for index in range(9):
        _seed_run(store, f'run-{index}', status='executing', updated_at=1)
    query = store.query
    monkeypatch.setattr(store, 'query', lambda operation, payload:
                        query(operation, {**payload, 'limit': 2}) if operation == 'record.list'
                        else query(operation, payload))
    monkeypatch.setattr(runs, 'advance_run', lambda *args, **kwargs: {'status': 'needs_attention'})
    assert len(runs.sweep_stale_runs(user_id=USER, now=10000)) == 9


def test_sweep_stale_runs_start_independently_and_join_owned_workers(store, monkeypatch):
    import threading
    import time
    from lib.evolution import runs
    count = 64
    for index in range(count):
        _seed_run(store, f'run-{index:03d}', status='executing', updated_at=1)
    barrier = threading.Barrier(count)
    def advance(run_id, *, user_id):
        assert user_id == USER
        barrier.wait(timeout=10)
        return {'status': 'needs_attention'}
    monkeypatch.setattr(runs, 'advance_run', advance)
    started = time.perf_counter()
    transitions = runs.sweep_stale_runs(user_id=USER, now=10000)
    elapsed = time.perf_counter()-started
    assert len(transitions) == count
    assert not any(thread.name.startswith('tofu-evolution-run') for thread in threading.enumerate())
    print(f'run_sweep_rendezvous={count} seconds={elapsed:.4f} workers_after=0')


def test_cancelled_episode_lookup_failure_does_not_suppress_other_runs(store, monkeypatch):
    from lib.evolution import runs, records
    cancelled = _seed_run(store, 'run-a-cancelled', status='cancelled', updated_at=1)
    cancelled['episode_id'] = 'episode-unavailable'
    records.put_record(records.NAMESPACE_RUNS, f'{USER}:run-a-cancelled', cancelled, kind='run')
    _seed_run(store, 'run-z-stale', status='executing', updated_at=1)
    monkeypatch.setattr(runs, 'get_episode', lambda *args, **kwargs: (_ for _ in ()).throw(_stall()))
    monkeypatch.setattr(runs, 'advance_run', lambda *args, **kwargs: {'status': 'needs_attention'})
    transitions = runs.sweep_stale_runs(user_id=USER, now=10000)
    assert transitions == [{'run_id': 'run-z-stale', 'from': 'executing', 'to': 'needs_attention'}]


def test_failed_sweep_worker_start_does_not_enter_run_or_suppress_sibling(store, monkeypatch):
    import threading
    from lib.evolution import runs
    for label in ('a', 'b'):
        _seed_run(store, f'run-{label}', status='executing', updated_at=1)
    original = threading.Thread.start
    failed = []
    def start(thread):
        if thread.name.startswith('tofu-evolution-run') and not failed:
            failed.append(thread.name)
            raise RuntimeError('synthetic worker start failure')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    seen = []
    monkeypatch.setattr(runs, 'advance_run', lambda run_id, **kwargs:
                        seen.append(run_id) or {'status': 'needs_attention'})
    assert runs.sweep_stale_runs(user_id=USER, now=10000) == [
        {'run_id': 'run-b', 'from': 'executing', 'to': 'needs_attention'}]
    assert seen == ['run-b']
    assert runs.get_run('run-a', user_id=USER)['status'] == 'executing'
    assert not any(thread.name.startswith('tofu-evolution-run') for thread in threading.enumerate())


def test_failed_storage_page_joins_accepted_workers_before_propagating(store, monkeypatch):
    import threading
    from lib.evolution import runs
    for index in range(3):
        _seed_run(store, f'run-{index}', status='executing', updated_at=1)
    query = store.query
    seen = []
    def read(operation, payload):
        if operation == 'record.list':
            if payload['after_key']:
                raise StorageError('database_integrity', 'synthetic invalid later page')
            return query(operation, {**payload, 'limit': 2})
        return query(operation, payload)
    monkeypatch.setattr(store, 'query', read)
    monkeypatch.setattr(runs, 'advance_run', lambda run_id, **kwargs:
                        seen.append(run_id) or {'status': 'needs_attention'})
    with pytest.raises(StorageError, match='invalid later page'):
        runs.sweep_stale_runs(user_id=USER, now=10000)
    assert sorted(seen) == ['run-0', 'run-1']
    assert not any(thread.name.startswith('tofu-evolution-run') for thread in threading.enumerate())

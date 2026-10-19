"""Real SQL run-row fencing for atomic native Goal authority observations."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _runs
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient

pytestmark = pytest.mark.unit


def command(backend, operation, **payload):
    return TransactionClient(backend).command(operation, {'user_id': 7, 'tenant_id': '3', **payload}, str(uuid.uuid4()))


def create(backend, run='run'):
    assert command(backend, 'orchestration.run.create', run_id=run, orch_id='goal', definition={})['created']


def direct(backend, operation):
    return backend.command('test.goal_fence', '', None, 'user', operation, time.monotonic()+20, receipt_required=False)


@pytest.mark.parametrize('mutation', ['event', 'goal_event', 'delete'])
def test_run_observation_lock_fences_every_event_mutation(backend, mutation):
    create(backend)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    def observe(session):
        assert session.fetch_one_for_update('SELECT id FROM orchestration_runs WHERE id=? AND user_id=? AND tenant_id=?', ('run', 7, '3'))
        entered.set()
        assert release.wait(10)
        assert session.fetch_one('SELECT COUNT(*) AS n FROM orchestration_run_events WHERE run_id=? AND user_id=? AND tenant_id=?', ('run', 7, '3'))['n'] == 0
    def mutate():
        submitted.set()
        if mutation == 'goal_event':
            return direct(backend, lambda session: _runs._goal_event_append(session, run_id='run', user_id=7, tenant_id='3', event={'type': 'test'}))
        if mutation == 'event':
            return command(backend, 'orchestration.event.append', run_id='run', sequence=0, event={'type': 'test'})
        return command(backend, 'orchestration.run.delete', run_id='run')
    with ThreadPoolExecutor(max_workers=3) as executor:
        reading = executor.submit(direct, backend, observe)
        assert entered.wait(5)
        writing = executor.submit(mutate)
        try:
            assert submitted.wait(5)
            with pytest.raises(TimeoutError):
                writing.result(timeout=0.1)
            if backend.name == 'postgres':
                # A different run still progresses while this row is held.
                executor.submit(create, backend, 'independent').result(timeout=5)
        finally:
            release.set()
        reading.result(timeout=10)
        result = writing.result(timeout=10)
    assert result == (0 if mutation == 'goal_event' else
                      {'inserted': True, 'accepted': True} if mutation == 'event' else {'deleted': True})


def test_concurrent_goal_appends_allocate_distinct_contiguous_sequences(backend):
    create(backend)
    barrier = threading.Barrier(8)
    def append(number):
        barrier.wait(timeout=5)
        return direct(backend, lambda session: _runs._goal_event_append(session, run_id='run', user_id=7,
            tenant_id='3', event={'type': 'test', 'number': number}))
    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(8)))
    assert sorted(sequences) == list(range(8))
    events = TransactionClient(backend).query('orchestration.event.page', dict(run_id='run', user_id=7, tenant_id='3', cursor=0))
    assert len(events['events']) == 8
    assert sorted(event['number'] for event in events['events']) == list(range(8))


def test_run_update_rechecks_terminal_state_after_waiting_for_concurrent_commit(backend):
    create(backend)
    entered, release = threading.Event(), threading.Event()
    def settle(session):
        result = _runs._orchestration_run_update(session, dict(run_id='run', user_id=7, tenant_id='3', status='done', final='winner'))
        assert result['changed']
        entered.set()
        assert release.wait(10)
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(direct, backend, settle)
        assert entered.wait(5)
        loser = executor.submit(command, backend, 'orchestration.run.update_status', run_id='run', status='running', final='stale')
        try:
            with pytest.raises(TimeoutError):
                loser.result(timeout=0.1)
        finally:
            release.set()
        winner.result(timeout=10)
        assert loser.result(timeout=10) == {'changed': False}
    result = TransactionClient(backend).query('orchestration.run.get', dict(run_id='run', user_id=7, tenant_id='3'))
    assert (result['status'], result['final']) == ('done', 'winner')


def test_delete_fences_new_events_before_removing_the_parent(backend, monkeypatch):
    from lib.storage_authority_sql.adapters.postgres import PostgresSession
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    create(backend)
    kind = PostgresSession if backend.name == 'postgres' else SQLiteSession
    original = kind.execute
    entered, release = threading.Event(), threading.Event()
    def pause_after_event_deletion(session, sql, params=()):
        result = original(session, sql, params)
        if sql.startswith('DELETE FROM orchestration_run_events'):
            entered.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(kind, 'execute', pause_after_event_deletion)
    with ThreadPoolExecutor(max_workers=2) as executor:
        deleting = executor.submit(command, backend, 'orchestration.run.delete', run_id='run')
        assert entered.wait(5)
        appending = executor.submit(command, backend, 'orchestration.event.append', run_id='run', sequence=1, event={'type': 'late'})
        try:
            appending.result(timeout=0.1)
        except TimeoutError:
            pass
        finally:
            release.set()
        assert deleting.result(timeout=10) == {'deleted': True}
        assert backend.query('test.no_orphans', lambda session: session.fetch_one(
            'SELECT COUNT(*) AS n FROM orchestration_run_events WHERE run_id=? AND user_id=? AND tenant_id=?',
            ('run', 7, '3')), time.monotonic()+5)['n'] == 0
        with pytest.raises(StorageError, match='does not exist'):
            appending.result(timeout=10)

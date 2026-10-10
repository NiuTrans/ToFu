"""Backend time and lifecycle, rather than caller clocks, fence worker effects."""
import sqlite3
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.operations_pkg import _worker_jobs as jobs
from lib.storage_sidecar.schema import initialize_schema
from lib.storage_sidecar.worker_lease_clock import WorkerLeaseClock

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


class ClockSource:
    wall = 100_000
    elapsed = 0

    def clock(self):
        return WorkerLeaseClock(wall_ns=lambda: self.wall * 1_000_000,
                                monotonic_ns=lambda: self.elapsed * 1_000_000)


def test_authority_clock_advances_through_wall_clock_rollback():
    source = ClockSource()
    clock = source.clock()
    source.elapsed = 1000
    source.wall = 101_000
    assert clock.now_ms() == 101_000
    source.elapsed = 2000
    source.wall = 200_000
    assert clock.now_ms() == 200_000
    source.elapsed = 3000
    source.wall = 90_000
    assert clock.now_ms() == 201_000
    source.elapsed = 4000
    assert clock.now_ms() == 202_000


def test_paused_wall_probes_do_not_inflate_constructor_or_observation_offset():
    source = ClockSource()
    pause = {'milliseconds': 1000}

    def wall_probe():
        source.elapsed += pause['milliseconds']
        source.wall += pause['milliseconds']
        pause['milliseconds'] = 0
        return source.wall * 1_000_000

    clock = WorkerLeaseClock(wall_ns=wall_probe,
        monotonic_ns=lambda: source.elapsed * 1_000_000)
    assert clock.now_ms() == source.wall == 101_000
    pause['milliseconds'] = 5000
    assert clock.now_ms() == source.wall == 106_000
    assert clock.now_ms() == source.wall


@pytest.fixture(params=['sqlite', 'postgres'])
def session(request):
    epoch = uuid.uuid4().hex
    source = ClockSource()
    clock = source.clock()
    if request.param == 'sqlite':
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        session = SQLiteSession(connection, event_read_epoch=epoch, worker_lease_clock=clock)
    else:
        import psycopg
        from psycopg.rows import dict_row
        from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
        connection = psycopg.connect(request.getfixturevalue('postgres_cluster'), row_factory=dict_row)
        _PgPool._configure(connection)
        session = PostgresSession(connection, event_read_epoch=epoch, worker_lease_clock=clock)
    try:
        initialize_schema(session)
        yield session, source
    finally:
        connection.rollback()
        connection.close()


def enqueue(session, task='job', kind='owned-kind'):
    return jobs._worker_job_enqueue(session, dict(task_id=task, user_id=41, tenant_id='owned',
        task_kind=kind, idempotency_key=task, payload={}, now_ms=1))


def claim(session, *, observed=1, worker='holder', kind='owned-kind'):
    return jobs._worker_job_claim_next(session, dict(worker_id=worker, task_kinds=[kind],
        now_ms=observed, lease_ms=10000))


def test_caller_observations_neither_expire_nor_revive_a_lease(session):
    session, source = session
    enqueue(session)
    first = claim(session)
    assert first['leaseDeadlineMs'] == 110_000
    assert 'leaseAuthorityEpoch' not in first
    assert claim(session, observed=800_000, worker='rival') is None
    proof = dict(task_id='job', worker_id='holder', fencing_token=1, now_ms=1)
    pulse = jobs._worker_job_heartbeat(session, dict(proof, lease_ms=10000))
    assert pulse['job']['leaseDeadlineMs'] == 110_000
    source.elapsed = 10_000
    source.wall = 90_000
    assert jobs._worker_job_claim_state(session, proof)['ok'] is False
    assert jobs._worker_job_heartbeat(session, dict(proof, lease_ms=60000))['ok'] is False
    assert jobs._worker_job_complete(session, dict(proof, terminal_status='succeeded'))['ok'] is False
    assert jobs._worker_job_defer(session, dict(proof, available_at_ms=2000,
        replay_cursor=1, observed_cancel_sequence=0))['ok'] is False
    next_job = claim(session, worker='next')
    assert next_job['fencingToken'] == 2
    assert next_job['leaseDeadlineMs'] == 120_000


def test_utc_rollback_keeps_future_availability_separate_from_advancing_lease_time(session):
    session, source = session
    source.wall = 200_000
    source.elapsed = 1000
    assert session.worker_lease_clock.now_ms() == 200_000
    source.wall = 90_000
    source.elapsed = 2000
    jobs._worker_job_enqueue(session, dict(task_id='future', user_id=41, tenant_id='owned',
        task_kind='owned-kind', idempotency_key='future', payload={}, now_ms=1,
        available_at_ms=150_000))
    # The monotonic lease clock remains above 200000. Absolute UTC availability
    # must still wait for the actual UTC slot; a future caller cannot make it due.
    assert claim(session, observed=250_000) is None


def test_backend_restart_recovers_future_deadline_by_index(session):
    session, source = session
    enqueue(session)
    first = claim(session)
    if session.backend == 'postgres':
        # A source/previous-server epoch is private persisted evidence. Distinct
        # Sidecar read epochs never constitute a database restart.
        session.execute("UPDATE storage_worker_jobs SET lease_authority_epoch=?", ('f' * 32,))
    else:
        session.event_read_epoch = uuid.uuid4().hex
    source.wall = 90_000
    session.worker_lease_clock = source.clock()
    proof = dict(task_id='job', worker_id='holder', fencing_token=1, now_ms=1)
    assert jobs._worker_job_claim_state(session, proof)['ok'] is False
    assert jobs._worker_job_heartbeat(session, dict(proof, lease_ms=60000))['ok'] is False
    next_job = claim(session, worker='next')
    assert next_job['fencingToken'] == first['fencingToken'] + 1
    assert next_job['leaseDeadlineMs'] == 100_000


def test_legacy_empty_epoch_is_nonlive_and_supported_kinds_filter_before_limit(session):
    session, source = session
    enqueue(session, task='unsupported', kind='unsupported-kind')
    claim(session, kind='unsupported-kind')
    enqueue(session)
    claim(session)
    session.execute("UPDATE storage_worker_jobs SET lease_authority_epoch='', lease_deadline_ms=500000")
    assert jobs._worker_job_claim_state(session, dict(task_id='job', worker_id='holder',
        fencing_token=1, now_ms=1))['ok'] is False
    assert claim(session, worker='next')['taskId'] == 'job'
    assert claim(session, worker='rival') is None


def test_sessions_without_backend_clock_fail_closed(session):
    session, _ = session
    enqueue(session)
    session.worker_lease_clock = None
    with pytest.raises(StorageError) as raised:
        claim(session)
    assert raised.value.code == ('database_unavailable' if session.backend == 'postgres' else 'database_internal')


def test_distinct_postgres_replica_lifetimes_preserve_a_live_claim(session):
    session, source = session
    if session.backend != 'postgres':
        pytest.skip('PostgreSQL shared database replica contract')
    from lib.storage_sidecar.adapters.postgres import PostgresSession
    enqueue(session)
    first = claim(session)
    second_replica = PostgresSession(session.connection,
        event_read_epoch=uuid.uuid4().hex, worker_lease_clock=source.clock())
    assert claim(second_replica, worker='other-replica') is None
    assert jobs._worker_job_claim_state(second_replica, dict(task_id='job',
        worker_id='holder', fencing_token=first['fencingToken'], now_ms=1))['ok'] is True

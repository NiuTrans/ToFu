"""Real server processes share one lazy monotonic worker clock and lifecycle.

Every test loads a privately compiled library into its own PostgreSQL cluster.
No existing installation, PostgreSQL configuration, production clock or data is
modified. Clock rollback injection is reserved for a separate test-only wrapper.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


@pytest.fixture
def clock_server(postgres_cluster, tmp_path):
    from tests.support.postgres_worker_clock import compile_clock_library, install_clock_function
    library = compile_clock_library(tmp_path / 'tofu_worker_clock.so')
    install_clock_function(postgres_cluster, library, name='test_worker_clock')
    return postgres_cluster


def observe(dsn):
    import psycopg
    with psycopg.connect(dsn) as connection:
        return connection.execute('SELECT public.test_worker_clock()').fetchone()[0]


def test_parallel_initialization_and_last_client_close_preserve_server_identity(clock_server):
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(observe, [clock_server] * 8))
    assert len({value['epoch'] for value in values}) == 1
    assert all(value['format'] == 'tofu.worker-lease-clock/v1' for value in values)
    assert all(len(value['epoch']) == 32 for value in values)
    assert all(0 < value['stateBytes'] <= 128 for value in values)
    # Every observing connection is now closed; the registry owns the state.
    after = observe(clock_server)
    assert after['epoch'] == values[0]['epoch']
    assert after['nowMs'] >= max(value['nowMs'] for value in values)


def test_separate_server_sessions_observe_advancing_time(clock_server):
    import psycopg
    with psycopg.connect(clock_server) as first, psycopg.connect(clock_server) as second:
        before = first.execute('SELECT public.test_worker_clock()').fetchone()[0]
        time.sleep(0.02)
        after = second.execute('SELECT public.test_worker_clock()').fetchone()[0]
    assert after['epoch'] == before['epoch']
    assert after['nowMs'] >= before['nowMs'] + 15


@pytest.fixture
def injected_clock_server(postgres_cluster, tmp_path):
    import psycopg
    from psycopg import sql
    from tests.support.postgres_worker_clock import compile_clock_library, install_clock_function
    source = Path(__file__).resolve().parents[1] / 'support/postgres_worker_clock_injected.c'
    library = compile_clock_library(tmp_path / 'test_only_worker_clock.so', source=source)
    install_clock_function(postgres_cluster, library)
    with psycopg.connect(postgres_cluster) as connection:
        connection.execute(sql.SQL(
            'CREATE FUNCTION public.test_force_wall_delta(bigint) RETURNS bigint '
            'AS {}, {} LANGUAGE C VOLATILE PARALLEL RESTRICTED'
        ).format(sql.Literal(str(library)), sql.Literal('test_force_worker_clock_wall_delta')))
        connection.execute(sql.SQL(
            'CREATE FUNCTION public.test_force_probe_delay(bigint) RETURNS bigint '
            'AS {}, {} LANGUAGE C VOLATILE PARALLEL RESTRICTED'
        ).format(sql.Literal(str(library)), sql.Literal('test_force_worker_clock_probe_delay')))
    return postgres_cluster


def test_probe_scheduling_delays_do_not_push_shared_time_into_future(injected_clock_server):
    import psycopg
    from tests.support.worker_job_time import wall_ms
    with psycopg.connect(injected_clock_server) as connection:
        for delay in (80, 0, 80, 0):
            connection.execute('SELECT public.test_force_probe_delay(%s)', (delay,))
            before = wall_ms()
            value = connection.execute('SELECT public.tofu_worker_lease_clock_v1()').fetchone()[0]
            assert before <= value['nowMs'] <= wall_ms()+2


def test_shared_server_clock_prevents_rollback_reviving_expired_cross_replica_claim(injected_clock_server):
    import psycopg
    from psycopg.rows import dict_row
    from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
    from lib.storage_sidecar.operations_pkg import _worker_jobs as jobs
    from lib.storage_sidecar.schema import initialize_schema
    from tests.storage_sidecar.test_worker_lease_authority import enqueue, claim
    dsn = injected_clock_server
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        _PgPool._configure(connection)
        session = PostgresSession(connection)
        initialize_schema(session)
        enqueue(session)
        job = claim(session)
    proof = dict(task_id='job', worker_id='holder', fencing_token=job['fencingToken'], now_ms=1)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute('SELECT public.test_force_wall_delta(11000)')
        assert jobs._worker_job_claim_state(PostgresSession(connection), proof)['ok'] is False
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute('SELECT public.test_force_wall_delta(-11000)')
        session = PostgresSession(connection)
        assert jobs._worker_job_claim_state(session, proof)['ok'] is False
        assert jobs._worker_job_heartbeat(session, dict(proof, lease_ms=60000))['ok'] is False
        assert jobs._worker_job_complete(session, dict(proof, terminal_status='succeeded'))['ok'] is False
        replacement = claim(session, worker='replacement')
        assert replacement['fencingToken'] == job['fencingToken'] + 1


def test_server_restart_revokes_future_deadline_and_recovers_existing_queue(injected_clock_server):
    import psycopg
    from psycopg.rows import dict_row
    from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
    from lib.storage_sidecar.operations_pkg import _worker_jobs as jobs
    from lib.storage_sidecar.schema import initialize_schema
    from tests.storage_sidecar.test_worker_lease_authority import enqueue, claim
    dsn = injected_clock_server
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        _PgPool._configure(connection)
        session = PostgresSession(connection)
        initialize_schema(session)
        enqueue(session)
        job = claim(session)
        first_epoch = session.worker_lease_authority().epoch
    dsn.restart()
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        session = PostgresSession(connection)
        epoch = session.worker_lease_authority().epoch
        assert epoch != first_epoch
        assert jobs._worker_job_get(session, dict(task_id='job', user_id=41)) == job
        assert jobs._worker_job_claim_state(session, dict(task_id='job', worker_id='holder',
            fencing_token=job['fencingToken'], now_ms=1))['ok'] is False
        replacement = claim(session, worker='replacement')
        assert replacement['fencingToken'] == job['fencingToken'] + 1


def test_actual_server_utc_controls_queue_while_lease_clock_keeps_its_high_water(injected_clock_server):
    import psycopg
    from psycopg.rows import dict_row
    from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
    from lib.storage_sidecar.operations_pkg import _worker_jobs as jobs
    from lib.storage_sidecar.schema import initialize_schema
    from tests.storage_sidecar.test_worker_lease_authority import claim
    from tests.support.worker_job_time import wall_ms
    with psycopg.connect(injected_clock_server, row_factory=dict_row) as connection:
        _PgPool._configure(connection)
        session = PostgresSession(connection)
        initialize_schema(session)
        base = wall_ms()
        connection.execute('SELECT public.test_force_wall_delta(11000)')
        raised = session.worker_lease_authority()
        connection.execute('SELECT public.test_force_wall_delta(-11000)')
        jobs._worker_job_enqueue(session, dict(task_id='future', user_id=41, tenant_id='owned',
            task_kind='owned-kind', idempotency_key='future', payload={}, now_ms=1,
            available_at_ms=base+1000))
        assert claim(session, observed=base+20000) is None
        rolled_back = session.worker_lease_authority()
        assert rolled_back.lease_now_ms >= raised.lease_now_ms
        assert rolled_back.utc_now_ms < base+1000
        connection.execute('SELECT public.test_force_wall_delta(2000)')
        current = session.worker_lease_authority()
        assert current.utc_now_ms >= base+1000
        job = claim(session)
        assert job['taskId'] == 'future' and job['fencingToken'] == 1
        assert job['leaseDeadlineMs'] >= raised.lease_now_ms+10000


def test_database_identity_scopes_a_shared_server_epoch(postgres_cluster, tmp_path):
    import psycopg
    from psycopg.rows import dict_row
    from lib.storage_sidecar.adapters.postgres import PostgresSession
    from tests.support.postgres_worker_clock import install_worker_clock, install_clock_function
    library = install_worker_clock(postgres_cluster, tmp_path)
    with psycopg.connect(postgres_cluster, autocommit=True) as connection:
        connection.execute('CREATE DATABASE owned_clock_peer')
    peer = psycopg.conninfo.make_conninfo(postgres_cluster, dbname='owned_clock_peer')
    install_clock_function(peer, library)
    observations = []
    for dsn in (postgres_cluster, peer):
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            raw = connection.execute('SELECT public.tofu_worker_lease_clock_v1() AS clock').fetchone()['clock']
            scoped = PostgresSession(connection).worker_lease_authority().epoch
            observations.append((raw['epoch'], scoped))
    assert observations[0][0] == observations[1][0]
    assert observations[0][1] != observations[1][1]


@pytest.mark.parametrize('corruption', ['missing', 'format', 'epoch', 'boolean-time',
    'overflow-time', 'boolean-size', 'oversize-state', 'extra-field',
    'missing-utc', 'boolean-utc', 'zero-utc', 'negative-utc', 'utc-ahead-of-lease'])
def test_missing_or_invalid_clock_never_mutates_a_queued_claim(postgres_cluster, corruption):
    import json
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from lib.storage.errors import StorageError
    from lib.storage.generated_worker_job_contract import WORKER_LEASE_MAX_NOW_MS
    from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
    from lib.storage_sidecar.operations_pkg import _worker_jobs as jobs
    from lib.storage_sidecar.schema import initialize_schema
    from tests.storage_sidecar.test_worker_lease_authority import enqueue, claim
    with psycopg.connect(postgres_cluster, row_factory=dict_row) as connection:
        _PgPool._configure(connection)
        session = PostgresSession(connection)
        initialize_schema(session)
        queued = enqueue(session)['job']
        connection.commit()
        if corruption != 'missing':
            value = dict(format='tofu.worker-lease-clock/v1', epoch='a'*32, nowMs=100000, utcMs=100000, stateBytes=48)
            changes = {'format': {'format': 'unknown'}, 'epoch': {'epoch': 'A'*32},
                'boolean-time': {'nowMs': True}, 'overflow-time': {'nowMs': WORKER_LEASE_MAX_NOW_MS+1},
                'boolean-size': {'stateBytes': True}, 'oversize-state': {'stateBytes': 129},
                'extra-field': {'clockOverride': 1}, 'missing-utc': {}, 'boolean-utc': {'utcMs': True},
                'zero-utc': {'utcMs': 0}, 'negative-utc': {'utcMs': -1},
                'utc-ahead-of-lease': {'utcMs': 100001}}
            value.update(changes[corruption])
            if corruption == 'missing-utc':value.pop('utcMs')
            connection.execute(sql.SQL('CREATE FUNCTION public.tofu_worker_lease_clock_v1() '
                'RETURNS jsonb LANGUAGE SQL AS {}').format(sql.Literal('SELECT '+
                    sql.Literal(json.dumps(value)).as_string(connection)+'::jsonb')))
            connection.commit()
        with pytest.raises(StorageError) as raised:
            claim(session)
        assert raised.value.code == ('database_unavailable' if corruption == 'missing' else 'database_internal')
        connection.rollback()
        assert jobs._worker_job_get(session, dict(task_id='job', user_id=41)) == queued

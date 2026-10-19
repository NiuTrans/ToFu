"""External PostgreSQL startup and one-shot migration authority contracts."""

from __future__ import annotations

from pathlib import Path
import time

import pytest


pytest.importorskip('psycopg')

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters import postgres
from lib.storage_authority_sql.config import SidecarConfig
from lib.storage_authority_sql import migrate
from lib.storage_authority_sql.schema import (
    SCHEMA_VERSION,
    TASK_EVENT_RETENTION_INDEX_NAMES,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('value', ['v2', '', 'true', 'null', '123', '{"n":1}',
                                  '\u4e2d\u6587', True, None, 12, 1.5, {'n': 1}, [1, 'null']])
def test_pooled_json_decodes_once_without_changing_scalar_types(value):
    import json
    from psycopg.adapt import AdaptersMap
    from psycopg.pq import Format
    from lib.storage_authority_sql.operations_pkg._common import _load

    connection = _Connection(None)
    connection.adapters = AdaptersMap(postgres.psycopg.adapters)
    postgres._PgPool._configure(connection)
    encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
    for oid in (114, 3802):
        for format in (Format.TEXT, Format.BINARY):
            loader = connection.adapters.get_loader(oid, format)(oid)
            wire = b'\x01' + encoded if oid == 3802 and format == Format.BINARY else encoded
            result = _load(loader.load(memoryview(wire)))
            assert result == value and type(result) is type(value)
    # Connection configuration must not alter unrelated psycopg clients.
    default = postgres.psycopg.adapters.get_loader(3802, Format.TEXT)(3802)
    assert default.load(b'"true"') == 'true'


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, params=()):
        self.connection.events.append(('execute', statement, params))

    def executemany(self, statement, params):
        rows = tuple(params)
        self.connection.events.append(('executemany', statement, rows))
        self.rowcount = len(rows)

    def fetchone(self):
        return self.connection.fetchone_result

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, fetchone_result) -> None:
        self.fetchone_result = fetchone_result
        self.events = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.events.append('commit')

    def rollback(self):
        self.events.append('rollback')

    def close(self):
        self.events.append('close')


class _Pool:
    def __init__(self, dsn, size, config, *, name) -> None:
        assert dsn == config.postgres_dsn
        self.size = size
        self.name = name
        self.closed = False

    def acquire(self, deadline_at):
        del deadline_at
        return _Connection(None)

    def release(self, connection, *, broken=False):
        del connection, broken

    def metrics(self):
        return {'pool_available': self.size, 'pool_size': self.size}

    def begin_close(self):
        self.closed = True

    def close(self):
        self.closed = True


class _TransactionPool:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.releases = []

    def acquire(self, _deadline_at):
        return self.connection

    def release(self, connection, *, broken):
        self.releases.append((connection, broken))


def _config(tmp_path: Path, *, allow_schema_migration: bool) -> SidecarConfig:
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'
    data_dir.mkdir()
    logs_dir.mkdir()
    return SidecarConfig(
        project_root=tmp_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        backend='postgres',
        deployment_mode='distributed',
        process_role='api',
        replica_id='api-0',
        token='test-token-' * 4,
        sqlite_path=data_dir / 'tofu.db',
        postgres_dsn='postgresql://external.invalid/tofu',
        redis_url='rediss://external.invalid/0',
        allow_schema_migration=allow_schema_migration,
        read_pool_size=2,
        write_pool_size=1,
    )


def _settings():
    return {
        'fsync': 'on',
        'synchronous_commit': 'on',
        'full_page_writes': 'on',
        'data_checksums': 'off',
        'max_connections': 100,
        'in_recovery': False,
    }


@pytest.mark.parametrize('server_limit', [4, 100])
def test_application_startup_validates_schema_without_ddl_or_local_pool_quota(
        tmp_path, monkeypatch, server_limit):
    settings = _settings()
    settings['max_connections'] = server_limit
    connection = _Connection(settings)
    validated = []
    monkeypatch.setattr(postgres.psycopg, 'connect', lambda *a, **k: connection)
    monkeypatch.setattr(postgres, '_PgPool', _Pool)
    monkeypatch.setattr(
        postgres, 'validate_schema_version',
        lambda session: validated.append(session) or SCHEMA_VERSION,
    )
    monkeypatch.setattr(
        postgres, 'initialize_schema',
        lambda session: pytest.fail('application startup attempted schema DDL'),
    )

    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    try:
        health = backend.start()
        assert backend._read_pool.size == backend.config.read_pool_size
        assert backend._write_pool.size == backend.config.write_pool_size
        assert health['preflight']['read_pool_capacity'] == 0
        assert health['preflight']['write_pool_capacity'] == 0
        assert health['preflight']['server_max_connections'] == server_limit
    finally:
        backend.close()

    assert health['ready'] is True
    assert len(validated) == 1
    assert 'commit' not in connection.events
    assert connection.events[-2:] == ['rollback', 'close']
    assert not (tmp_path / 'data' / 'pgdata').exists()


def test_private_contract_backend_may_initialize_a_disposable_schema(
        tmp_path, monkeypatch):
    connection = _Connection(_settings())
    initialized = []
    monkeypatch.setattr(postgres.psycopg, 'connect', lambda *a, **k: connection)
    monkeypatch.setattr(postgres, '_PgPool', _Pool)
    monkeypatch.setattr(
        postgres, 'initialize_schema',
        lambda session: initialized.append(session),
    )
    monkeypatch.setattr(
        postgres, 'validate_schema_version',
        lambda session: pytest.fail('test migration path unexpectedly validated only'),
    )

    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=True))
    try:
        backend.start()
    finally:
        backend.close()

    assert len(initialized) == 1
    assert 'commit' in connection.events


def test_external_postgres_backup_is_never_run_by_an_application_pod(tmp_path):
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))

    with pytest.raises(StorageError, match='platform-managed') as raised:
        backend.backup(time.monotonic() + 1)

    assert raised.value.code == 'database_protocol_error'


def test_transaction_timeouts_use_parameterizable_set_config(tmp_path):
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    result = backend._transaction(
        pool,
        lambda _session: 'ok',
        time.monotonic() + 1,
        readonly=True,
        retries=0,
    )

    executed = [
        event for event in connection.events
        if isinstance(event, tuple) and event[0] == 'execute'
    ]
    statements = [event[1] for event in executed]
    assert result == 'ok'
    assert statements[0] == 'SET TRANSACTION READ ONLY'
    assert statements[1:] == [
        "SELECT set_config('statement_timeout', %s, true)",
        "SELECT set_config('lock_timeout', %s, true)",
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
    ]
    assert all(
        isinstance(event[2][0], str) for event in executed[1:])
    assert all('SET LOCAL' not in statement for statement in statements)
    assert pool.releases == [(connection, False)]


def test_operation_transaction_timeout_override_is_bounded_and_explicit(tmp_path):
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    assert backend._transaction(
        pool,
        lambda _session: 'ok',
        time.monotonic() + 40,
        readonly=False,
        retries=0,
        transaction_timeout_s=30.0,
    ) == 'ok'

    set_config = [
        event for event in connection.events
        if isinstance(event, tuple)
        and event[0] == 'execute'
        and "statement_timeout" in event[1]
    ]
    assert 29_000 <= int(set_config[0][2][0]) <= 30_000
    with pytest.raises(StorageError) as raised:
        backend._transaction(
            pool,
            lambda _session: pytest.fail('invalid budget reached operation'),
            time.monotonic() + 40,
            readonly=False,
            retries=0,
            transaction_timeout_s=301.0,
        )
    assert raised.value.code == 'database_protocol_error'


def test_postgres_session_batches_exact_mutations_with_translated_parameters():
    connection = _Connection(None)
    session = postgres.PostgresSession(connection)

    affected = session.execute_many_exact(
        "UPDATE records SET value=? WHERE id=? AND note LIKE '50%'",
        (("a", "one"), ("b", "two")),
    )

    assert affected == 2
    assert connection.events == [(
        'executemany',
        "UPDATE records SET value=%s WHERE id=%s AND note LIKE '50%%'",
        (("a", "one"), ("b", "two")),
    )]


def test_transaction_failure_rolls_back_and_releases_pool_slot(tmp_path):
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    with pytest.raises(StorageError) as raised:
        backend._transaction(
            pool,
            lambda _session: (_ for _ in ()).throw(ValueError('semantic failure')),
            time.monotonic() + 1,
            readonly=False,
            retries=0,
        )

    assert raised.value.code == 'database_internal'
    assert 'rollback' in connection.events
    assert pool.releases == [(connection, False)]


def test_postgres_command_uses_compact_receipt_and_replays_before_mutation(
    tmp_path, monkeypatch,
):
    from lib.storage_authority_sql.receipt_codec import (
        COMMAND_RECEIPT_LOOKUP_SQL,
        command_receipt_identity_v2,
        encode_receipt_response,
    )

    class ReceiptSession:
        def __init__(self):
            self.lookup_rows = []
            self.lookups = []
            self.inserts = []

        def fetch_one(self, sql, params=()):
            assert 'pg_advisory_xact_lock' in sql
            assert params == ('postgres-receipt-command',)
            return {'locked': None}

        def fetch_all(self, sql, params=()):
            self.lookups.append((sql, params))
            return list(self.lookup_rows)

        def execute(self, sql, params=()):
            self.inserts.append((sql, params))
            return 1

    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False)
    )
    backend._write_pool = object()
    session = ReceiptSession()
    monkeypatch.setattr(
        backend, '_transaction',
        lambda _pool, operation, _deadline, **_kwargs: operation(session),
    )
    operation = 'record.put'
    digest = 'ab' * 32
    response = {'ok': True, 'version': 1}

    first = backend.command(
        operation, digest, 'postgres-receipt-command', 'user',
        lambda _session: response, time.monotonic() + 1,
        receipt_required=True,
    )

    command_key, digest_bytes = command_receipt_identity_v2(
        'postgres-receipt-command', operation, digest
    )
    assert first == response
    assert session.lookups == [(
        COMMAND_RECEIPT_LOOKUP_SQL,
        (
            operation, digest, 'postgres-receipt-command',
            operation, digest_bytes, command_key,
        ),
    )]
    assert len(session.inserts) == 1
    insert_sql, insert_params = session.inserts[0]
    assert 'INSERT INTO storage_command_receipts_v2' in insert_sql
    assert insert_params[:3] == (command_key, operation, digest_bytes)
    assert insert_params[3] == encode_receipt_response(response)

    session.lookup_rows = [{
        'receipt_format': 'v2',
        'request_matches': 1,
        'response_json': encode_receipt_response(response),
    }]
    replay = backend.command(
        operation, digest, 'postgres-receipt-command', 'user',
        lambda _session: pytest.fail('receipt replay repeated the mutation'),
        time.monotonic() + 1,
        receipt_required=True,
    )
    assert replay == response
    assert len(session.inserts) == 1


class _CommitFailConnection(_Connection):
    """Loses the connection during COMMIT (lost ack) for the first N tries."""

    def __init__(self, failures: int) -> None:
        super().__init__(None)
        self.commit_failures = failures
        self.commits = 0

    def commit(self):
        self.commits += 1
        if self.commits <= self.commit_failures:
            raise postgres.psycopg.OperationalError(
                'connection lost during commit')
        self.events.append('commit')


def test_commit_phase_failure_is_not_reexecuted_without_a_receipt(tmp_path):
    """A lost commit ack on a receipt-free command surfaces ambiguously.

    The transaction may have landed; re-executing the operation could repeat
    non-idempotent effects. The adapter must surface the classified error
    after exactly one execution instead of retrying like an execute-phase
    failure.
    """
    connection = _CommitFailConnection(failures=3)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)
    executions = []

    with pytest.raises(StorageError) as raised:
        backend._transaction(
            pool,
            lambda _session: executions.append(1) or 'ok',
            time.monotonic() + 5,
            readonly=False,
            retries=3,
            replay_safe=False,
        )

    assert raised.value.code == 'database_unavailable'
    assert raised.value.retryable is True
    assert executions == [1]
    assert connection.commits == 1
    assert backend._metrics['retries'] == 0
    assert backend._metrics['failures'] == 1


def test_commit_phase_failure_replays_only_receipt_backed_commands(tmp_path):
    """Receipt-backed commands re-execute safely after a lost commit ack.

    The advisory lock plus receipt probe deduplicate the replay: if the first
    commit landed, the probe returns the recorded response; otherwise the
    mutation runs exactly once on the retry.
    """
    connection = _CommitFailConnection(failures=2)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)
    executions = []

    result = backend._transaction(
        pool,
        lambda _session: executions.append(1) or 'ok',
        time.monotonic() + 5,
        readonly=False,
        retries=3,
        replay_safe=True,
    )

    assert result == 'ok'
    assert len(executions) == 3
    assert backend._metrics['retries'] == 2
    assert backend._metrics['failures'] == 0


def test_execute_phase_failure_keeps_retrying_without_a_receipt(tmp_path):
    """Begin/execute-phase retry behavior is unchanged for all commands."""
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)
    attempts = []

    def flaky(_session):
        attempts.append(1)
        if len(attempts) < 2:
            raise postgres.psycopg.OperationalError(
                'connection lost mid-execute')
        return 'ok'

    assert backend._transaction(
        pool, flaky, time.monotonic() + 5,
        readonly=False, retries=3, replay_safe=False,
    ) == 'ok'
    assert len(attempts) == 2
    assert backend._metrics['retries'] == 1


def test_command_marks_only_receipt_backed_work_replay_safe(tmp_path, monkeypatch):
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    backend._write_pool = object()
    captured = []
    monkeypatch.setattr(
        backend,
        '_transaction',
        lambda *args, **kwargs: captured.append(kwargs) or 'ok',
    )

    assert backend.command(
        'maintenance.prune', 'ab' * 32, None, 'maintenance',
        lambda _session: {'ok': True}, time.monotonic() + 1,
        receipt_required=False,
    ) == 'ok'
    assert captured[-1]['replay_safe'] is False

    assert backend.command(
        'record.put', 'ab' * 32, 'command-1', 'user',
        lambda _session: {'ok': True}, time.monotonic() + 1,
        receipt_required=True,
    ) == 'ok'
    assert captured[-1]['replay_safe'] is True


def test_idle_in_transaction_timeout_maps_to_retryable_timeout():
    """SQLSTATE 25P03 is a server-enforced budget expiry, not an internal fault."""
    error = postgres.psycopg.OperationalError(
        'terminating connection due to idle-in-transaction timeout')
    error.sqlstate = '25P03'
    mapped = postgres._map_postgres_error(error)
    assert mapped.code == 'database_timeout'
    assert mapped.retryable is True
    assert mapped.retry_after_ms == 25


def test_idle_in_transaction_timeout_follows_the_declared_budget(tmp_path):
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    assert backend._transaction(
        pool,
        lambda _session: 'ok',
        time.monotonic() + 40,
        readonly=False,
        retries=0,
        transaction_timeout_s=30.0,
    ) == 'ok'

    idle_configs = [
        event for event in connection.events
        if isinstance(event, tuple)
        and event[0] == 'execute'
        and 'idle_in_transaction_session_timeout' in event[1]
    ]
    assert len(idle_configs) == 1
    assert 29_000 <= int(idle_configs[0][2][0]) <= 30_000


def test_idle_in_transaction_timeout_defaults_to_the_config_budget(tmp_path):
    connection = _Connection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    assert backend._transaction(
        pool, lambda _session: 'ok', time.monotonic() + 40,
        readonly=False, retries=0,
    ) == 'ok'

    configured = {}
    for event in connection.events:
        if (isinstance(event, tuple) and event[0] == 'execute'
                and 'set_config' in event[1]):
            configured[event[1].split("'")[1]] = event[2][0]
    assert configured['idle_in_transaction_session_timeout'] == (
        configured['statement_timeout'])


def test_rollback_failure_discards_the_connection_and_warns(tmp_path, caplog):
    class _RollbackFailConnection(_Connection):
        def rollback(self):
            raise postgres.psycopg.OperationalError('rollback failed')

    connection = _RollbackFailConnection(None)
    backend = postgres.PostgresBackend(
        _config(tmp_path, allow_schema_migration=False))
    pool = _TransactionPool(connection)

    with caplog.at_level('WARNING', logger='tofu.storage.sidecar.postgres'):
        with pytest.raises(StorageError) as raised:
            backend._transaction(
                pool,
                lambda _session: (_ for _ in ()).throw(
                    ValueError('semantic failure')),
                time.monotonic() + 1,
                readonly=False,
                retries=0,
            )

    assert raised.value.code == 'database_internal'
    assert pool.releases == [(connection, True)]
    assert any(
        record.levelname == 'WARNING' and 'rollback failed' in record.message.lower()
        for record in caplog.records
    )


def _driver_pool(tmp_path, monkeypatch, connection_factory):
    config = _config(tmp_path, allow_schema_migration=False)
    monkeypatch.setattr(postgres.psycopg, 'connect', lambda *args, **kwargs: connection_factory())
    monkeypatch.setattr(postgres._PgPool, '_configure', staticmethod(lambda connection: None))
    return postgres._PgPool(config.postgres_dsn, 1, config, name='test-demand-pool')


def test_pool_release_failure_closes_the_connection_instead_of_leaking(tmp_path, monkeypatch):
    class Connection(_Connection):
        def rollback(self):
            raise RuntimeError('reset failed')
    connection = Connection(None)
    pool = _driver_pool(tmp_path, monkeypatch, lambda: connection)
    try:
        borrowed = pool.acquire(time.monotonic() + 5)
        with pytest.raises(RuntimeError, match='reset failed'):
            pool.release(borrowed)
        assert connection.events == ['close']
        assert pool.metrics()['active_leases'] == 0
        assert pool.metrics()['pool_available'] == 0
    finally:
        pool.close()


def test_pool_release_reuses_healthy_connections_without_closing(tmp_path, monkeypatch):
    connection = _Connection(None)
    pool = _driver_pool(tmp_path, monkeypatch, lambda: connection)
    try:
        borrowed = pool.acquire(time.monotonic() + 5)
        pool.release(borrowed)
        assert connection.events == ['rollback']
        assert pool.acquire(time.monotonic() + 5) is connection
        pool.release(connection)
    finally:
        pool.close()
    assert connection.events[-1] == 'close'


def test_pool_release_retires_broken_connections(tmp_path, monkeypatch):
    connection = _Connection(None)
    pool = _driver_pool(tmp_path, monkeypatch, lambda: connection)
    try:
        borrowed = pool.acquire(time.monotonic() + 5)
        pool.release(borrowed, broken=True)
        assert connection.events == ['close']
        assert pool.metrics()['active_leases'] == 0
        assert pool.metrics()['pool_available'] == 0
    finally:
        pool.close()


def test_one_shot_migration_takes_lock_commits_then_validates(monkeypatch):
    connection = _Connection({'locked': None})
    monkeypatch.setattr(migrate.psycopg, 'connect', lambda *a, **k: connection)
    monkeypatch.setattr(
        migrate, 'initialize_schema',
        lambda session: connection.events.append('initialize'),
    )
    monkeypatch.setattr(
        migrate, 'validate_schema_version',
        lambda session: connection.events.append('validate') or SCHEMA_VERSION,
    )

    version = migrate.migrate_external_postgres(
        'postgresql://external.invalid/tofu')

    executed = [event for event in connection.events
                if isinstance(event, tuple) and event[0] == 'execute']
    statements = [event[1] for event in executed]
    assert sum('pg_advisory_xact_lock' in sql for sql in statements) == 1
    assert all(any(name in sql for sql in statements)
               for name in TASK_EVENT_RETENTION_INDEX_NAMES)
    assert any(
        'DROP INDEX IF EXISTS idx_storage_events_retention' in sql
        for sql in statements)
    assert connection.events.index('initialize') < connection.events.index('commit')
    assert connection.events.index('commit') < connection.events.index('validate')
    assert connection.events[-2:] == ['rollback', 'close']
    assert version == SCHEMA_VERSION


def test_postgres_connections_start_on_demand_above_idle_hint(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    connections = []
    lock = threading.Lock()
    create_barrier = threading.Barrier(32, timeout=10)
    borrowed_barrier = threading.Barrier(32, timeout=10)
    def connect():
        create_barrier.wait()
        connection = _Connection(None)
        with lock:
            connections.append(connection)
        return connection
    pool = _driver_pool(tmp_path, monkeypatch, connect)
    def borrow(index):
        connection = pool.acquire(time.monotonic() + 15)
        try:
            borrowed_barrier.wait()
            return index
        finally:
            pool.release(connection)
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=32) as executor:
            assert list(executor.map(borrow, range(32))) == list(range(32))
        assert pool.metrics()['peak_leases'] == 32
        assert pool.metrics()['connections_num'] == 32
        assert pool.metrics()['pool_available'] == 1
        assert pool.metrics()['active_leases'] == 0
        assert pool.metrics()['pool_max'] == 0
        print(f'32 controlled Psycopg connections / idle hint 1: '
              f'{(time.monotonic()-started)*1000:.2f} ms')
    finally:
        pool.close()
    assert all(connection.events[-1] == 'close' for connection in connections)


@pytest.mark.parametrize('delay_connect', [False, True])
def test_postgres_close_joins_borrowers_and_connection_preparation(tmp_path, monkeypatch, delay_connect):
    import threading
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    connection = _Connection(None)
    errors = []
    def connect():
        if delay_connect:
            entered.set()
            assert release.wait(5)
        return connection
    pool = _driver_pool(tmp_path, monkeypatch, connect)
    def borrow():
        try:
            item = pool.acquire(time.monotonic() + 10)
            if not delay_connect:
                entered.set()
                assert release.wait(5)
            pool.release(item)
        except BaseException as error:
            errors.append(error)
    def close():
        pool.close()
        closed.set()
    borrower, closer = threading.Thread(target=borrow), threading.Thread(target=close)
    try:
        borrower.start()
        assert entered.wait(2)
        closer.start()
        with pool.pool.condition:
            assert pool.pool.condition.wait_for(lambda: pool.pool.closed, timeout=2)
        assert not closed.is_set()
        with pytest.raises(StorageError, match='not available'):
            pool.acquire(time.monotonic() + 1)
        release.set()
        borrower.join(3); closer.join(3)
        assert not borrower.is_alive() and not closer.is_alive()
        assert not errors and closed.is_set() and connection.events[-1] == 'close'
    finally:
        release.set()
        borrower.join(5)
        if closer.ident is not None:
            closer.join(5)
        pool.close()


def test_actual_driver_connection_failure_releases_preparation_and_keeps_error(tmp_path, monkeypatch):
    error = postgres.psycopg.OperationalError('controlled connection failure')
    def connect():
        raise error
    pool = _driver_pool(tmp_path, monkeypatch, connect)
    try:
        with pytest.raises(StorageError) as caught:
            pool.acquire(time.monotonic() + 5)
        assert caught.value.code == 'database_unavailable'
        assert pool.metrics()['active_leases'] == 0
        assert pool.metrics()['pool_available'] == 0
    finally:
        pool.close()


def test_post_commit_retirement_error_cannot_invalidate_or_repeat_committed_result(tmp_path):
    class Pool(_TransactionPool):
        def release(self, connection, *, broken):
            super().release(connection, broken=broken)
            raise StorageError('database_unavailable', 'controlled retirement failure')
    connection = _Connection(None)
    pool = Pool(connection)
    backend = postgres.PostgresBackend(_config(tmp_path, allow_schema_migration=False))
    calls = []
    result = backend._transaction(pool,
        lambda session: calls.append('mutation') or {'committed': True},
        time.monotonic() + 5, readonly=False, retries=0)
    assert result == {'committed': True} and calls == ['mutation']
    assert connection.events.count('commit') == 1 and len(pool.releases) == 1

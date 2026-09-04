"""External PostgreSQL startup and one-shot migration authority contracts."""

from __future__ import annotations

from pathlib import Path
import time

import pytest


pytest.importorskip('psycopg')

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters import postgres
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar import migrate
from lib.storage_sidecar.schema import (
    SCHEMA_VERSION,
    TASK_EVENT_RETENTION_INDEX_NAMES,
)


pytestmark = pytest.mark.unit


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


def test_application_startup_validates_schema_without_ddl_or_probe_write(
        tmp_path, monkeypatch):
    connection = _Connection(_settings())
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
    from lib.storage_sidecar.receipt_codec import (
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

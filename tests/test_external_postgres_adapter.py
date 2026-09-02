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

"""Application startup and migration schema-authority contracts."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import (
    SCHEMA_VERSION,
    initialize_schema,
    validate_schema_version,
)


pytestmark = pytest.mark.unit


class _SchemaProbe:
    backend = 'postgres'

    def __init__(self, row=None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error

    def fetch_one(self, sql, params=()):
        assert 'storage_meta' in sql
        assert params == ('schema_version',)
        if self.error is not None:
            raise self.error
        return self.row


def test_application_schema_probe_accepts_only_the_exact_version():
    assert validate_schema_version(
        _SchemaProbe({'meta_value': str(SCHEMA_VERSION)})) == SCHEMA_VERSION

    for row in (None, {'meta_value': 'invalid'},
                {'meta_value': str(SCHEMA_VERSION - 1)},
                {'meta_value': str(SCHEMA_VERSION + 1)}):
        with pytest.raises(StorageError, match='migration job') as raised:
            validate_schema_version(_SchemaProbe(row))
        assert raised.value.code == 'database_integrity'


def test_application_schema_probe_classifies_a_missing_catalog():
    with pytest.raises(StorageError, match='migration job') as raised:
        validate_schema_version(_SchemaProbe(error=RuntimeError('missing table')))

    assert raised.value.code == 'database_integrity'


def test_schema_39_adds_empty_bounded_compaction_receipts(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v39.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '39'))
    connection.execute('''
        CREATE TABLE storage_compaction_archives (
            archive_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
    ''')
    connection.execute(
        'INSERT INTO storage_compaction_archives VALUES (?,?,?,?)',
        ('legacy', 'conversation', 1, 1),
    )

    initialize_schema(SQLiteSession(connection))

    row = connection.execute(
        'SELECT receipt_json FROM storage_compaction_archives '
        'WHERE archive_id=?', ('legacy',),
    ).fetchone()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    column_types = {
        item['name']: item['type']
        for item in connection.execute(
            'PRAGMA table_info("storage_compaction_archives")')
    }
    connection.close()

    assert int(version) == SCHEMA_VERSION == 40
    assert row['receipt_json'] == '{}'
    assert column_types['receipt_json'] == 'TEXT'


def test_jsondoc_migration_spelling_is_backend_neutral():
    from lib.storage_sidecar import schema

    sqlite_probe = type('SQLiteProbe', (), {'backend': 'sqlite'})()
    postgres_probe = type('PostgresProbe', (), {'backend': 'postgres'})()
    statement = 'ALTER TABLE example ADD COLUMN receipt JSONDOC NOT NULL'

    assert ' receipt TEXT ' in schema._sql_for_backend(sqlite_probe, statement)
    assert ' receipt JSONB ' in schema._sql_for_backend(postgres_probe, statement)


def test_private_postgres_contract_test_uses_a_secret_file(
        tmp_path: Path, monkeypatch):
    dsn_file = tmp_path / 'postgres-dsn'
    dsn_file.write_text(
        'postgresql://test:test@127.0.0.1/test', encoding='utf-8')
    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 't' * 48)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.setenv('TOFU_STORAGE_TEST_BACKEND', 'postgres')
    monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn_file))

    config = SidecarConfig.from_environment()

    assert config.backend == 'postgres'
    assert config.postgres_dsn.startswith('postgresql://')
    assert config.allow_schema_migration is True
    assert 'test:test' not in repr(config)
    assert not hasattr(config, 'pgdata')


def test_private_postgres_contract_test_requires_a_secret_file(
        tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 't' * 48)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.setenv('TOFU_STORAGE_TEST_BACKEND', 'postgres')
    monkeypatch.delenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', raising=False)

    with pytest.raises(RuntimeError, match='TEST_POSTGRES_DSN_FILE'):
        SidecarConfig.from_environment()

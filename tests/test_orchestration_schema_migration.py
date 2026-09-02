"""Schema 28+ assigns legacy orchestration history to one explicit owner."""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema


pytestmark = pytest.mark.unit


def test_schema_27_migrates_runs_and_events_without_importing_ownerless_files(
    tmp_path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v27.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '27'))
    connection.execute('''
        CREATE TABLE orchestration_runs (
            id TEXT PRIMARY KEY, orch_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '', definition TEXT NOT NULL DEFAULT '{}',
            input TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            final TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0, finished_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    connection.execute('''
        CREATE TABLE orchestration_run_events (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT '', node_id TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id, seq)
        )
    ''')
    connection.execute(
        'INSERT INTO orchestration_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            'legacy-run', 'legacy-flow', 'Legacy', '{"nodes":[]}', 'start',
            'done', 'finished', '', 'personal-key', 10, 20, 20,
        ),
    )
    connection.execute(
        'INSERT INTO orchestration_run_events VALUES (?,?,?,?,?,?)',
        ('legacy-run', 0, 'done', 'stop', '{"type":"done"}', 20),
    )

    initialize_schema(SQLiteSession(connection))

    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    run = connection.execute(
        'SELECT user_id, tenant_id, status FROM orchestration_runs WHERE id=?',
        ('legacy-run',),
    ).fetchone()
    event = connection.execute(
        'SELECT user_id, tenant_id, seq FROM orchestration_run_events '
        'WHERE run_id=?',
        ('legacy-run',),
    ).fetchone()
    definition_count = connection.execute(
        'SELECT COUNT(*) FROM orchestration_definitions').fetchone()[0]
    indexes = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    connection.close()

    assert int(version) == SCHEMA_VERSION == 40
    assert tuple(run) == (1, '', 'done')
    assert tuple(event) == (1, '', 0)
    assert definition_count == 0
    assert {
        'idx_orch_runs_status',
        'idx_orch_runs_orch',
        'idx_orch_definitions_owner_updated',
    } <= indexes

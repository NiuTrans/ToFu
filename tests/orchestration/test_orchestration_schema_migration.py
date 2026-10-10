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

    connection.execute(
        'INSERT INTO orchestration_run_events VALUES (?,?,?,?,?,?)',
        ('orphan-run', 0, 'done', 'stop', '{"type":"done"}', 20),
    )
    # A pre-v61 archive has an explicit user but no child scope. Migration
    # must preserve it without inventing a child owner or a parent record.
    connection.execute('''
        CREATE TABLE storage_compaction_archives (
            archive_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            user_id BIGINT NOT NULL, messages_json TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT 'force',
            task_id TEXT NOT NULL DEFAULT '', round_num BIGINT NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '', tokens_before BIGINT NOT NULL DEFAULT 0,
            tokens_after BIGINT NOT NULL DEFAULT 0,
            msgs_before BIGINT NOT NULL DEFAULT 0, msgs_after BIGINT NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '', payload_size BIGINT NOT NULL DEFAULT 0,
            created_at_ms BIGINT NOT NULL
        )
    ''')
    connection.execute(
        'INSERT INTO storage_compaction_archives '
        '(archive_id, conversation_id, user_id, messages_json, created_at_ms) '
        'VALUES (?, ?, ?, ?, ?)',
        ('legacy-archive', 'missing-conversation', 17, '[]', 20),
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
    orphan_event = connection.execute(
        'SELECT user_id, tenant_id, seq FROM orchestration_run_events '
        'WHERE run_id=?', ('orphan-run',),
    ).fetchone()
    orphan_run = connection.execute(
        'SELECT id FROM orchestration_runs WHERE id=?', ('orphan-run',),
    ).fetchone()
    archive = connection.execute(
        'SELECT user_id, conversation_id, archive_scope, messages_json, '
        'post_messages_json, receipt_json FROM storage_compaction_archives '
        'WHERE archive_id=?', ('legacy-archive',),
    ).fetchone()
    archive_columns = {
        row['name']: row for row in connection.execute(
            'PRAGMA table_info(storage_compaction_archives)')
    }
    scope_index_columns = [
        row['name'] for row in connection.execute(
            'PRAGMA index_info(idx_storage_compaction_archives_scope)')
    ]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert archive_columns['user_id']['notnull'] == 1
    assert archive_columns['user_id']['dflt_value'] is None
    assert archive_columns['archive_scope']['notnull'] == 1
    assert archive_columns['archive_scope']['dflt_value'] == "''"
    assert scope_index_columns == [
        'user_id', 'conversation_id', 'archive_scope', 'created_at_ms', 'archive_id',
    ]
    assert tuple(archive) == (17, 'missing-conversation', '', '[]', None, '{}')
    assert tuple(orphan_event) == (1, '', 0)
    assert orphan_run is None
    assert tuple(run) == (1, '', 'done')
    assert tuple(event) == (1, '', 0)
    assert definition_count == 0
    assert {
        'idx_orch_runs_status',
        'idx_orch_runs_orch',
        'idx_orch_definitions_owner_updated',
    } <= indexes

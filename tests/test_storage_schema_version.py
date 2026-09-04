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

    assert int(version) == SCHEMA_VERSION
    assert row['receipt_json'] == '{}'
    assert column_types['receipt_json'] == 'TEXT'


def test_schema_41_adds_scoped_media_metadata_to_knowledge(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v41.db')
    connection.row_factory = sqlite3.Row

    initialize_schema(SQLiteSession(connection))

    document_columns = {
        item['name']: item
        for item in connection.execute(
            'PRAGMA table_info("storage_knowledge_documents")')
    }
    asset_columns = {
        item['name']: item
        for item in connection.execute(
            'PRAGMA table_info("storage_knowledge_assets")')
    }
    connection.close()

    assert document_columns['scope']['notnull'] == 1
    assert document_columns['scope']['dflt_value'] == "'library'"
    assert document_columns['media_metadata_json']['notnull'] == 1
    assert document_columns['media_metadata_json']['dflt_value'] == "'{}'"
    assert asset_columns['metadata_json']['notnull'] == 1
    assert asset_columns['metadata_json']['dflt_value'] == "'{}'"


def test_schema_45_adds_bounded_attempt_dispatch_recovery_index(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v45.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute(
        'DROP INDEX idx_storage_generation_attempts_dispatchable')
    connection.execute(
        'ALTER TABLE storage_generation_attempts DROP COLUMN dispatch_mode')
    connection.execute(
        "UPDATE storage_meta SET meta_value='44' "
        "WHERE meta_key='schema_version'"
    )

    initialize_schema(session)

    columns = {
        row['name']: row
        for row in connection.execute(
            'PRAGMA table_info("storage_generation_attempts")')
    }
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_storage_generation_attempts_dispatchable'"
    ).fetchone()['sql']
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert columns['dispatch_mode']['notnull'] == 1
    assert columns['dispatch_mode']['dflt_value'] == "''"
    assert "status = 'pending'" in index_sql
    assert "task_id = ''" in index_sql
    assert "dispatch_mode = 'conversation_executor'" in index_sql


def test_schema_46_adds_bounded_attempt_timing_authority(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v46.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute('DROP INDEX idx_storage_generation_attempts_task')
    connection.execute(
        'ALTER TABLE storage_generation_attempts DROP COLUMN timing_trace_json')
    connection.execute(
        "UPDATE storage_meta SET meta_value='45' "
        "WHERE meta_key='schema_version'"
    )

    initialize_schema(session)

    columns = {
        row['name']: row
        for row in connection.execute(
            'PRAGMA table_info("storage_generation_attempts")')
    }
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    task_index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_storage_generation_attempts_task'"
    ).fetchone()['sql']
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert columns['timing_trace_json']['notnull'] == 1
    assert columns['timing_trace_json']['dflt_value'] == "'{}'"
    assert "task_id <> ''" in task_index_sql


def test_schema_47_indexes_owner_conversation_trace_discovery(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v47.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute(
        'DROP INDEX idx_storage_generation_attempts_conversation_created')
    connection.execute(
        "UPDATE storage_meta SET meta_value='46' "
        "WHERE meta_key='schema_version'")

    initialize_schema(session)

    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_storage_generation_attempts_conversation_created'"
    ).fetchone()['sql']
    plan = connection.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT a.attempt_id,a.task_id,a.status,a.turn_id,a.created_at,"
        "a.settled_at FROM storage_generation_attempts a "
        "JOIN storage_conversation_turns t ON t.turn_id=a.turn_id "
        "AND t.conversation_id=a.conversation_id "
        "WHERE a.conversation_id=? AND t.user_id=? AND a.task_id<>'' "
        "AND a.created_at<? "
        "ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT ?",
        ('conversation', 1, 2**63 - 1, 31),
    ).fetchall()
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert 'conversation_id, created_at DESC, attempt_id DESC' in index_sql
    assert "WHERE task_id <> ''" in index_sql
    assert any(
        'idx_storage_generation_attempts_conversation_created' in row['detail']
        for row in plan
    )


def test_schema_48_and_49_add_projection_head_metadata_without_blob_backfill(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v48.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    historical_projection = b'{"content":"historical projection"}'
    connection.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,ordinal,actor,projection_json,"
        "projection_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ('legacy-turn', 'legacy-conversation', 1, 0, 'assistant',
         historical_projection, 7, 1, 1),
    )
    connection.execute('DROP TABLE storage_turn_projection_checkpoints')
    for column_name in (
        'projection_patch_bytes',
        'projection_patch_count',
        'projection_materialized_revision',
        'projection_checkpoint_revision',
    ):
        connection.execute(
            f'ALTER TABLE storage_conversation_turns DROP COLUMN {column_name}')
    connection.execute(
        "UPDATE storage_meta SET meta_value='47' "
        "WHERE meta_key='schema_version'")
    connection.commit()

    traced_statements: list[str] = []
    changes_before = connection.total_changes
    connection.set_trace_callback(traced_statements.append)
    initialize_schema(session)
    connection.set_trace_callback(None)

    columns = {
        row['name']: row
        for row in connection.execute(
            'PRAGMA table_info("storage_conversation_turns")')
    }
    row = connection.execute(
        'SELECT projection_json,projection_checkpoint_revision,'
        'projection_materialized_revision,'
        'projection_patch_count,projection_patch_bytes '
        'FROM storage_conversation_turns WHERE turn_id=?',
        ('legacy-turn',),
    ).fetchone()
    plan = connection.execute(
        'EXPLAIN QUERY PLAN SELECT projection_revision,payload_json '
        'FROM storage_attempt_events WHERE attempt_id=? '
        'AND projection_revision>? AND projection_revision<=? '
        'ORDER BY sequence LIMIT ?',
        ('attempt', 7, 9, 257),
    ).fetchall()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    checkpoint_count = connection.execute(
        'SELECT COUNT(*) FROM storage_turn_projection_checkpoints'
    ).fetchone()[0]
    migration_dml = [
        statement.upper()
        for statement in traced_statements
        if statement.lstrip().upper().startswith(('SELECT ', 'UPDATE '))
    ]
    migration_change_count = connection.total_changes - changes_before
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert row['projection_json'] == historical_projection
    assert row['projection_checkpoint_revision'] is None
    assert row['projection_materialized_revision'] is None
    assert row['projection_patch_count'] == 0
    assert row['projection_patch_bytes'] == 0
    assert columns['projection_materialized_revision']['notnull'] == 0
    assert columns['projection_checkpoint_revision']['notnull'] == 0
    assert columns['projection_patch_count']['dflt_value'] == '0'
    assert columns['projection_patch_bytes']['dflt_value'] == '0'
    assert any(
        (
            'idx_storage_attempt_events_projection_chain' in item['detail']
            or 'sqlite_autoindex_storage_attempt_events_1' in item['detail']
        )
        and 'attempt_id=?' in item['detail']
        for item in plan
    )
    assert migration_change_count == 1
    assert checkpoint_count == 0
    assert all('PROJECTION_JSON' not in statement for statement in migration_dml)


def test_schema_49_upgrades_an_established_schema_48_authority(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v49-from-v48.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    historical_projection = b'{"content":"established v48 projection"}'
    connection.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,ordinal,actor,projection_json,"
        "projection_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ('v48-turn', 'v48-conversation', 1, 0, 'assistant',
         historical_projection, 11, 1, 1),
    )
    connection.execute('DROP TABLE storage_turn_projection_checkpoints')
    connection.execute(
        'ALTER TABLE storage_conversation_turns '
        'DROP COLUMN projection_checkpoint_revision')
    connection.execute(
        "UPDATE storage_meta SET meta_value='48' "
        "WHERE meta_key='schema_version'")
    connection.commit()

    traced_statements: list[str] = []
    changes_before = connection.total_changes
    connection.set_trace_callback(traced_statements.append)
    initialize_schema(session)
    connection.set_trace_callback(None)

    row = connection.execute(
        'SELECT projection_json,projection_checkpoint_revision '
        'FROM storage_conversation_turns WHERE turn_id=?',
        ('v48-turn',),
    ).fetchone()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    checkpoint_count = connection.execute(
        'SELECT COUNT(*) FROM storage_turn_projection_checkpoints'
    ).fetchone()[0]
    migration_dml = [
        statement.upper()
        for statement in traced_statements
        if statement.lstrip().upper().startswith(('SELECT ', 'UPDATE '))
    ]
    migration_change_count = connection.total_changes - changes_before
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert row['projection_json'] == historical_projection
    assert row['projection_checkpoint_revision'] is None
    assert checkpoint_count == 0
    assert migration_change_count == 1
    assert all('PROJECTION_JSON' not in statement for statement in migration_dml)


def test_schema_50_repairs_the_checkpoint_unchanged_revision_cohort(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v50-checkpoint-repair.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    checkpoint_projection = b'{"content":"still current"}'
    connection.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,ordinal,actor,status,"
        "current_attempt_id,projection_json,projection_revision,"
        "projection_checkpoint_revision,projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            'repair-turn', 'repair-conversation', 7, 0, 'assistant', 'running',
            'repair-attempt', b'{}', 8, 7, None, 0, 0, 1, 2,
        ),
    )
    connection.execute(
        "INSERT INTO storage_turn_projection_checkpoints("
        "turn_id,conversation_id,user_id,attempt_id,projection_revision,"
        "projection_json,projection_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            'repair-turn', 'repair-conversation', 7, 'repair-attempt', 7,
            checkpoint_projection, len(checkpoint_projection), 1,
        ),
    )
    connection.execute(
        "UPDATE storage_meta SET meta_value='49' "
        "WHERE meta_key='schema_version'")
    connection.commit()

    initialize_schema(session)

    turn = connection.execute(
        "SELECT projection_revision,projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count,"
        "projection_patch_bytes FROM storage_conversation_turns "
        "WHERE turn_id='repair-turn'"
    ).fetchone()
    checkpoint = connection.execute(
        "SELECT projection_revision,projection_json,projection_bytes "
        "FROM storage_turn_projection_checkpoints WHERE turn_id='repair-turn'"
    ).fetchone()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert turn['projection_revision'] == 8
    assert turn['projection_checkpoint_revision'] == 8
    assert turn['projection_materialized_revision'] is None
    assert turn['projection_patch_count'] == 0
    assert turn['projection_patch_bytes'] == 0
    assert checkpoint['projection_revision'] == 8
    assert checkpoint['projection_json'] == checkpoint_projection
    assert checkpoint['projection_bytes'] == len(checkpoint_projection)


def test_schema_51_adds_attempt_event_references_without_json_backfill(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v51-from-v50.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    historical_event = b'{"contract":"legacy-inline-change"}'
    connection.execute(
        'INSERT INTO storage_conversation_changes('
        'conversation_id,user_id,sync_sequence,change_type,turn_id,'
        'attempt_id,event_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
        ('conversation', 1, 1, 'attempt.event', 'turn', 'attempt',
         historical_event, 1),
    )
    connection.execute(
        'DROP INDEX idx_storage_conversation_changes_attempt_event_reference')
    connection.execute(
        'ALTER TABLE storage_conversation_changes DROP COLUMN attempt_sequence')
    connection.execute(
        "UPDATE storage_meta SET meta_value='50' "
        "WHERE meta_key='schema_version'")
    connection.commit()

    traced_statements: list[str] = []
    changes_before = connection.total_changes
    connection.set_trace_callback(traced_statements.append)
    initialize_schema(session)
    connection.set_trace_callback(None)

    columns = {
        row['name']: row
        for row in connection.execute(
            'PRAGMA table_info("storage_conversation_changes")')
    }
    stored = connection.execute(
        'SELECT event_json,attempt_sequence '
        'FROM storage_conversation_changes WHERE conversation_id=?',
        ('conversation',),
    ).fetchone()
    plan = connection.execute(
        'EXPLAIN QUERY PLAN SELECT 1 FROM storage_conversation_changes '
        'WHERE attempt_id=? AND attempt_sequence IS NOT NULL',
        ('attempt',),
    ).fetchall()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    migration_dml = [
        statement.upper()
        for statement in traced_statements
        if statement.lstrip().upper().startswith(('SELECT ', 'UPDATE '))
    ]
    migration_change_count = connection.total_changes - changes_before
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert stored['event_json'] == historical_event
    assert stored['attempt_sequence'] is None
    assert columns['attempt_sequence']['notnull'] == 0
    assert any(
        'idx_storage_conversation_changes_attempt_event_reference'
        in row['detail']
        for row in plan
    )
    assert migration_change_count == 1
    assert all('EVENT_JSON' not in statement for statement in migration_dml)


def test_schema_52_adds_empty_compact_receipts_without_legacy_backfill(
    tmp_path: Path,
):
    connection = sqlite3.connect(tmp_path / 'schema-v52-from-v51.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    legacy_response = b'{"ok":true,"contract":"legacy-receipt"}'
    connection.execute(
        'INSERT INTO storage_command_receipts('
        'command_id,operation,request_digest,response_json,committed_at_ms) '
        'VALUES (?,?,?,?,?)',
        ('legacy-command', 'record.put', 'ab' * 32, legacy_response, 1),
    )
    connection.execute('DROP TABLE storage_command_receipts_v2')
    connection.execute(
        "UPDATE storage_meta SET meta_value='51' "
        "WHERE meta_key='schema_version'"
    )
    connection.commit()

    traced_statements: list[str] = []
    changes_before = connection.total_changes
    connection.set_trace_callback(traced_statements.append)
    initialize_schema(session)
    connection.set_trace_callback(None)

    legacy = connection.execute(
        'SELECT operation,request_digest,response_json,committed_at_ms '
        'FROM storage_command_receipts WHERE command_id=?',
        ('legacy-command',),
    ).fetchone()
    compact_count = connection.execute(
        'SELECT COUNT(*) FROM storage_command_receipts_v2'
    ).fetchone()[0]
    compact_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        ('storage_command_receipts_v2',),
    ).fetchone()[0]
    compact_columns = {
        row['name']: row['type']
        for row in connection.execute(
            'PRAGMA table_info("storage_command_receipts_v2")'
        )
    }
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    migration_change_count = connection.total_changes - changes_before
    receipt_dml = [
        statement for statement in traced_statements
        if statement.lstrip().upper().startswith(
            ('INSERT ', 'UPDATE ', 'DELETE ')
        ) and 'STORAGE_COMMAND_RECEIPTS' in statement.upper()
    ]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert dict(legacy) == {
        'operation': 'record.put',
        'request_digest': 'ab' * 32,
        'response_json': legacy_response,
        'committed_at_ms': 1,
    }
    assert compact_count == 0
    assert 'WITHOUT ROWID' in compact_sql.upper()
    assert compact_columns['command_key'] == 'BLOB'
    assert compact_columns['request_digest'] == 'BLOB'
    assert migration_change_count == 1
    assert receipt_dml == []


def test_schema_53_adds_empty_owner_scoped_desktop_egress_preferences(
        tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v53-from-v52.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute('DROP TABLE storage_desktop_egress_preferences')
    connection.execute(
        "UPDATE storage_meta SET meta_value='52' "
        "WHERE meta_key='schema_version'")
    connection.commit()

    initialize_schema(session)

    columns = {
        row['name']: row
        for row in connection.execute(
            'PRAGMA table_info("storage_desktop_egress_preferences")')
    }
    count = connection.execute(
        'SELECT COUNT(*) FROM storage_desktop_egress_preferences').fetchone()[0]
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert count == 0
    assert columns['owner_user_id']['pk'] == 1
    assert columns['agent_id']['notnull'] == 1
    assert columns['updated_at_ms']['notnull'] == 1


def test_schema_56_adds_project_event_columns_before_their_unique_index(
        tmp_path: Path):
    connection = sqlite3.connect(tmp_path / 'schema-v56-from-v55.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '55'))
    connection.execute('''
        CREATE TABLE storage_events (
            task_id TEXT NOT NULL,
            sequence BIGINT NOT NULL,
            stream_kind TEXT NOT NULL DEFAULT 'task',
            event_type TEXT NOT NULL DEFAULT '',
            event_kind TEXT NOT NULL DEFAULT '',
            event_json BLOB NOT NULL,
            created_at_ms BIGINT NOT NULL,
            PRIMARY KEY (task_id, sequence)
        )
    ''')
    connection.execute(
        'INSERT INTO storage_events('
        'task_id, sequence, event_json, created_at_ms) VALUES (?, ?, ?, ?)',
        ('legacy-task', 1, b'{}', 1),
    )

    initialize_schema(SQLiteSession(connection))

    columns = {
        row['name']: row
        for row in connection.execute('PRAGMA table_info("storage_events")')
    }
    event = connection.execute(
        'SELECT owner_user_id, project_key, project_sequence, event_json '
        'FROM storage_events WHERE task_id=? AND sequence=?',
        ('legacy-task', 1),
    ).fetchone()
    project_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ('idx_storage_events_project_sequence',),
    ).fetchone()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.close()

    assert int(version) == SCHEMA_VERSION
    assert {'owner_user_id', 'project_key', 'project_sequence'} <= columns.keys()
    assert dict(event) == {
        'owner_user_id': 0,
        'project_key': '',
        'project_sequence': 0,
        'event_json': b'{}',
    }
    assert 'WHERE project_sequence > 0' in project_index['sql']


def test_jsondoc_migration_spelling_is_backend_neutral():
    from lib.storage_sidecar import schema

    sqlite_probe = type('SQLiteProbe', (), {'backend': 'sqlite'})()
    postgres_probe = type('PostgresProbe', (), {'backend': 'postgres'})()
    statement = 'ALTER TABLE example ADD COLUMN receipt JSONDOC NOT NULL'

    assert ' receipt TEXT ' in schema._sql_for_backend(sqlite_probe, statement)
    assert ' receipt JSONB ' in schema._sql_for_backend(postgres_probe, statement)

    compact = (
        'CREATE TABLE example(command_key BLOB PRIMARY KEY) WITHOUT ROWID'
    )
    assert schema._sql_for_backend(sqlite_probe, compact).endswith(
        ' WITHOUT ROWID'
    )
    postgres_compact = schema._sql_for_backend(postgres_probe, compact)
    assert ' BYTEA ' in postgres_compact
    assert 'WITHOUT ROWID' not in postgres_compact


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

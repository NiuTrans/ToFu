"""Bounded PostgreSQL tuning and large-table maintenance contracts."""

from __future__ import annotations

import inspect
import os

import pytest

pytestmark = pytest.mark.unit


def test_managed_pg_knobs_read_same_project_env_from_standalone_tools(
        tmp_path, monkeypatch):
    """A migration/test import must not restage server defaults."""
    from lib.database._bootstrap import _config as config

    env_file = tmp_path / '.env'
    env_file.write_text(
        'TOFU_DB_MAX_CONNS=64\n'
        'TOFU_PG_MAX_CONNECTIONS=200\n', encoding='utf-8')
    monkeypatch.setattr(config, '_PROJECT_ENV_PATH', os.fspath(env_file))
    monkeypatch.setenv('TOFU_PG_MAX_CONNECTIONS', '96')

    assert config._project_config_value('TOFU_PG_MAX_CONNECTIONS') == '200'
    assert config._bounded_env_int(
        'TOFU_PG_MAX_CONNECTIONS', 96, 32, 4096) == 200


def test_memory_tuning_scales_but_stays_bounded(monkeypatch):
    from lib.database._bootstrap import _config as config

    for name in (
        'TOFU_PG_MEMORY_BUDGET_MB', 'TOFU_PG_SHARED_BUFFERS_MB',
        'TOFU_PG_EFFECTIVE_CACHE_MB', 'TOFU_PG_MAINTENANCE_WORK_MEM_MB',
        'TOFU_PG_WORK_MEM_MB',
    ):
        monkeypatch.delenv(name, raising=False)

    small = config._memory_tuning_mb(4096)
    large = config._memory_tuning_mb(262144)
    assert small['shared_buffers'] == 256
    assert large['shared_buffers'] == 2048
    assert small['effective_cache_size'] >= 2 * small['shared_buffers']
    assert large['effective_cache_size'] == 16384
    assert small['work_mem'] == large['work_mem'] == 8
    assert large['maintenance_work_mem'] <= 512


def test_invalid_memory_env_falls_back_instead_of_bricking_startup(monkeypatch):
    from lib.database._bootstrap import _config as config

    monkeypatch.setenv('TOFU_PG_SHARED_BUFFERS_MB', 'not-an-int')
    assert config._memory_tuning_mb(8192)['shared_buffers'] == 512


def test_managed_config_keeps_durability_and_adds_measured_tuning():
    from lib.database._bootstrap import _config as config

    body = config._build_managed_pg_config(archive_enabled=False)
    expected = {
        'fsync = on', 'synchronous_commit = on', 'full_page_writes = on',
        'idle_in_transaction_session_timeout = 120s',
        'checkpoint_timeout = 15min', 'max_wal_size = 4GB',
        'bgwriter_lru_maxpages = 1000', 'jit = off',
        'track_io_timing = on',
        'logging_collector = on',
        "log_directory = 'log'",
        "log_filename = 'postgresql-%a-%H.log'",
        'log_rotation_age = 1h',
        'log_rotation_size = 0',
        'log_truncate_on_rotation = on',
        'log_file_mode = 0600',
        'log_statement = none',
        'log_min_error_statement = panic',
        'log_parameter_max_length_on_error = 0',
        'log_connections = off', 'log_disconnections = off',
    }
    assert expected.issubset(set(body))
    assert any(line.startswith('shared_buffers = ') for line in body)
    assert any(line.startswith('effective_cache_size = ') for line in body)


def test_database_integer_knobs_fail_safe_and_are_bounded(monkeypatch):
    from lib.database import _core

    monkeypatch.setenv('TOFU_TEST_INTEGER_KNOB', 'not-an-integer')
    assert _core._bounded_env_int(
        'TOFU_TEST_INTEGER_KNOB', 17, 1, 100) == 17
    monkeypatch.setenv('TOFU_TEST_INTEGER_KNOB', '9999')
    assert _core._bounded_env_int(
        'TOFU_TEST_INTEGER_KNOB', 17, 1, 100) == 100
    assert _core._IDLE_IN_TRANSACTION_S == 120


def test_large_pg_tables_have_targeted_autovacuum_and_no_payload_stats():
    from lib.database._schema_pg import _chat

    src = inspect.getsource(_chat._apply_chat_runtime_tuning)
    for table in ('conversations', 'task_events', 'task_results',
                  'conversation_messages'):
        assert f'ALTER TABLE {table} SET (' in src
    assert 'ALTER TABLE conversations ALTER COLUMN messages SET STATISTICS 0' in src
    assert 'ALTER TABLE task_events ALTER COLUMN payload SET STATISTICS 0' in src
    assert 'idx_task_events_stream_ts' in src
    assert 'idx_task_terminal_retention' in src
    assert 'INCLUDE (conv_id)' in src
    assert "WHERE type NOT IN (" in src
    assert 'DROP INDEX IF EXISTS idx_conv_msgs_conv' in src
    assert ('jsonb_array_length(NEW.messages)'
            in _chat._CONVERSATIONS_REV_TRIGGER_FUNCTION_SQL)
    assert 'autovacuum_vacuum_cost_limit=1000' in src

    compression = '\n'.join(_chat._LZ4_COMPRESSION_STATEMENTS)
    for table, column in (
            ('conversations', 'messages'),
            ('conversation_messages', 'meta'),
            ('task_events', 'payload'),
            ('task_results', 'content'),
            ('transcript_archive', 'messages_json')):
        assert f'ALTER TABLE {table}' in compression
        assert f'ALTER COLUMN {column} SET COMPRESSION lz4' in compression


def test_runtime_tuning_is_not_hidden_behind_schema_version_fast_path():
    from lib.database._schema_pg import _init

    src = inspect.getsource(_init.init_db)
    fast_path = src[src.index('if current_version =='):src.index(
        "logger.info('[DB] Schema version %s")]
    assert '_apply_chat_runtime_tuning(conn)' in fast_path


def test_runtime_tuning_commits_all_metadata_changes():
    from lib.database._schema_pg import _chat

    class Cursor:
        def __init__(self):
            self.statements = []
            self.closed = False

        def execute(self, statement):
            self.statements.append(statement)

        def close(self):
            self.closed = True

    class Raw:
        def __init__(self, cursor): self._cursor = cursor
        def cursor(self): return self._cursor

    class Conn:
        def __init__(self):
            self.cursor = Cursor()
            self._conn = Raw(self.cursor)
            self.commits = 0
            self.rollbacks = 0

        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    conn = Conn()
    assert _chat._apply_chat_runtime_tuning(conn) is True
    assert conn.commits == 2 and conn.rollbacks == 0
    assert conn.cursor.closed is True
    assert len(conn.cursor.statements) == 12 + len(
        _chat._LZ4_COMPRESSION_STATEMENTS)


def test_unsupported_lz4_does_not_rollback_base_runtime_tuning():
    from lib.database._schema_pg import _chat

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            if 'SET COMPRESSION lz4' in statement:
                raise RuntimeError('compression method lz4 not supported')

        def close(self):
            pass

    class Raw:
        def __init__(self):
            self.cursors = []

        def cursor(self):
            cursor = Cursor()
            self.cursors.append(cursor)
            return cursor

    class Conn:
        def __init__(self):
            self._conn = Raw()
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    conn = Conn()
    assert _chat._apply_chat_runtime_tuning(conn) is True
    # The base trigger/autovacuum/index transaction committed. Only the
    # independent optional compression transaction rolled back.
    assert conn.commits == 1
    assert conn.rollbacks == 1
    assert len(conn._conn.cursors) == 2


def test_streaming_event_partial_index_is_in_full_schema_path():
    from lib.database._schema_pg import _chat

    src = inspect.getsource(_chat._init_chat_schema)
    assert 'idx_task_events_stream_ts' in src
    assert 'INCLUDE (task_id, event_id)' in src
    assert "'messages_snapshot','round_usage','round_start','round_end'" in src


def test_sqlite_task_first_retention_indexes_converge_on_version_fast_path():
    from lib.database._schema_sqlite import _chat, init_db

    tuning = inspect.getsource(_chat._apply_chat_runtime_tuning)
    assert 'idx_task_terminal_retention' in tuning
    assert 'ON task_results(completed_at, task_id, conv_id)' in tuning
    assert 'idx_task_events_stream_task_ts' in tuning
    assert 'ON task_events(task_id, ts_ms, event_id)' in tuning
    assert 'DROP INDEX IF EXISTS idx_task_events_stream_ts' in tuning
    assert 'DROP INDEX IF EXISTS idx_conv_msgs_conv' in tuning
    assert 'PRAGMA optimize' in tuning
    assert '_apply_chat_runtime_tuning(conn)' in inspect.getsource(init_db)

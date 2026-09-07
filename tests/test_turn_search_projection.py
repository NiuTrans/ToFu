"""Independent turn-search projection failure and resource contracts."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.schema import initialize_schema
from lib.storage_sidecar.turn_search_projection import (
    LOCAL_DATABASE_NAME,
    LocalSQLiteTurnSearchTarget,
    PROJECTION_NAME,
    TurnSearchProjectionRuntime,
)


pytestmark = pytest.mark.unit


def test_direct_sidecar_config_keeps_projection_out_of_working_directory(
        tmp_path):
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'
    data_dir.mkdir()
    logs_dir.mkdir()
    config = SidecarConfig(
        project_root=tmp_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        backend='sqlite',
        deployment_mode='personal',
        process_role='all',
        replica_id=None,
        token='projection-config-test-token-' * 2,
        sqlite_path=data_dir / 'tofu.db',
        postgres_dsn='',
        redis_url='',
        allow_schema_migration=True,
        read_pool_size=1,
        write_pool_size=1,
    )

    assert config.turn_search_projection_dir \
        == (data_dir / 'projections').resolve()
    assert config.turn_search_projection_dir != __import__('pathlib').Path('.')


def _snapshot(*, user_id=1, conversation_id='conv-1', turn_id='turn-1',
              content='searchable needle'):
    return {
        'conversation_id': conversation_id,
        'conversation_updated_at_ms': 100,
        'user_id': user_id,
        'turn_id': turn_id,
        'lane_id': 'main',
        'ordinal': 0,
        'actor': 'human',
        'status': 'completed',
        'projection_json': orjson.dumps({'content': content}),
        'projection_revision': 1,
        'updated_at': 100,
    }


def test_local_projection_is_disposable_private_and_searchable(tmp_path):
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_search_op,
    )

    target = LocalSQLiteTurnSearchTarget(
        tmp_path / 'projection', 128 * 1024 * 1024)
    target.start()
    try:
        target.apply_turn(
            {'user_id': 1, 'entity_key': 'turn-1'},
            _snapshot(),
            'generation-1',
        )
        result = target.query(
            lambda session: _conversation_search_op(session, {
                'query': 'searchable needle',
                'user_id': 1,
                'limit': 10,
                'snippet_radius': 20,
            }),
            time.monotonic() + 1,
        )

        assert [row['id'] for row in result] == ['conv-1']
        assert target.path == tmp_path / 'projection' / LOCAL_DATABASE_NAME
        if os.name != 'nt':
            assert target.directory.stat().st_mode & 0o077 == 0
    finally:
        target.close()


def test_corrupt_projection_is_discarded_without_artifact_growth(tmp_path):
    directory = tmp_path / 'projection'
    directory.mkdir()
    (directory / LOCAL_DATABASE_NAME).write_bytes(b'not a sqlite database')

    target = LocalSQLiteTurnSearchTarget(directory, 128 * 1024 * 1024)
    target.start()
    try:
        assert target.status()['last_error'] == 'corrupt_projection_rebuilt'
        assert not list(directory.glob('*.corrupt-*'))
        target.apply_turn(
            {'user_id': 1, 'entity_key': 'turn-1'},
            _snapshot(),
            'generation-after-recovery',
        )
    finally:
        target.close()


def test_projection_capacity_failure_is_explicit(tmp_path):
    target = LocalSQLiteTurnSearchTarget(tmp_path / 'projection', 1)
    target.start()
    try:
        with pytest.raises(StorageError, match='resource budget'):
            target.apply_turn(
                {'user_id': 1, 'entity_key': 'turn-1'},
                _snapshot(),
                'generation-1',
            )
        assert target.status()['state'] == 'capacity_exceeded'
    finally:
        target.close()


class _Backend:
    name = 'sqlite'

    def __init__(self, connection):
        self.connection = connection

    def query(self, _name, operation, _deadline_at):
        return operation(SQLiteSession(self.connection))

    def command(
        self, _name, _digest, _command_id, _priority, operation, _deadline_at,
        *, receipt_required,
    ):
        assert receipt_required is False
        result = operation(SQLiteSession(self.connection))
        self.connection.commit()
        return result


class _UnusedTarget:
    pass


def _authority():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    initialize_schema(SQLiteSession(connection))
    connection.commit()
    return connection


def _insert_authority_turn(connection, snapshot):
    user_id = snapshot['user_id']
    conversation_id = snapshot['conversation_id']
    connection.execute(
        'INSERT INTO storage_conversations('
        'id,user_id,title,messages_json,created_at_ms,updated_at_ms,'
        'settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (conversation_id, user_id, '', b'[]', 1, 100, b'{}', 0, '', 1),
    )
    connection.execute(
        'INSERT INTO storage_conversation_turns('
        'turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,'
        'kind,run_id,status,current_attempt_id,projection_json,'
        'projection_revision,settlement_json,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            snapshot['turn_id'], conversation_id, user_id, 'main', None, 0,
            'human', 'reply', '', 'completed', None,
            snapshot['projection_json'], 1, b'{}', 1, 100,
        ),
    )
    connection.commit()


def test_backfill_cursor_is_owner_scoped_and_skips_oversize_source_rows():
    connection = _authority()
    try:
        # Reverse lexical turn ids across owners prove pagination follows the
        # repository owner tuple rather than an accidental turn-id ordering.
        _insert_authority_turn(connection, _snapshot(
            user_id=1, conversation_id='conv-a', turn_id='z-turn',
            # UTF-8 plus the storage codec's bounded 2x hydration ratio leave
            # 250K source units inside the worker's 2 MiB page budget.
            content='x' * 250_001))
        _insert_authority_turn(connection, _snapshot(
            user_id=2, conversation_id='conv-b', turn_id='a-turn',
            content='small searchable row'))
        runtime = TurnSearchProjectionRuntime(
            _Backend(connection), _UnusedTarget(), backfill_delay_s=0)

        page = runtime._backfill_page('')

        assert page['skipped'] == 1
        assert len(page['rows']) == 1
        assert page['rows'][0]['user_id'] == 2
        assert runtime._decode_backfill_cursor(page['cursor']) == (
            2, 'conv-b', 'a-turn')
    finally:
        connection.close()


def test_stale_ack_cannot_delete_a_newer_dirty_marker():
    connection = _authority()
    try:
        connection.execute(
            'INSERT INTO storage_projection_outbox('
            'projection_name,entity_kind,user_id,entity_key,version_token,'
            'enqueued_at_ms) VALUES (?,?,?,?,?,?)',
            (PROJECTION_NAME, 'turn', 1, 'turn-1', 'new-token', 1),
        )
        connection.commit()
        runtime = TurnSearchProjectionRuntime(
            _Backend(connection), _UnusedTarget(), backfill_delay_s=0)

        assert runtime._ack({
            'entity_kind': 'turn', 'user_id': 1, 'entity_key': 'turn-1',
            'version_token': 'old-token',
        }) is False
        row = connection.execute(
            'SELECT version_token FROM storage_projection_outbox').fetchone()
        assert row['version_token'] == 'new-token'
    finally:
        connection.close()


def test_shutdown_defers_target_close_until_stuck_authority_read_returns(
        monkeypatch):
    started = threading.Event()
    release = threading.Event()
    target_closed = threading.Event()

    class _BlockingBackend:
        name = 'sqlite'

        def query(self, _name, _operation, _deadline_at):
            started.set()
            release.wait(2)
            return []

    class _TrackedTarget:
        def start(self):
            return None

        def close(self):
            target_closed.set()

        def status(self):
            return {'state': 'warming'}

    # Keep this regression fast while preserving the production five-second
    # bounded join in the runtime.
    real_join = threading.Thread.join

    def short_join(thread, timeout=None):
        return real_join(thread, min(float(timeout or 0.02), 0.02))

    monkeypatch.setattr(threading.Thread, 'join', short_join)
    runtime = TurnSearchProjectionRuntime(
        _BlockingBackend(), _TrackedTarget(), backfill_delay_s=60)
    runtime.start()
    assert started.wait(1)

    runtime.close()
    assert not target_closed.is_set()
    assert runtime.status()['last_worker_error'] \
        == 'shutdown_waiting_for_authority_io'

    release.set()
    assert target_closed.wait(1)

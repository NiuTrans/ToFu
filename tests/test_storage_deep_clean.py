"""Offline deep-clean pass: retention delete + verified atomic compaction.

Pins the operator-window contract of scripts/storage_deep_clean.py against a
synthetic miniature authority:

• settled-and-old attempts' transport rows are deleted; live and fresh rows
  survive;
• VACUUM INTO + swap shrinks the file, preserves auto_vacuum=INCREMENTAL and
  every authority table's row count, and retains the pre-clean file;
• low-space incremental reclaim shrinks in place without a second DB copy;
• a live project lease refuses the pass outright.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from lib.storage_sidecar.schema import (
    LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT,
    TASK_EVENT_RETENTION_INDEX_NAMES,
)

pytestmark = pytest.mark.unit


def _load_module():
    spec = importlib.util.spec_from_file_location(
        'storage_deep_clean',
        str(Path(__file__).resolve().parents[1]
            / 'scripts' / 'storage_deep_clean.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_conversation_mirror_schema(connection):
    storage_columns = {
        str(row[1])
        for row in connection.execute(
            'PRAGMA table_info("storage_conversations")')
    }
    if 'messages_json' not in storage_columns:
        connection.execute(
            "ALTER TABLE storage_conversations ADD COLUMN "
            "messages_json TEXT NOT NULL DEFAULT '[]'")
    connection.execute(
        'CREATE TABLE conversations ('
        'id TEXT NOT NULL, user_id INTEGER NOT NULL, messages TEXT NOT NULL, '
        'PRIMARY KEY(id,user_id))')
    connection.execute(
        'CREATE TABLE conversation_messages ('
        'conv_id TEXT NOT NULL, seq INTEGER NOT NULL, '
        "content TEXT NOT NULL DEFAULT '', "
        "content_json TEXT NOT NULL DEFAULT '[]', "
        "thinking TEXT NOT NULL DEFAULT '', "
        "translated_content TEXT NOT NULL DEFAULT '', "
        "meta TEXT NOT NULL DEFAULT '{}', meta_light TEXT, "
        "billing_meta TEXT, translation_state TEXT, "
        'PRIMARY KEY(conv_id,seq))')
    connection.execute(
        'CREATE TABLE conversation_turns ('
        'turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, '
        'user_id INTEGER NOT NULL)')


def _insert_legacy_conversation(
        connection, conv_id, legacy_messages, *, current_messages=None,
        mirror_messages=None, with_turn=False):
    connection.execute(
        'INSERT INTO conversations VALUES (?,?,?)',
        (conv_id, 1, json.dumps(legacy_messages, indent=2)))
    if current_messages is not None:
        connection.execute(
            'INSERT INTO storage_conversations('
            'id,user_id,messages_json) VALUES (?,?,?)',
            (conv_id, 1, json.dumps(
                current_messages, separators=(',', ':'), sort_keys=True)),
        )
    for sequence, message in enumerate(
            legacy_messages if mirror_messages is None else mirror_messages):
        connection.execute(
            'INSERT INTO conversation_messages('
            'conv_id,seq,meta,translation_state) VALUES (?,?,?,?)',
            (conv_id, sequence,
             json.dumps(message, separators=(',', ':')),
             '{"v":1}'),
        )
    if with_turn:
        connection.execute(
            'INSERT INTO conversation_turns VALUES (?,?,?)',
            (f'turn-{conv_id}', conv_id, 1))


@pytest.fixture
def authority(tmp_path: Path):
    """A tiny WAL authority with one old-settled and one live attempt."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    path = data_dir / 'tofu.db'
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
    connection.execute('VACUUM')
    connection.execute(
        'CREATE TABLE storage_generation_attempts ('
        'attempt_id TEXT PRIMARY KEY, status TEXT NOT NULL, '
        'settled_at BIGINT)')
    connection.execute(
        'CREATE TABLE storage_attempt_events ('
        'attempt_id TEXT NOT NULL, sequence BIGINT NOT NULL, '
        'payload_json TEXT NOT NULL, '
        'PRIMARY KEY(attempt_id, sequence))')
    connection.execute(
        'CREATE TABLE storage_conversation_changes ('
        'attempt_id TEXT NOT NULL, attempt_sequence BIGINT)')
    connection.execute(
        'CREATE TABLE storage_conversations ('
        'id TEXT PRIMARY KEY, user_id BIGINT NOT NULL DEFAULT 1, '
        'rev BIGINT, updated_at_ms BIGINT NOT NULL DEFAULT 0, '
        'title TEXT NOT NULL DEFAULT \'\', '
        'created_at_ms BIGINT NOT NULL DEFAULT 0, '
        'settings_json BLOB NOT NULL DEFAULT \'{}\', '
        "messages_json BLOB NOT NULL DEFAULT '[]', "
        "search_text TEXT NOT NULL DEFAULT '', "
        'msg_count BIGINT NOT NULL DEFAULT 0)')
    connection.execute(
        'CREATE TABLE storage_events ('
        'task_id TEXT NOT NULL, sequence BIGINT NOT NULL, '
        "stream_kind TEXT NOT NULL DEFAULT 'task', "
        "event_type TEXT NOT NULL DEFAULT '', "
        "event_kind TEXT NOT NULL DEFAULT '', "
        'event_json BLOB NOT NULL, created_at_ms BIGINT NOT NULL, '
        'PRIMARY KEY(task_id, sequence))')
    connection.execute(
        'CREATE INDEX idx_storage_events_retention '
        'ON storage_events(stream_kind, event_type, created_at_ms)')
    connection.execute(
        'CREATE TABLE attempt_events ('
        'attempt_id TEXT NOT NULL, seq INTEGER NOT NULL, '
        'payload TEXT NOT NULL, created_at INTEGER NOT NULL, '
        'PRIMARY KEY(attempt_id, seq))')
    connection.execute(
        'CREATE INDEX idx_attempt_events_created '
        'ON attempt_events(created_at)')
    connection.execute(
        'CREATE TABLE task_events ('
        'task_id TEXT NOT NULL, event_id INTEGER NOT NULL, '
        'ts_ms INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, '
        'PRIMARY KEY(task_id, event_id))')
    connection.execute(
        'CREATE INDEX idx_task_events_ts ON task_events(ts_ms)')
    fat = 'x' * (128 * 1024)
    now = int(time.time() * 1000)
    old = now - 10 * 86_400_000
    old_structural = old - 31 * 86_400_000
    large_snapshot = json.dumps({
        'type': 'messages_snapshot',
        'kind': 'request',
        'messages': [{'role': 'user', 'content': 'repeatable ' * 20_000}],
    }, separators=(',', ':'))
    connection.execute(
        "INSERT INTO storage_generation_attempts VALUES ('old', 'completed', ?)",
        (old,))
    connection.execute(
        "INSERT INTO storage_generation_attempts VALUES ('live', 'running', NULL)")
    for seq in range(200):
        connection.execute(
            "INSERT INTO storage_attempt_events VALUES ('old', ?, ?)",
            (seq, fat))
    for seq in range(3):
        connection.execute(
            "INSERT INTO storage_attempt_events VALUES ('live', ?, ?)",
            (seq, fat))
    connection.execute(
        "INSERT INTO storage_conversations(id, rev) VALUES ('c1', 7)")
    connection.executemany(
        'INSERT INTO storage_events('
        'task_id, sequence, stream_kind, event_type, event_kind, '
        'event_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
            ('migration-task', 1, 'task', '', '',
             '{"type":"delta","content":"expired"}', old),
            ('migration-task', 2, 'task', '', '', large_snapshot, old),
            ('migration-task', 3, 'task', '', '',
             '{"type":"messages_snapshot","kind":"state"}',
             old_structural),
            ('migration-task', 4, 'task', '', '',
             '{"type":"delta","content":"fresh"}', now),
            ('migration-task', 5, 'task', '', '', '{}', old),
            ('migration-task', 6, 'task', '', '', '{}', old_structural),
            ('migration-task', 7, 'task', '', '', b'not-json', old),
            ('migration-task', 8, 'task', 'messages_snapshot', 'request',
             large_snapshot, old),
            ('migration-task', 9, 'task', 'delta', '',
             '{"type":"delta","content":"expired typed"}', old),
            ('migration-task', 10, 'task', 'messages_snapshot', 'state',
             '{"type":"messages_snapshot","kind":"state"}',
             old_structural),
            ('project-stream', 1, 'project_feed', '', '',
             '{"type":"delta","content":"project"}', old_structural),
        ],
    )
    connection.executemany(
        'INSERT INTO attempt_events VALUES (?, ?, ?, ?)',
        [
            ('legacy-a', 1, '旧中间帧', old),
            ('legacy-a', 2, 'latest state', old + 1),
            ('legacy-b', 1, 'only state', old),
        ],
    )
    connection.executemany(
        'INSERT INTO task_events VALUES (?, ?, ?, ?, ?)',
        [
            ('legacy-task', 1, old, 'delta', '过期流'),
            ('legacy-task', 2, old_structural, 'messages_snapshot',
             'expired structural'),
            ('legacy-task', 3, now, 'delta', 'fresh stream'),
        ],
    )
    connection.commit()
    connection.close()
    yield tmp_path, path


def test_offline_compact_retains_only_eligible_rows_and_shrinks(authority):
    module = _load_module()
    project_root, path = authority
    old_rollback = (
        path.parent / 'tofu.db.pre-compact-20200101T000000Z')
    old_rollback.write_bytes(b'older rollback')
    os.utime(old_rollback, (1, 1))
    before = path.stat().st_size
    assert before > 200 * 128 * 1024  # the bloat is really there

    report = module.offline_compact(project_root, ttl_days=1.0)

    after = path.stat().st_size
    assert report['ok'] is True
    assert report['deleted_rows'] == 200
    assert after < before // 4, 'compaction must reclaim the deleted mass'
    assert report['after']['auto_vacuum'] == 'incremental'
    retained = Path(report['retained'])
    assert retained.is_file() and retained.stat().st_size == before
    assert not old_rollback.exists()
    assert report['rollback_retention'] == {
        'retention_count': 1,
        'removed': [old_rollback.name],
        'errors': [],
    }
    assert report['legacy_transport']['attempt_events']['deleted_rows'] == 1
    assert report['legacy_transport']['attempt_events'][
        'deleted_payload_bytes'] == len('旧中间帧'.encode())
    assert report['legacy_transport']['attempt_events']['retained_rows'] == 2
    assert report['legacy_transport']['task_events']['streaming'][
        'deleted_rows'] == 1
    assert report['legacy_transport']['task_events']['streaming'][
        'deleted_payload_bytes'] == len('过期流'.encode())
    assert report['legacy_transport']['task_events']['structural'][
        'deleted_rows'] == 1
    assert report['legacy_transport']['task_events']['retained_rows'] == 1
    event_maintenance = report['task_event_maintenance']
    assert event_maintenance['scanned_rows'] == 10
    assert event_maintenance['deleted_rows'] == 5
    assert event_maintenance['deleted_streaming_rows'] == 2
    assert event_maintenance['deleted_structural_rows'] == 3
    assert event_maintenance['reclassified_blank_rows'] == 2
    assert event_maintenance['opaque_blank_rows'] == 3
    assert event_maintenance['invalid_blank_rows'] == 1
    assert event_maintenance['compressed_rows'] == 2
    assert event_maintenance['compression_saved_bytes'] > 100_000
    assert event_maintenance['retained_rows'] == 5
    assert event_maintenance['retained_blank_rows'] == 2
    archive_maintenance = report['compaction_archive_maintenance']
    assert archive_maintenance['mode'] == 'bounded_codec_and_legacy_migration'
    assert archive_maintenance['current_archive_codec'] == {
        'mode': 'unsupported_schema', 'updated_rows': 0}
    assert archive_maintenance['legacy_archive_migration'] == {
        'mode': 'unsupported_schema', 'migrated_rows': 0}
    assert report['task_result_maintenance'] == {
        'mode': 'unsupported_schema', 'updated_rows': 0}

    connection = sqlite3.connect(str(path))
    live_rows = connection.execute(
        "SELECT count(*) FROM storage_attempt_events WHERE attempt_id='live'"
    ).fetchone()[0]
    old_rows = connection.execute(
        "SELECT count(*) FROM storage_attempt_events WHERE attempt_id='old'"
    ).fetchone()[0]
    conv_rev = connection.execute(
        "SELECT rev FROM storage_conversations WHERE id='c1'").fetchone()[0]
    legacy_attempt_rows = connection.execute(
        'SELECT attempt_id, seq, payload FROM attempt_events '
        'ORDER BY attempt_id, seq').fetchall()
    legacy_task_rows = connection.execute(
        'SELECT task_id, event_id, type, payload FROM task_events '
        'ORDER BY task_id, event_id').fetchall()
    task_event_rows = connection.execute(
        'SELECT sequence, event_type, event_kind, event_json '
        'FROM storage_events WHERE task_id=? ORDER BY sequence',
        ('migration-task',),
    ).fetchall()
    project_event_rows = connection.execute(
        'SELECT event_type, event_json FROM storage_events '
        'WHERE task_id=?',
        ('project-stream',),
    ).fetchall()
    connection.close()
    assert live_rows == 3
    assert old_rows == 0
    assert conv_rev == 7
    assert legacy_attempt_rows == [
        ('legacy-a', 2, 'latest state'),
        ('legacy-b', 1, 'only state'),
    ]
    assert legacy_task_rows == [
        ('legacy-task', 3, 'delta', 'fresh stream'),
    ]
    assert [row[0] for row in task_event_rows] == [2, 4, 5, 7, 8]
    by_sequence = {row[0]: row for row in task_event_rows}
    assert by_sequence[2][1:3] == ('messages_snapshot', 'request')
    assert by_sequence[4][1:3] == ('delta', '')
    assert by_sequence[5][1:3] == ('', '')
    assert by_sequence[7][1:3] == ('', '')
    assert by_sequence[8][1:3] == ('messages_snapshot', 'request')
    from lib.storage_sidecar.task_event_codec import (
        COMPRESSED_TASK_EVENT_MAGIC,
        decode_task_event_payload,
    )
    for sequence in (2, 8):
        stored = bytes(by_sequence[sequence][3])
        assert stored.startswith(COMPRESSED_TASK_EVENT_MAGIC)
        assert json.loads(decode_task_event_payload(stored))[
            'type'] == 'messages_snapshot'
    assert decode_task_event_payload(by_sequence[5][3]) == b'{}'
    assert decode_task_event_payload(by_sequence[7][3]) == b'not-json'
    assert project_event_rows == [(
        '', '{"type":"delta","content":"project"}')]
    assert report['parity']['storage_conversations'] == 1
    assert report['parity']['storage_events'] == 6
    assert 'idx_storage_conversations_meta' in report['installed_indexes']
    assert TASK_EVENT_RETENTION_INDEX_NAMES <= set(report['installed_indexes'])
    assert report['retired_indexes'] == ['idx_storage_events_retention']
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' "
            "AND name='idx_storage_conversations_meta'"
        ).fetchone() == (1,)
        installed = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'")
        }
        assert TASK_EVENT_RETENTION_INDEX_NAMES <= installed
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' "
            "AND name='idx_storage_events_retention'"
        ).fetchone() is None
    finally:
        connection.close()


def test_offline_compact_refuses_a_live_lease(authority):
    module = _load_module()
    project_root, _path = authority
    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(project_root / 'data')
    lease.acquire()
    try:
        with pytest.raises(Exception):
            module.offline_compact(project_root, ttl_days=1.0)
    finally:
        lease.release()


@pytest.mark.parametrize('dangling_link', [
    False,
    pytest.param(True, marks=pytest.mark.skipif(
        os.name == 'nt', reason='dangling symlink contract is POSIX-only')),
])
def test_offline_compact_preserves_preexisting_artifact_collision(
        authority, monkeypatch, dangling_link):
    module = _load_module()
    project_root, path = authority
    from datetime import datetime as RealDateTime

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return RealDateTime(2026, 8, 27, 1, 2, 3, tzinfo=tz)

    monkeypatch.setattr(module, 'datetime', FixedDateTime)
    collision = path.parent / '.tofu.db.compact-20260827T010203Z'
    if dangling_link:
        collision.symlink_to('missing-candidate')
    else:
        collision.write_bytes(b'preexisting candidate')

    with pytest.raises(RuntimeError, match='artifact collision'):
        module.offline_compact(project_root, ttl_days=1.0)

    if dangling_link:
        assert collision.is_symlink()
        assert os.readlink(collision) == 'missing-candidate'
    else:
        assert collision.read_bytes() == b'preexisting candidate'
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT count(*) FROM storage_attempt_events').fetchone() == (203,)
    finally:
        connection.close()


def test_low_space_compact_reclaims_in_place_without_rollback_copy(authority):
    module = _load_module()
    project_root, path = authority
    before = path.stat().st_size

    report = module.offline_compact(
        project_root, ttl_days=1.0, low_space=True)

    assert report['ok'] is True
    assert report['reclaim_mode'] == 'incremental-low-space'
    assert report['deleted_rows'] == 200
    assert report['incremental']['reclaimed_pages'] > 0
    assert path.stat().st_size < before // 4
    assert not list(path.parent.glob('tofu.db.pre-compact-*'))
    assert report['parity']['storage_conversations'] == 1
    assert 'idx_storage_conversations_meta' in report['installed_indexes']
    assert TASK_EVENT_RETENTION_INDEX_NAMES <= set(report['installed_indexes'])
    assert report['retired_indexes'] == ['idx_storage_events_retention']
    assert report['legacy_transport']['attempt_events']['deleted_rows'] == 1
    assert report['task_event_maintenance']['deleted_rows'] == 5
    assert report['task_event_maintenance']['compressed_rows'] == 2
    assert report['task_result_maintenance'] == {
        'mode': 'unsupported_schema', 'updated_rows': 0}


def test_no_vacuum_performs_in_place_deferred_index_transition(authority):
    module = _load_module()
    project_root, path = authority

    report = module.offline_compact(
        project_root, ttl_days=1.0, vacuum=False)

    assert report['ok'] is True
    assert report['vacuum'] is False
    assert TASK_EVENT_RETENTION_INDEX_NAMES <= set(report['installed_indexes'])
    assert report['retired_indexes'] == ['idx_storage_events_retention']
    assert report['legacy_transport'] == {
        'mode': 'skipped_without_physical_reclaim'}
    assert report['task_event_maintenance'] == {
        'mode': 'skipped_without_physical_reclaim'}
    assert report['compaction_archive_maintenance'] == {
        'mode': 'skipped_without_physical_reclaim'}
    assert report['task_result_maintenance'] == {
        'mode': 'skipped_without_physical_reclaim'}
    connection = sqlite3.connect(str(path))
    try:
        indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'")
        }
    finally:
        connection.close()
    assert TASK_EVENT_RETENTION_INDEX_NAMES <= indexes
    assert 'idx_storage_events_retention' not in indexes
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT count(*) FROM attempt_events').fetchone() == (3,)
        assert connection.execute(
            'SELECT count(*) FROM task_events').fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM storage_events WHERE stream_kind='task'"
        ).fetchone() == (10,)
        assert connection.execute(
            "SELECT count(*) FROM storage_events "
            "WHERE stream_kind='task' AND event_type=''"
        ).fetchone() == (7,)
    finally:
        connection.close()


def test_analyze_reports_header_and_tables(authority):
    module = _load_module()
    project_root, path = authority
    search_text = 'rebuildable frozen search projection'
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE storage_records('
        'namespace TEXT NOT NULL,record_key TEXT NOT NULL,'
        'value_json BLOB NOT NULL,version INTEGER NOT NULL,'
        'PRIMARY KEY(namespace,record_key))'
    )
    task_result_document = json.dumps({
        'task_id': 'large-task-result',
        'segments': 'repeated task result segment ' * 4_000,
    }, separators=(',', ':')).encode()
    knowledge_document = b'{"body":"small"}'
    connection.executemany(
        'INSERT INTO storage_records VALUES (?,?,?,?)',
        [
            ('task_results', 'large-task-result', task_result_document, 1),
            ('knowledge', 'small-document', knowledge_document, 1),
        ],
    )
    connection.execute(
        'UPDATE storage_conversations SET search_text=? WHERE id=?',
        (search_text, 'c1'),
    )
    connection.commit()
    connection.close()
    report = module.analyze(project_root)
    header = report['header']
    assert header['bytes'] == path.stat().st_size
    assert header['auto_vacuum'] == 'incremental'
    assert header['live_bytes'] <= header['bytes']
    assert 0.0 <= header['freelist_ratio'] <= 1.0
    plan = report['compaction_plan']
    assert plan['source_bytes'] == header['live_bytes']
    assert plan['required_free_bytes'] > plan['source_bytes']
    assert isinstance(plan['verified_copy_capacity_ok'], bool)
    assert isinstance(plan['offline_compaction_recommended'], bool)
    assert set(plan['missing_deferred_indexes']) == {
        'idx_storage_conversations_meta', *TASK_EVENT_RETENTION_INDEX_NAMES}
    assert plan['obsolete_deferred_indexes'] == [
        'idx_storage_events_retention']
    assert plan['offline_index_maintenance_required'] is True
    assert plan['online_event_retention'] == {
        'mode': 'legacy_exact_type',
        'legacy_event_type_limit': LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT,
    }
    search_retirement = plan['conversation_search_text_retirement']
    assert search_retirement == {
        'available': True,
        'mode': 'rebuildable_header_projection_retirement',
        'measurement': 'exact_encoded_payload',
        'measurement_complete': True,
        'candidate_rows': 1,
        'payload_bytes': len(search_text),
    }
    conversation_storage = report['tables']['storage_conversations']
    assert conversation_storage['measurement'] == 'exact_encoded_payload'
    assert conversation_storage['row_count'] == 1
    assert conversation_storage['payload_bytes'] == 2 + len(search_text)
    record_storage = report['tables']['storage_records']
    assert record_storage['measurement'] == 'exact_encoded_payload'
    assert record_storage['row_count'] == 2
    assert record_storage['payload_bytes'] == (
        len(task_result_document) + len(knowledge_document))
    assert record_storage['task_result_field_codec_candidates'] == {
        'measurement': 'exact_threshold_source_payload',
        'minimum_document_bytes': 32 * 1024,
        'row_count': 1,
        'source_document_bytes': len(task_result_document),
        'semantic_savings_require_offline_validation': True,
    }
    legacy_retention = plan['transport_retention']
    assert legacy_retention['measurement_complete'] is True
    assert legacy_retention['timed_out'] is False
    assert legacy_retention['ttl_days'] == 1.0
    assert legacy_retention['candidate_rows'] == 203
    assert legacy_retention['candidate_payload_bytes'] == (
        200 * 128 * 1024
        + sum(map(len, (
            '旧中间帧'.encode(),
            '过期流'.encode(),
            b'expired structural',
        )))
    )
    assert legacy_retention['sources']['storage_attempt_events'] == {
        'measurement': 'exact_expired_payload',
        'row_count': 200,
        'payload_bytes': 200 * 128 * 1024,
    }
    assert legacy_retention['sources']['attempt_events'] == {
        'measurement': 'exact_expired_payload',
        'row_count': 1,
        'payload_bytes': len('旧中间帧'.encode()),
    }
    assert legacy_retention['sources']['task_events_streaming'][
        'row_count'] == 1
    assert legacy_retention['sources']['task_events_structural'][
        'row_count'] == 1
    maintenance = plan['offline_maintenance']
    assert maintenance['recommended'] is True
    assert maintenance['reasons'] == [
        'expired_transport',
        'rebuildable_conversation_search_text',
        'deferred_index_maintenance',
    ]
    assert maintenance['requires_stopped_server'] is True
    assert maintenance['requires_physical_reclaim'] is True
    assert maintenance['expired_transport_payload_bytes'] == (
        legacy_retention['candidate_payload_bytes'])
    assert maintenance['rebuildable_conversation_search_text_bytes'] == len(
        search_text)
    assert maintenance['recommended_command'] == (
        'python3 scripts/storage_deep_clean.py --offline '
        '--ttl-days 1 --confirm')
    assert maintenance['blocked_reason'] == ''
    events = report['tables']['storage_attempt_events']
    assert events['measurement'] == 'exact_encoded_payload'
    assert events['row_count'] == 203
    assert events['min_rowid'] == 1
    assert events['max_rowid'] == 203
    assert events['rowid_span'] == 203
    assert events['rowid_holes'] == 0
    assert events['payload_bytes'] == 203 * 128 * 1024
    assert events['estimated_bytes'] == events['payload_bytes']
    assert events['estimated_bytes_is_exact'] is True
    storage_events = report['tables']['storage_events']
    assert storage_events['row_count'] == 11
    assert storage_events['groups_truncated'] is False
    event_groups = {
        (row['stream_kind'], row['event_type']): row
        for row in storage_events['groups']
    }
    assert event_groups[('task', '')]['row_count'] == 7
    assert event_groups[('task', 'messages_snapshot')]['row_count'] == 2
    assert event_groups[('task', 'delta')]['row_count'] == 1
    assert event_groups[('project_feed', '')]['row_count'] == 1
    scan = report['table_scan']
    assert scan['method'] == 'exact_encoded_payload'
    assert scan['budget_seconds'] == module._ANALYZE_SQL_BUDGET_SECONDS
    assert scan['completed_tables'] == len(report['tables'])
    assert scan['completed_retention_sources'] == 4
    assert scan['timed_out'] is False
    assert scan['elapsed_seconds'] >= 0
    recovery = report['recovery_artifacts']
    assert recovery['deep_clean_rollbacks']['count'] == 0
    assert recovery['verified_sqlite_backups']['count'] == 0
    assert recovery['operator_managed_large_files']['count'] == 0
    assert recovery['total_allocated_bytes'] == 0


def test_analyze_exact_measurement_counts_rowid_holes_and_utf8_bytes():
    module = _load_module()
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE payloads(payload TEXT NOT NULL)')
    connection.executemany(
        'INSERT INTO payloads(payload) VALUES (?)',
        [('a',), ('中',), ('three',)],
    )
    connection.execute('DELETE FROM payloads WHERE rowid=2')

    measurement = module._measure_table(
        connection,
        'payloads',
        ('payload',),
        deadline_at=time.monotonic() + 1.0,
    )

    assert measurement == {
        'measurement': 'exact_encoded_payload',
        'row_count': 2,
        'min_rowid': 1,
        'max_rowid': 3,
        'rowid_span': 3,
        'rowid_holes': 1,
        'payload_bytes': len(b'a') + len(b'three'),
        'avg_payload_bytes': 3,
        'estimated_bytes': len(b'a') + len(b'three'),
        'estimated_bytes_is_exact': True,
    }
    with pytest.raises(TimeoutError, match='budget exhausted'):
        module._measure_table(
            connection,
            'payloads',
            ('payload',),
            deadline_at=time.monotonic() - 1.0,
        )
    connection.close()


def test_analyze_measures_turn_codec_candidates_in_the_same_scan():
    module = _load_module()
    module._TURN_PROJECTION_CODEC_MIN_BYTES = 8
    connection = sqlite3.connect(':memory:')
    connection.execute(
        'CREATE TABLE storage_conversation_turns('
        'projection_json BLOB NOT NULL,settlement_json BLOB NOT NULL)'
    )
    connection.executemany(
        'INSERT INTO storage_conversation_turns VALUES (?,?)',
        [(b'{}', b'{}'), (b'12345678', b'abc'), (b'1234567890', b'd')],
    )

    measurement = module._measure_storage_conversation_turns(
        connection,
        deadline_at=time.monotonic() + 1.0,
    )

    assert measurement['row_count'] == 3
    assert measurement['payload_bytes'] == 2 + 2 + 8 + 3 + 10 + 1
    assert measurement['turn_projection_codec_candidates'] == {
        'measurement': 'exact_threshold_source_payload',
        'minimum_projection_bytes': 8,
        'row_count': 2,
        'projection_bytes': 18,
        'semantic_savings_require_offline_validation': True,
    }
    with pytest.raises(TimeoutError, match='budget exhausted'):
        module._measure_storage_conversation_turns(
            connection,
            deadline_at=time.monotonic() - 1.0,
        )
    connection.close()


def test_analyze_table_scan_stops_at_one_shared_deadline(authority):
    module = _load_module()
    project_root, _path = authority
    module._ANALYZE_SQL_BUDGET_SECONDS = 0.0

    report = module.analyze(project_root)

    assert report['table_scan']['timed_out'] is True
    assert report['table_scan']['completed_tables'] == 0
    assert report['table_scan']['completed_retention_sources'] == 0
    assert report['tables']
    assert all(
        row == {
            'error': 'analysis_budget_exhausted',
            'measurement': 'not_completed',
        }
        for row in report['tables'].values()
    )
    retention = report['compaction_plan']['transport_retention']
    assert retention['measurement_complete'] is False
    assert retention['timed_out'] is True
    assert all(
        row['error'] == 'analysis_budget_exhausted'
        for row in retention['sources'].values()
        if row['measurement'] == 'not_completed'
    )
    search_retirement = report['compaction_plan'][
        'conversation_search_text_retirement']
    assert search_retirement['measurement_complete'] is False
    assert search_retirement['error'] == 'analysis_budget_exhausted'


def test_analyze_rejects_nonpositive_or_nonfinite_retention_horizon(authority):
    module = _load_module()
    project_root, _path = authority

    for ttl_days in (0, -1, float('inf'), float('nan')):
        with pytest.raises(ValueError, match='positive finite'):
            module.analyze(project_root, ttl_days=ttl_days)


def test_analyze_inventories_tool_owned_and_operator_owned_recovery(authority):
    module = _load_module()
    project_root, path = authority
    data_dir = path.parent
    module._OPERATOR_MANAGED_LARGE_FILE_MIN_BYTES = 1
    rollback = data_dir / 'tofu.db.pre-compact-20200101T000000Z'
    rollback.write_bytes(b'rollback')
    operator_file = data_dir / 'pg_backup.sql'
    operator_file.write_bytes(b'postgres rollback')
    backups = data_dir / 'backups'
    backups.mkdir()
    backup = backups / 'storage-sqlite-20260824T000000Z.sqlite3'
    backup.write_bytes(b'sqlite backup')
    backup.with_name(backup.name + '.manifest.json').write_text('{}')
    legacy_snapshots = data_dir / 'db_snapshots'
    legacy_snapshots.mkdir()
    legacy_snapshot = legacy_snapshots / 'tofu-legacy.sqlite3'
    legacy_snapshot.write_bytes(b'legacy snapshot')
    pg_backups = data_dir / 'pg_backups'
    pg_backups.mkdir()
    pg_dump = pg_backups / 'pg_dumpall.sql'
    pg_dump.write_bytes(b'postgres dump')
    retired_migration = data_dir / 'retired_migration_artifacts-20260808'
    retired_migration.mkdir()
    retired_database = retired_migration / 'tofu.db.pg-migration'
    retired_database.write_bytes(b'retired migration')

    recovery = module.analyze(project_root)['recovery_artifacts']

    assert recovery['deep_clean_rollbacks']['artifacts'][0]['name'] \
        == rollback.name
    assert '--retire-rollback ' + rollback.name \
        in recovery['deep_clean_rollbacks']['artifacts'][0]['retire_command']
    assert recovery['verified_sqlite_backups']['artifacts'][0]['name'] \
        == backup.name
    assert recovery['operator_managed_large_files']['artifacts'] == [{
        'name': operator_file.name,
        'path': str(operator_file),
        'logical_bytes': len(b'postgres rollback'),
        'allocated_bytes': operator_file.stat().st_blocks * 512,
        'modified_at_unix_s': round(operator_file.stat().st_mtime, 3),
        'lifecycle': 'owner_signoff_required',
    }]
    directories = {
        row['name']: row
        for row in recovery['operator_managed_directories']['directories']
    }
    assert set(directories) == {
        'db_snapshots', 'pg_backups',
        'retired_migration_artifacts-20260808',
    }
    assert directories['db_snapshots']['lifecycle'] \
        == 'retired_sqlite_backup_owner_review'
    assert directories['db_snapshots']['artifacts'][0]['name'] \
        == legacy_snapshot.name
    assert directories['pg_backups']['lifecycle'] \
        == 'postgres_backup_owner_review'
    assert directories['pg_backups']['artifacts'][0]['name'] == pg_dump.name
    assert directories['retired_migration_artifacts-20260808']['lifecycle'] \
        == 'retired_migration_owner_review'
    assert directories['retired_migration_artifacts-20260808'][
        'artifacts'][0]['name'] == retired_database.name
    assert recovery['operator_managed_directories']['total_logical_bytes'] == (
        len(b'legacy snapshot')
        + len(b'postgres dump')
        + len(b'retired migration'))
    assert recovery['total_allocated_bytes'] > 0


def test_operator_managed_directory_inventory_is_bounded_and_ignores_links(
        tmp_path):
    module = _load_module()
    data_dir = tmp_path / 'data'
    pg_backups = data_dir / 'pg_backups'
    pg_backups.mkdir(parents=True)
    first = pg_backups / 'first.sql'
    second = pg_backups / 'second.sql'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    outside = tmp_path / 'outside.sql'
    outside.write_bytes(b'outside')
    (pg_backups / 'linked.sql').symlink_to(outside)
    module._OPERATOR_MANAGED_DIRECTORY_ENTRY_SCAN_LIMIT = 1

    bounded = module._operator_managed_directory_inventory(data_dir)

    directory = bounded['directories'][0]
    assert directory['name'] == 'pg_backups'
    assert directory['scanned_entries'] == 1
    assert directory['capped'] is True
    assert directory['count'] <= 1
    module._OPERATOR_MANAGED_DIRECTORY_ENTRY_SCAN_LIMIT = 10
    complete = module._operator_managed_directory_inventory(data_dir)
    directory = complete['directories'][0]
    assert directory['capped'] is False
    assert directory['count'] == 2
    assert {row['name'] for row in directory['artifacts']} == {
        first.name, second.name}
    assert outside.read_bytes() == b'outside'


def test_retire_rollback_requires_exact_artifact_and_preserves_live(authority):
    module = _load_module()
    project_root, path = authority
    compact = module.offline_compact(project_root, ttl_days=1.0)
    retained = Path(compact['retained'])

    with pytest.raises(ValueError, match='basename'):
        module.retire_rollback(project_root, '../tofu.db')

    report = module.retire_rollback(project_root, retained.name)

    assert report['ok'] is True
    assert report['retired'] == retained.name
    assert report['reclaimed_logical_bytes'] > 0
    assert not retained.exists()
    assert path.is_file()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute('PRAGMA quick_check').fetchone() == ('ok',)
    finally:
        connection.close()


def test_retire_rollback_refuses_live_authority_hardlink(authority):
    module = _load_module()
    project_root, path = authority
    alias = path.parent / 'tofu.db.pre-compact-20200101T000000Z'
    os.link(path, alias)

    with pytest.raises(RuntimeError, match='aliases the live authority'):
        module.retire_rollback(project_root, alias.name)

    assert path.exists() and alias.exists()


def test_retire_rollback_keeps_recovery_point_when_live_check_fails(
        authority, monkeypatch):
    module = _load_module()
    project_root, _path = authority
    compact = module.offline_compact(project_root, ttl_days=1.0)
    retained = Path(compact['retained'])

    class BadCheck:
        def execute(self, _sql):
            return self

        def fetchone(self):
            return ('injected corruption',)

        def close(self):
            pass

    monkeypatch.setattr(module, '_open_readonly', lambda _path: BadCheck())
    with pytest.raises(RuntimeError, match='quick_check failed'):
        module.retire_rollback(project_root, retained.name)

    assert retained.exists()


def test_legacy_transport_batch_enforces_byte_budget(authority):
    module = _load_module()
    module._LEGACY_TRANSPORT_BATCH_PAYLOAD_BYTES = 10

    assert module._bounded_legacy_transport_batch([(1, 6), (2, 5)]) \
        == ([1], 6)
    with pytest.raises(RuntimeError, match='exceeds.*byte budget'):
        module._bounded_legacy_transport_batch([(1, 11)])


def test_legacy_conversation_mirrors_retire_only_three_way_parity(tmp_path):
    module = _load_module()
    module._LEGACY_CONVERSATION_MIRROR_DOCUMENT_BYTES = 128
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    path = data_dir / 'tofu.db'
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute(
        'CREATE TABLE storage_conversations ('
        'id TEXT NOT NULL, user_id INTEGER NOT NULL, '
        'messages_json TEXT NOT NULL, PRIMARY KEY(id,user_id))')
    _create_legacy_conversation_mirror_schema(connection)
    matching = [{'role': 'user', 'content': 'same'}]
    _insert_legacy_conversation(
        connection, 'matching', matching, current_messages=matching)
    _insert_legacy_conversation(
        connection, 'mirror-mismatch', matching, current_messages=matching,
        mirror_messages=[{'role': 'user', 'content': 'row-only'}])
    _insert_legacy_conversation(
        connection, 'archive-mismatch', matching,
        current_messages=[{'role': 'user', 'content': 'current'}])
    _insert_legacy_conversation(
        connection, 'missing-current', matching)
    _insert_legacy_conversation(
        connection, 'has-turn', matching, current_messages=matching,
        with_turn=True)
    oversized = [{'role': 'user', 'content': 'x' * 256}]
    _insert_legacy_conversation(
        connection, 'oversized', oversized, current_messages=oversized)
    connection.close()

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='legacy mirror retirement test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            report = module._retire_legacy_conversation_mirrors(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert report['deleted_conversations'] == 1
    assert report['deleted_message_rows'] == 1
    assert report['verified_conversations'] == 1
    assert report['checked_conversations'] == 3
    assert report['semantic_mismatches'] == 1
    assert report['mirror_mismatches'] == 1
    assert report['oversize_documents'] == 1
    assert report['missing_current_authority'] == 1
    assert report['legacy_turn_conversations'] == 1
    assert report['batches'] == 1
    assert report['retained_conversations'] == 5
    assert report['retained_message_rows'] == 5
    assert report['max_batch_payload_bytes'] \
        <= report['batch_payload_budget_bytes']
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT id FROM conversations ORDER BY id').fetchall() == [
                ('archive-mismatch',),
                ('has-turn',),
                ('mirror-mismatch',),
                ('missing-current',),
                ('oversized',),
            ]
        assert connection.execute(
            'SELECT conv_id FROM conversation_messages '
            'ORDER BY conv_id').fetchall() == [
                ('archive-mismatch',),
                ('has-turn',),
                ('mirror-mismatch',),
                ('missing-current',),
                ('oversized',),
            ]
    finally:
        connection.close()


def test_legacy_mirror_digest_hydrates_a_compressed_current_archive():
    module = _load_module()
    from lib.storage_sidecar.archived_message_codec import (
        encode_archived_message_sequence_for_storage,
    )

    messages = [{
        'role': 'assistant',
        'content': 'frozen durable result ' * 20_000,
    }]
    plain = module.orjson.dumps(messages, option=module.orjson.OPT_SORT_KEYS)
    stored = module.orjson.dumps(
        encode_archived_message_sequence_for_storage(messages),
        option=module.orjson.OPT_SORT_KEYS,
    )

    assert len(stored) < len(plain)
    assert module._canonical_message_document_digest(stored) \
        == module._canonical_message_document_digest(plain)


def test_offline_compact_can_retire_verified_legacy_mirrors(authority):
    module = _load_module()
    project_root, path = authority
    connection = sqlite3.connect(path)
    _create_legacy_conversation_mirror_schema(connection)
    messages = [{'role': 'user', 'content': 'durable current authority'}]
    connection.execute(
        'UPDATE storage_conversations SET messages_json=? WHERE id=?',
        (json.dumps(messages, separators=(',', ':')), 'c1'))
    connection.execute(
        'INSERT INTO conversations VALUES (?,?,?)',
        ('c1', 1, json.dumps(messages, indent=2)))
    connection.execute(
        'INSERT INTO conversation_messages('
        'conv_id,seq,meta,translation_state) VALUES (?,?,?,?)',
        ('c1', 0, json.dumps(messages[0]), '{"v":1}'))
    connection.commit()
    connection.close()

    analysis = module.analyze(project_root)
    mirror_plan = analysis['compaction_plan'][
        'legacy_conversation_mirror_retirement']
    assert mirror_plan['available'] is True
    assert mirror_plan['measurement_complete'] is True
    assert mirror_plan['measured_payload_bytes'] > 0
    assert '--retire-legacy-conversation-mirrors' in mirror_plan['command']

    report = module.offline_compact(
        project_root,
        ttl_days=1.0,
        retire_legacy_conversation_mirrors=True,
    )

    mirrors = report['legacy_conversation_mirrors']
    assert mirrors['deleted_conversations'] == 1
    assert mirrors['deleted_message_rows'] == 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT count(*) FROM conversations').fetchone() == (0,)
        assert connection.execute(
            'SELECT count(*) FROM conversation_messages').fetchone() == (0,)
        assert json.loads(connection.execute(
            'SELECT messages_json FROM storage_conversations WHERE id=?',
            ('c1',),
        ).fetchone()[0]) == messages
    finally:
        connection.close()


def test_offline_compact_interns_frozen_archives_idempotently(authority):
    module = _load_module()
    project_root, path = authority
    repeated = 'archived result ' * 20_000
    projection = {
        'role': 'assistant',
        'content': 'done',
        'segments': [{
            'type': 'tool_use',
            'id': 'call-archive',
            'input': {'path': 'large.txt'},
            'result': {'content': repeated, 'isError': False},
        }],
        'toolRounds': [{
            'toolCallId': 'call-archive',
            'toolArgs': {'path': 'large.txt'},
            'toolContent': repeated,
        }],
    }
    archived = [projection]
    plain = json.dumps(
        archived, separators=(',', ':'), sort_keys=True).encode()
    connection = sqlite3.connect(path)
    search_text = 'derived search duplicate ' * 10_000
    connection.execute(
        'UPDATE storage_conversations '
        'SET messages_json=?,msg_count=1,search_text=? WHERE id=?',
        (plain, search_text, 'c1'),
    )
    connection.commit()
    connection.close()

    report = module.offline_compact(project_root, ttl_days=1.0)

    maintenance = report['archived_conversation_maintenance']
    assert maintenance['updated_rows'] == 1
    assert maintenance['projection_encoded_messages'] == 1
    assert maintenance['compressed_messages'] == 1
    assert maintenance['compression_saved_bytes'] > len(repeated) * 0.99
    assert maintenance['cleared_search_text_rows'] == 1
    assert maintenance['cleared_search_text_bytes'] == len(search_text)
    assert maintenance['retained_search_text_rows'] == 0
    assert maintenance['message_saved_bytes'] > len(repeated) * 0.99
    assert maintenance['saved_bytes'] > len(repeated) * 0.99
    assert maintenance['max_page_payload_bytes'] \
        <= maintenance['page_payload_budget_bytes']
    connection = sqlite3.connect(path)
    stored = connection.execute(
        'SELECT messages_json,search_text '
        'FROM storage_conversations WHERE id=?',
        ('c1',),
    ).fetchone()
    connection.close()
    from lib.storage_sidecar.operations_pkg._conversations import (
        _archived_conversation_messages,
    )
    assert _archived_conversation_messages(stored[0]) == archived
    assert len(stored[0]) < len(plain) * 0.6
    assert stored[1] == ''

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        path.parent,
        owner_kind='offline_maintenance',
        owner_label='archive codec idempotency test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            second = module._maintain_archived_conversation_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert second['updated_rows'] == 0
    assert second['already_encoded_rows'] == 1
    assert second['unchanged_rows'] == 1
    assert second['cleared_search_text_rows'] == 0
    assert second['retained_search_text_rows'] == 0
    assert second['write_batches'] == 0


def _turn_projection_for_codec_test(call_id='codec-call'):
    repeated = 'historical durable tool result ' * 8_000
    tool_args = {'query': 'bounded exact projection ' * 2_000}
    return {
        'content': 'done',
        'thinking': '',
        'segments': [{
            'type': 'tool_use',
            'id': call_id,
            'input': tool_args,
            'result': {'content': repeated, 'status': 'ok'},
        }],
        'toolRounds': [{
            'toolCallId': call_id,
            'toolArgs': tool_args,
            'toolContent': repeated,
            'status': 'ok',
        }],
    }


def _create_turn_projection_maintenance_db(
        tmp_path, rows, *, chain_columns=True):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    path = data_dir / 'tofu.db'
    connection = sqlite3.connect(path)
    chain_schema = (
        ',projection_checkpoint_revision INTEGER,'
        'projection_materialized_revision INTEGER,'
        'projection_patch_count INTEGER NOT NULL DEFAULT 0,'
        'projection_patch_bytes INTEGER NOT NULL DEFAULT 0'
        if chain_columns else ''
    )
    connection.execute(
        'CREATE TABLE storage_conversation_turns ('
        'turn_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,'
        'user_id INTEGER NOT NULL,projection_revision INTEGER NOT NULL,'
        f'projection_json BLOB NOT NULL{chain_schema})'
    )
    columns = (
        'turn_id,conversation_id,user_id,projection_revision,projection_json,'
        'projection_checkpoint_revision,projection_materialized_revision,'
        'projection_patch_count,projection_patch_bytes'
        if chain_columns else
        'turn_id,conversation_id,user_id,projection_revision,projection_json'
    )
    placeholders = ','.join('?' for _ in rows[0])
    connection.executemany(
        f'INSERT INTO storage_conversation_turns({columns}) '
        f'VALUES ({placeholders})',
        rows,
    )
    connection.commit()
    connection.close()
    return data_dir, path


def _run_turn_projection_maintenance(module, data_dir, path, *, trace=None):
    from lib.storage_sidecar.preflight import ProjectLease

    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='Turn projection codec test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        if trace is not None:
            connection.set_trace_callback(trace.append)
        try:
            return module._maintain_turn_projection_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()


def test_turn_projection_maintenance_is_lossless_idempotent_and_chain_safe(
        tmp_path):
    module = _load_module()
    public = _turn_projection_for_codec_test()
    plain = json.dumps(
        public, separators=(',', ':'), sort_keys=True).encode()
    invalid = b'{' + (b'x' * module._TURN_PROJECTION_CODEC_MIN_BYTES)
    data_dir, path = _create_turn_projection_maintenance_db(
        tmp_path,
        [
            ('compact', 'c1', 1, 7, plain, None, None, 0, 0),
            ('invalid', 'c1', 1, 3, invalid, None, None, 0, 0),
            ('live-chain', 'c1', 1, 9, plain, 9, None, 0, 0),
        ],
    )

    report = _run_turn_projection_maintenance(
        module, data_dir, path)

    assert report['mode'] == 'lossless_existing_turn_projection_codec'
    assert report['scanned_rows'] == 2
    assert report['updated_rows'] == 1
    assert report['invalid_rows'] == 1
    assert report['already_encoded_rows'] == 0
    assert report['saved_bytes'] > len(plain) * 0.4
    assert report['chain_guard_columns'] == [
        'projection_checkpoint_revision',
        'projection_materialized_revision',
        'projection_patch_count',
        'projection_patch_bytes',
    ]
    connection = sqlite3.connect(path)
    try:
        compact, live_chain, retained_invalid = connection.execute(
            'SELECT '
            '(SELECT CAST(projection_json AS BLOB) '
            ' FROM storage_conversation_turns WHERE turn_id=\'compact\'),'
            '(SELECT CAST(projection_json AS BLOB) '
            ' FROM storage_conversation_turns WHERE turn_id=\'live-chain\'),'
            '(SELECT CAST(projection_json AS BLOB) '
            ' FROM storage_conversation_turns WHERE turn_id=\'invalid\')'
        ).fetchone()
    finally:
        connection.close()
    from lib.storage_sidecar.projection_codec import (
        STORAGE_PROJECTION_CODEC_KEY,
        decode_projection_from_storage,
    )
    import orjson

    stored = orjson.loads(compact)
    assert STORAGE_PROJECTION_CODEC_KEY in stored
    assert decode_projection_from_storage(stored) == public
    assert live_chain == plain
    assert retained_invalid == invalid

    second = _run_turn_projection_maintenance(
        module, data_dir, path)
    assert second['scanned_rows'] == 2
    assert second['already_encoded_rows'] == 1
    assert second['unchanged_rows'] == 1
    assert second['invalid_rows'] == 1
    assert second['updated_rows'] == 0
    assert second['write_batches'] == 0


def test_turn_projection_maintenance_never_materializes_oversize_row(
        tmp_path):
    module = _load_module()
    public = _turn_projection_for_codec_test()
    plain = json.dumps(
        public, separators=(',', ':'), sort_keys=True).encode()
    module._TURN_PROJECTION_DOCUMENT_BYTES = len(plain) - 1
    data_dir, path = _create_turn_projection_maintenance_db(
        tmp_path,
        [('oversize', 'c1', 1, 1, plain)],
        chain_columns=False,
    )
    statements = []

    report = _run_turn_projection_maintenance(
        module, data_dir, path, trace=statements)

    assert report['oversize_rows'] == 1
    assert report['scanned_rows'] == 0
    assert report['updated_rows'] == 0
    assert not any(
        'SELECT CAST(projection_json AS BLOB)' in statement
        for statement in statements
    )


def test_turn_projection_maintenance_splits_source_payload_budget(tmp_path):
    module = _load_module()
    public = _turn_projection_for_codec_test()
    plain = json.dumps(
        public, separators=(',', ':'), sort_keys=True).encode()
    module._TURN_PROJECTION_PAGE_PAYLOAD_BYTES = len(plain)
    module._TURN_PROJECTION_DOCUMENT_BYTES = len(plain)
    data_dir, path = _create_turn_projection_maintenance_db(
        tmp_path,
        [
            (f'turn-{index}', 'c1', 1, 1, plain)
            for index in range(3)
        ],
        chain_columns=False,
    )

    report = _run_turn_projection_maintenance(
        module, data_dir, path)

    assert report['scanned_rows'] == 3
    assert report['updated_rows'] == 3
    assert report['write_batches'] == 3
    assert report['max_page_payload_bytes'] == len(plain)
    assert report['max_page_payload_bytes'] \
        <= report['page_payload_budget_bytes']


def test_archive_maintenance_clears_search_without_reencoding_messages(
        authority):
    module = _load_module()
    project_root, path = authority
    search_text = 'stale rebuildable projection'
    connection = sqlite3.connect(path)
    original = connection.execute(
        'SELECT CAST(messages_json AS BLOB) '
        'FROM storage_conversations WHERE id=?', ('c1',)
    ).fetchone()[0]
    connection.execute(
        'UPDATE storage_conversations SET search_text=? WHERE id=?',
        (search_text, 'c1'),
    )
    connection.commit()
    connection.close()

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        project_root / 'data',
        owner_kind='offline_maintenance',
        owner_label='search projection retirement test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            report = module._maintain_archived_conversation_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert report['updated_rows'] == 1
    assert report['compacted_message_rows'] == 0
    assert report['message_saved_bytes'] == 0
    assert report['cleared_search_text_rows'] == 1
    assert report['cleared_search_text_bytes'] == len(search_text)
    assert report['saved_bytes'] == len(search_text)
    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            'SELECT CAST(messages_json AS BLOB),search_text '
            'FROM storage_conversations WHERE id=?', ('c1',)
        ).fetchone()
        assert stored == (original, '')
    finally:
        connection.close()


def test_archive_maintenance_preserves_search_when_transcript_is_invalid(
        authority):
    module = _load_module()
    project_root, path = authority
    invalid = b'not-json'
    search_text = 'last searchable recovery witness'
    connection = sqlite3.connect(path)
    connection.execute(
        'UPDATE storage_conversations '
        'SET messages_json=?,msg_count=1,search_text=? WHERE id=?',
        (invalid, search_text, 'c1'),
    )
    connection.commit()
    connection.close()

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        project_root / 'data',
        owner_kind='offline_maintenance',
        owner_label='invalid archive search preservation test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            report = module._maintain_archived_conversation_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert report['invalid_rows'] == 1
    assert report['updated_rows'] == 0
    assert report['cleared_search_text_rows'] == 0
    assert report['retained_search_text_rows'] == 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT CAST(messages_json AS BLOB),search_text '
            'FROM storage_conversations WHERE id=?', ('c1',)
        ).fetchone() == (invalid, search_text)
    finally:
        connection.close()


def test_archive_maintenance_never_materializes_oversize_document(
        tmp_path):
    module = _load_module()
    module._ARCHIVED_CONVERSATION_DOCUMENT_BYTES = 10
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    path = data_dir / 'tofu.db'
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute(
        'CREATE TABLE storage_conversations ('
        'id TEXT NOT NULL, user_id INTEGER NOT NULL, '
        'messages_json BLOB NOT NULL, msg_count INTEGER NOT NULL, '
        'search_text TEXT NOT NULL DEFAULT \'\', '
        'PRIMARY KEY(id,user_id))')
    original = b'[{"role":"user","content":"too large"}]'
    connection.execute(
        'INSERT INTO storage_conversations('
        'id,user_id,messages_json,msg_count) VALUES (?,?,?,?)',
        ('oversize', 1, original, 1),
    )
    connection.close()

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='archive oversize test',
    )
    lease.acquire()
    statements = []
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        connection.set_trace_callback(statements.append)
        try:
            report = module._maintain_archived_conversation_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert report['oversize_rows'] == 1
    assert report['scanned_rows'] == 0
    assert report['updated_rows'] == 0
    assert not any(
        'SELECT CAST(messages_json AS BLOB)' in statement
        for statement in statements
    )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            'SELECT messages_json FROM storage_conversations'
        ).fetchone()[0] == original
    finally:
        connection.close()


def test_archive_maintenance_splits_source_payload_budget(authority):
    module = _load_module()
    project_root, path = authority
    repeated = 'page bounded result ' * 2_000
    projection = {
        'role': 'assistant',
        'content': 'done',
        'segments': [{
            'type': 'tool_use',
            'id': 'page-call',
            'result': {'content': repeated},
        }],
        'toolRounds': [{
            'toolCallId': 'page-call',
            'toolContent': repeated,
        }],
    }
    plain = json.dumps(
        [projection], separators=(',', ':'), sort_keys=True).encode()
    module._ARCHIVED_CONVERSATION_PAGE_PAYLOAD_BYTES = len(plain)
    module._ARCHIVED_CONVERSATION_DOCUMENT_BYTES = len(plain)

    connection = sqlite3.connect(path)
    connection.execute(
        'UPDATE storage_conversations SET messages_json=?,msg_count=1 '
        'WHERE id=?',
        (plain, 'c1'),
    )
    connection.executemany(
        'INSERT INTO storage_conversations('
        'id,user_id,messages_json,msg_count) VALUES (?,?,?,?)',
        [('c2', 1, plain, 1), ('c3', 1, plain, 1)],
    )
    connection.commit()
    connection.close()

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        project_root / 'data',
        owner_kind='offline_maintenance',
        owner_label='archive page budget test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            report = module._maintain_archived_conversation_rows(
                connection, db_path=path, lease=lease)
        finally:
            connection.close()
    finally:
        lease.release()

    assert report['scanned_rows'] == 3
    assert report['updated_rows'] == 3
    assert report['write_batches'] == 3
    assert report['max_page_payload_bytes'] == len(plain)
    assert report['max_page_payload_bytes'] \
        <= report['page_payload_budget_bytes']


def test_legacy_mirror_retirement_fails_closed_on_ambiguous_global_id(
        tmp_path):
    module = _load_module()
    path = tmp_path / 'tofu.db'
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE storage_conversations ('
        'id TEXT NOT NULL, user_id INTEGER NOT NULL, '
        'messages_json TEXT NOT NULL, PRIMARY KEY(id,user_id))')
    _create_legacy_conversation_mirror_schema(connection)
    connection.executemany(
        'INSERT INTO conversations VALUES (?,?,?)',
        [('shared', 1, '[]'), ('shared', 2, '[]')],
    )
    connection.executemany(
        'INSERT INTO storage_conversations VALUES (?,?,?)',
        [('shared', 1, '[]'), ('shared', 2, '[]')],
    )
    connection.commit()

    report = module._retire_legacy_conversation_mirrors(
        connection, db_path=path, lease=None)

    assert report['mode'] == 'ambiguous_legacy_conversation_ids'
    assert report['ambiguous_global_ids'] == 1
    assert report['deleted_conversations'] == 0
    assert connection.execute(
        'SELECT count(*) FROM conversations').fetchone() == (2,)
    connection.close()


def test_legacy_mirror_retirement_requires_physical_reclaim(authority):
    module = _load_module()
    project_root, _path = authority

    with pytest.raises(RuntimeError, match='requires physical reclaim'):
        module.offline_compact(
            project_root,
            ttl_days=1.0,
            vacuum=False,
            retire_legacy_conversation_mirrors=True,
        )


def test_task_event_maintenance_projects_usage_with_rowid_keyset_and_is_idempotent(
        authority):
    module = _load_module()
    project_root, path = authority

    typed_event = json.dumps({
        'type': 'round_usage',
        'usage': {
            'trace_id': 'typed-trace',
            '_future_public_field': 'typed-keep',
            '_wire_fp': [{'content': 'x' * 12_000}],
            '_wire_bytes': list(range(1_000)),
        },
    }, separators=(',', ':'))
    blank_event = json.dumps({
        'type': 'round_usage',
        'usage': {
            'trace_id': 'blank-trace',
            '_future_public_field': 'blank-keep',
            '_wire_field_bytes': [
                {'messages': index} for index in range(400)
            ],
        },
    }, separators=(',', ':'))
    assert len(typed_event.encode()) < module.TASK_EVENT_COMPRESSION_MIN_BYTES
    assert len(blank_event.encode()) < module.TASK_EVENT_COMPRESSION_MIN_BYTES
    connection = sqlite3.connect(path)
    connection.executemany(
        'INSERT INTO storage_events('
        'task_id,sequence,stream_kind,event_type,event_kind,event_json,'
        'created_at_ms) VALUES (?,?,?,?,?,?,?)',
        [
            ('typed-usage', 1, 'task', 'round_usage', '', typed_event,
             int(time.time() * 1000)),
            ('blank-usage', 1, 'task', '', '', blank_event,
             int(time.time() * 1000)),
            ('invalid-usage', 1, 'task', 'round_usage', '', b'not-json',
             int(time.time() * 1000)),
        ],
    )
    connection.commit()
    connection.close()

    first = module.offline_compact(project_root, ttl_days=1.0)
    assert first['task_event_maintenance']['deleted_rows'] == 5
    maintenance = first['task_event_maintenance']
    assert maintenance['usage_projection_candidates'] == 3
    assert maintenance['usage_projected_rows'] == 2
    assert maintenance['invalid_usage_rows'] == 1
    assert maintenance['non_object_usage_rows'] == 0
    assert maintenance['usage_projection_input_bytes'] == (
        len(typed_event.encode()) + len(blank_event.encode()))
    assert maintenance['usage_projection_output_bytes'] \
        <= maintenance['usage_projection_input_bytes'] * 0.05
    assert maintenance['usage_projection_removed_bytes'] == (
        maintenance['usage_projection_input_bytes']
        - maintenance['usage_projection_output_bytes'])

    connection = sqlite3.connect(path)
    try:
        plans = [
            str(row[3])
            for row in connection.execute(
                'EXPLAIN QUERY PLAN '
                + module._TASK_EVENT_MAINTENANCE_PAGE_SQL,
                (0, 'task', module._TASK_EVENT_MAINTENANCE_SELECT_ROWS),
            )
        ]
        retained_usage = connection.execute(
            'SELECT event_type,event_json FROM storage_events '
            "WHERE task_id IN ('typed-usage','blank-usage') "
            'ORDER BY task_id',
        ).fetchall()
        invalid_usage = connection.execute(
            "SELECT event_json FROM storage_events WHERE task_id='invalid-usage'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert any('INTEGER PRIMARY KEY (rowid>?)' in plan for plan in plans)
    assert all('TEMP B-TREE' not in plan for plan in plans)
    assert [row[0] for row in retained_usage] == ['round_usage', 'round_usage']
    assert invalid_usage == b'not-json'
    for _event_type, stored_payload in retained_usage:
        payload = json.loads(stored_payload)
        assert payload['usage']['trace_id'] in {'blank-trace', 'typed-trace'}
        assert payload['usage']['_future_public_field'] in {
            'blank-keep', 'typed-keep'}
        assert not any(
            key.startswith('_wire_') for key in payload['usage'])

    from lib.storage_sidecar.preflight import ProjectLease
    lease = ProjectLease(
        project_root / 'data',
        owner_kind='offline_maintenance',
        owner_label='idempotency test',
    )
    lease.acquire()
    try:
        connection = module._SQLITE_TOOLING.open_sqlite_tool_connection(
            path, writable=True, lease=lease)
        try:
            second = module._maintain_task_event_rows(
                connection,
                now_ms=int(time.time() * 1000),
                db_path=path,
                lease=lease,
            )
        finally:
            connection.close()
    finally:
        lease.release()

    assert second['deleted_rows'] == 0
    assert second['updated_rows'] == 0
    assert second['compressed_rows'] == 0
    assert second['already_compressed_rows'] == 0
    assert second['usage_projection_candidates'] == 3
    assert second['usage_projected_rows'] == 0
    assert second['invalid_usage_rows'] == 1
    assert second['usage_projection_removed_bytes'] == 0
    assert second['write_batches'] == 0

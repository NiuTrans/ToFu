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
        'CREATE TABLE storage_conversations ('
        'id TEXT PRIMARY KEY, user_id BIGINT NOT NULL DEFAULT 1, '
        'rev BIGINT, updated_at_ms BIGINT NOT NULL DEFAULT 0, '
        'title TEXT NOT NULL DEFAULT \'\', '
        'created_at_ms BIGINT NOT NULL DEFAULT 0, '
        'settings_json BLOB NOT NULL DEFAULT \'{}\', '
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


def test_analyze_table_scan_stops_at_one_shared_deadline(authority):
    module = _load_module()
    project_root, _path = authority
    module._ANALYZE_SQL_BUDGET_SECONDS = 0.0

    report = module.analyze(project_root)

    assert report['table_scan']['timed_out'] is True
    assert report['table_scan']['completed_tables'] == 0
    assert report['tables']
    assert all(
        row == {
            'error': 'analysis_budget_exhausted',
            'measurement': 'not_completed',
        }
        for row in report['tables'].values()
    )


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
    assert recovery['total_allocated_bytes'] > 0


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


def test_task_event_maintenance_uses_rowid_keyset_and_is_idempotent(authority):
    module = _load_module()
    project_root, path = authority

    first = module.offline_compact(project_root, ttl_days=1.0)
    assert first['task_event_maintenance']['deleted_rows'] == 5

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
    finally:
        connection.close()
    assert any('INTEGER PRIMARY KEY (rowid>?)' in plan for plan in plans)
    assert all('TEMP B-TREE' not in plan for plan in plans)

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
    assert second['write_batches'] == 0

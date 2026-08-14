import os
import sqlite3

import pytest

from lib.database import backup as backup_mod

pytestmark = pytest.mark.unit


def _source(path, value='precious'):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE durable_data (id INTEGER PRIMARY KEY, value TEXT)')
    conn.execute('INSERT INTO durable_data(value) VALUES (?)', (value,))
    conn.commit()
    conn.close()


def test_vacuum_snapshot_is_verified_and_directly_reopenable(tmp_path):
    source = tmp_path / 'tofu.db'
    snapshots = tmp_path / 'db_snapshots'
    _source(source)

    result = backup_mod.backup_sqlite_database(
        db_path=str(source), snapshot_dir=str(snapshots), retention_count=2)

    assert result['ok'] is True
    assert result['verified'] is True
    snap = result['path']
    assert os.path.dirname(snap) == str(snapshots)
    with sqlite3.connect(snap) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT value FROM durable_data').fetchone()[0] == 'precious'


def test_snapshot_retention_is_count_based(tmp_path, monkeypatch):
    source = tmp_path / 'tofu.db'
    snapshots = tmp_path / 'db_snapshots'
    _source(source)
    ticks = iter(['20260101_000001', '20260101_000002', '20260101_000003'])
    monkeypatch.setattr(backup_mod.time, 'strftime', lambda _fmt: next(ticks))

    for _ in range(3):
        assert backup_mod.backup_sqlite_database(
            db_path=str(source), snapshot_dir=str(snapshots),
            retention_count=2)['ok']

    kept = list(snapshots.glob('tofu-*.sqlite3'))
    assert len(kept) == 2


def test_default_retention_keeps_two_full_recovery_points(
        tmp_path, monkeypatch):
    source = tmp_path / 'tofu.db'
    snapshots = tmp_path / 'db_snapshots'
    _source(source)
    ticks = iter(['20260102_000001', '20260102_000002', '20260102_000003'])
    monkeypatch.setattr(backup_mod.time, 'strftime', lambda _fmt: next(ticks))
    monkeypatch.delenv('TOFU_SQLITE_SNAPSHOT_RETENTION', raising=False)

    for _ in range(3):
        assert backup_mod.backup_sqlite_database(
            db_path=str(source), snapshot_dir=str(snapshots))['ok']

    assert len(list(snapshots.glob('tofu-*.sqlite3'))) == 2


def test_configured_snapshot_directory_can_be_separate_failure_domain(
        tmp_path, monkeypatch):
    source = tmp_path / 'authority' / 'tofu.db'
    source.parent.mkdir()
    external = tmp_path / 'mounted-backups'
    _source(source)
    monkeypatch.setenv('TOFU_SQLITE_SNAPSHOT_DIR', str(external))

    result = backup_mod.backup_sqlite_database(
        db_path=str(source), retention_count=2)

    assert result['ok'] is True
    assert os.path.dirname(result['path']) == str(external)
    assert not (source.parent / 'db_snapshots').exists()


def test_snapshot_lock_refuses_duplicate_expensive_backup(tmp_path):
    source = tmp_path / 'tofu.db'
    snapshots = tmp_path / 'db_snapshots'
    _source(source)
    snapshots.mkdir()
    (snapshots / backup_mod._LOCK_NAME).mkdir()

    result = backup_mod.backup_sqlite_database(
        db_path=str(source), snapshot_dir=str(snapshots), retention_count=2)

    assert result == {'ok': False, 'reason': 'snapshot_in_progress'}


def test_missing_source_fails_without_creating_snapshot_tree(tmp_path):
    target = tmp_path / 'missing.db'
    result = backup_mod.backup_sqlite_database(db_path=str(target))
    assert result['ok'] is False
    assert result['reason'] == 'sqlite_source_missing'


def test_legacy_scheduler_slot_dispatches_backend_neutral_backup(monkeypatch):
    import lib.database as database
    from lib.scheduler.manager import ScheduledTaskManager

    monkeypatch.setattr(database, 'backup_database', lambda: {
        'ok': True, 'path': '/project/data/db_snapshots/tofu.sqlite3',
        'size_mb': 12.5, 'pruned': 1,
    })
    manager = ScheduledTaskManager.__new__(ScheduledTaskManager)
    ok, message = manager._execute_task({
        'task_type': 'pg_backup', 'command': '', 'max_runtime': 10,
    })

    assert ok is True
    assert 'db_snapshots' in message
    assert 'pruned 1' in message

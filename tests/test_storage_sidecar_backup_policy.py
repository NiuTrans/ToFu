"""Behavioral contracts for bounded personal SQLite backup artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar import backup_policy, durability
from lib.storage_sidecar.adapters.sqlite import SQLiteBackend
from lib.storage_sidecar.config import SidecarConfig


pytestmark = pytest.mark.unit


def _config(tmp_path: Path) -> SidecarConfig:
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'
    data_dir.mkdir()
    logs_dir.mkdir()
    return SidecarConfig(
        project_root=tmp_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        backend='sqlite',
        deployment_mode='personal',
        process_role='all',
        replica_id=None,
        token='test-token-' * 4,
        sqlite_path=data_dir / 'tofu.db',
        postgres_dsn='',
        redis_url='',
        allow_schema_migration=True,
        read_pool_size=1,
        write_pool_size=1,
    )


def test_backup_checksum_releases_scanned_pages_from_cache(tmp_path, monkeypatch):
    artifact = tmp_path / 'artifact.sqlite3'
    artifact.write_bytes(b'bounded-backup-content')
    calls = []
    monkeypatch.setattr(durability.os, 'POSIX_FADV_DONTNEED', 4, raising=False)
    monkeypatch.setattr(
        durability.os,
        'posix_fadvise',
        lambda descriptor, offset, length, advice: calls.append(
            (descriptor, offset, length, advice)),
        raising=False,
    )

    checksum = durability.sha256_file(artifact, time.monotonic() + 1)

    assert len(checksum) == 64
    assert len(calls) == 1
    assert calls[0][0] >= 0
    assert calls[0][1:] == (0, 0, 4)


def test_capacity_failure_precedes_temporary_copy(tmp_path, monkeypatch):
    backups = tmp_path / 'backups'
    backups.mkdir()
    monkeypatch.setattr(
        backup_policy.shutil,
        'disk_usage',
        lambda _path: type('Usage', (), {'total': 1, 'free': 0})(),
    )

    with pytest.raises(StorageError) as raised:
        backup_policy.capacity_preflight(backups, 4096)

    assert raised.value.code == 'database_unavailable'
    assert list(backups.iterdir()) == []


def test_capacity_rejects_projected_same_volume_recovery_copy_budget(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / 'data'
    backups = data_dir / 'backups'
    backups.mkdir(parents=True)
    verified = backups / 'storage-sqlite-20260824T000000Z.sqlite3'
    verified.write_bytes(b'verified')
    rollback = data_dir / 'tofu.db.pre-compact-20260824T000000Z'
    rollback.write_bytes(b'rollback')
    retained = (
        backup_policy.verified_backup_inventory(backups)[
            'total_allocated_bytes']
        + backup_policy.rollback_artifact_inventory(data_dir)[
            'total_allocated_bytes']
    )
    estimate = 4096
    monkeypatch.setattr(
        backup_policy,
        'recovery_copy_budget_bytes',
        lambda: retained + estimate - 1,
        raising=False,
    )

    with pytest.raises(StorageError, match='recovery-copy budget') as raised:
        backup_policy.capacity_preflight(backups, estimate)

    assert raised.value.retryable is False
    assert verified.read_bytes() == b'verified'
    assert rollback.read_bytes() == b'rollback'


def test_external_backup_mount_does_not_charge_local_rollback(
    tmp_path, monkeypatch,
):
    data_dir = tmp_path / 'data'
    backups = data_dir / 'backups'
    backups.mkdir(parents=True)
    rollback = data_dir / 'tofu.db.pre-compact-20260824T000000Z'
    rollback.write_bytes(b'rollback')
    original_stat = Path.stat

    class ExternalMountStat:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.st_dev = wrapped.st_dev + 1

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        return ExternalMountStat(result) if path == backups else result

    monkeypatch.setattr(Path, 'stat', stat)

    footprint = backup_policy.recovery_copy_footprint(backups)

    assert footprint['rollback_same_volume'] is False
    assert footprint['same_volume_rollback_bytes'] == 0
    assert footprint['retained_recovery_bytes'] == 0


def test_stale_jobs_reclaim_only_expired_dead_owner_artifacts(
    tmp_path, monkeypatch,
):
    backups = tmp_path / 'backups'
    backups.mkdir()
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_BACKUP_TEMP_TTL_SECONDS', '1')
    old = time.time() - 10

    dead = backups / '.storage-sqlite-old.sqlite3.tmp-dead'
    dead.write_bytes(b'partial')
    dead_manifest = backup_policy.job_manifest_path(dead)
    dead_manifest.write_text(
        json.dumps({'pid': 999_999_999, 'temporary_path': str(dead)}),
        encoding='utf-8',
    )
    active = backups / '.storage-sqlite-live.sqlite3.tmp-live'
    active.write_bytes(b'partial')
    active_manifest = backup_policy.job_manifest_path(active)
    active_manifest.write_text(
        json.dumps({'pid': os.getpid(), 'temporary_path': str(active)}),
        encoding='utf-8',
    )
    for path in (dead, dead_manifest, active, active_manifest):
        os.utime(path, (old, old))

    assert backup_policy.reclaim_stale_job_artifacts(backups) == 2
    assert not dead.exists()
    assert not dead_manifest.exists()
    assert active.exists()
    assert active_manifest.exists()


def test_retention_keeps_two_verified_artifact_manifest_pairs(
    tmp_path, monkeypatch,
):
    backups = tmp_path / 'backups'
    backups.mkdir()
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_BACKUP_RETENTION', '2')
    artifacts = []
    for index in range(3):
        artifact = backups / f'storage-sqlite-20260824T00000{index}Z.sqlite3'
        artifact.write_bytes(f'backup-{index}'.encode())
        manifest = artifact.with_name(artifact.name + '.manifest.json')
        manifest.write_text('{}', encoding='utf-8')
        os.utime(artifact, (100 + index, 100 + index))
        artifacts.append(artifact)

    assert backup_policy.prune_verified_backups(
        backups, preserve=artifacts[-1]) == 1
    assert not artifacts[0].exists()
    assert not artifacts[0].with_name(
        artifacts[0].name + '.manifest.json').exists()
    assert all(path.exists() for path in artifacts[1:])
    assert all(
        path.with_name(path.name + '.manifest.json').exists()
        for path in artifacts[1:]
    )
    inventory = backup_policy.verified_backup_inventory(backups)
    assert inventory['count'] == 2
    assert inventory['excess_count'] == 0
    assert all(row['manifest_present'] for row in inventory['artifacts'])
    assert inventory['total_logical_bytes'] == sum(
        artifact.stat().st_size for artifact in artifacts[1:])


def test_deep_clean_rollback_retention_counts_published_point_in_bound(
        tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION', '1')
    artifacts = []
    for index in range(3):
        artifact = (
            data_dir / f'tofu.db.pre-compact-20260824T00000{index}Z')
        artifact.write_bytes(f'rollback-{index}'.encode())
        os.utime(artifact, (100 + index, 100 + index))
        artifacts.append(artifact)

    report = backup_policy.prune_retained_rollbacks(
        data_dir, preserve=artifacts[0])

    assert report == {
        'retention_count': 1,
        'removed': [artifacts[2].name, artifacts[1].name],
        'errors': [],
    }
    assert artifacts[0].exists()
    assert all(not artifact.exists() for artifact in artifacts[1:])
    inventory = backup_policy.rollback_artifact_inventory(data_dir)
    assert inventory['count'] == 1
    assert inventory['excess_count'] == 0
    assert inventory['total_logical_bytes'] == len(b'rollback-0')
    assert inventory['artifacts'][0]['name'] == artifacts[0].name


def test_deep_clean_rollback_policy_refuses_unsafe_target_and_caps_override(
        tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    safe = data_dir / 'tofu.db.pre-compact-20260824T000000Z'
    safe.write_bytes(b'rollback')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside')
    symlink = data_dir / 'tofu.db.pre-compact-20260824T000001Z'
    symlink.symlink_to(outside)

    assert backup_policy.resolve_rollback_artifact(
        data_dir, safe.name) == safe
    with pytest.raises(ValueError, match='basename'):
        backup_policy.resolve_rollback_artifact(data_dir, '../outside')
    with pytest.raises(ValueError, match='unsafe'):
        backup_policy.resolve_rollback_artifact(data_dir, symlink.name)

    monkeypatch.setenv('TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION', '999')
    assert backup_policy.rollback_retention_count() == 4


def test_backup_failure_releases_read_slot_and_concurrency_lease(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    try:
        def reject_capacity(_backups, _estimated_bytes):
            raise StorageError('database_unavailable', 'no capacity')

        monkeypatch.setattr(
            'lib.storage_sidecar.adapters.sqlite.capacity_preflight',
            reject_capacity,
        )
        with pytest.raises(StorageError, match='no capacity'):
            backend.backup(time.monotonic() + 5)

        assert backend.metrics()['read_pool_size'] == 1
        assert backend._backup_lock.acquire(blocking=False)
        backend._backup_lock.release()
        assert not list((tmp_path / 'data' / 'backups').glob('*.tmp-*'))
    finally:
        backend.close()


def test_fastpath_backup_uses_checkpointed_snapshot_without_read_pool_backup(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    calls = []

    class FakeShipper:
        def pin_checkpointed_snapshot_for_backup(
                self, destination, *, deadline_at):
            calls.append((destination, deadline_at))
            source = sqlite3.connect(backend._authority_path)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            return {
                'generation': 7,
                'bytes': destination.stat().st_size,
                'copy_strategy': 'hardlink',
            }

    backend._shipper = FakeShipper()
    monkeypatch.setattr(
        backend,
        '_acquire_read',
        lambda _deadline: (_ for _ in ()).throw(
            AssertionError('fastpath backup must not use SQLite online backup')),
    )
    try:
        result = backend.backup(time.monotonic() + 10)
        artifact = tmp_path / result['backup']
        manifest = json.loads(
            (tmp_path / result['manifest']).read_text(encoding='utf-8'))

        assert len(calls) == 1
        assert artifact.is_file()
        assert result['source_mode'] == 'fastpath-checkpointed-shadow'
        assert result['snapshot_generation'] == 7
        assert result['copy_strategy'] == 'hardlink'
        assert manifest['source_mode'] == result['source_mode']
        assert manifest['snapshot_generation'] == 7
        assert manifest['sha256'] == result['sha256']
    finally:
        backend._shipper = None
        backend.close()


def test_fastpath_backup_timeout_is_typed_and_cleans_private_artifacts(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()

    class TimedOutShipper:
        def pin_checkpointed_snapshot_for_backup(
                self, destination, *, deadline_at):
            del destination, deadline_at
            raise TimeoutError('snapshot pin timed out')

    backend._shipper = TimedOutShipper()
    try:
        with pytest.raises(StorageError) as raised:
            backend.backup(time.monotonic() + 5)
        assert raised.value.code == 'database_timeout'
        assert raised.value.retryable is True
        backups = tmp_path / 'data' / 'backups'
        assert not list(backups.glob('.storage-sqlite-*'))
        assert backend._backup_lock.acquire(blocking=False)
        backend._backup_lock.release()
    finally:
        backend._shipper = None
        backend.close()


def test_concurrent_full_backup_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    try:
        assert backend._backup_lock.acquire(blocking=False)
        try:
            with pytest.raises(StorageError) as raised:
                backend.backup(time.monotonic() + 5)
        finally:
            backend._backup_lock.release()
        assert raised.value.code == 'database_busy'
        assert raised.value.retryable is True
    finally:
        backend.close()

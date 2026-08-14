"""Cross-host SQLite authority marker contracts.

事故锚点：共享/FUSE SQLite 若由两个主机同时写入，会绕过单进程写者锁并
造成锁风暴、事务互相覆盖或数据库损坏；这些回归测试固定 fail-closed 所有权
协议及其安全接管边界。
"""

from __future__ import annotations

import json
import os
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def owner(monkeypatch):
    from lib.database import sqlite_owner as module
    module.release_owner()
    monkeypatch.setattr(module, 'REFRESH_S', 3600.0)
    monkeypatch.setattr(module, '_claim', None)
    monkeypatch.setattr(module, '_lost_reason', '')
    monkeypatch.setattr(module, '_last_verified_wall', 0.0)
    monkeypatch.setenv('TOFU_SQLITE_OWNER_GUARD', '1')
    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    yield module
    module.release_owner()


def _write_marker(owner, db_path, *, host, ts=None, instance='peer'):
    ts = time.time() if ts is None else ts
    path, _lock = owner._paths(str(db_path))
    owner._atomic_write(path, {
        'version': 1,
        'host': host,
        'updated_at': ts,
        'db': str(db_path.resolve()),
        'members': {instance: {'pid': 1234, 'ts': ts}},
    })
    return path


def test_fresh_other_host_fails_closed(owner, tmp_path, monkeypatch):
    db_path = tmp_path / 'tofu.db'
    marker = _write_marker(owner, db_path, host='host-a')
    monkeypatch.setenv('TOFU_DB_HOST_ID', 'host-b')

    with pytest.raises(owner.SQLiteOwnershipError, match='host-a'):
        owner.claim_owner(str(db_path))
    assert json.loads(marker.read_text())['host'] == 'host-a'


def test_stale_other_host_is_taken_over_atomically(owner, tmp_path, monkeypatch):
    db_path = tmp_path / 'tofu.db'
    old = time.time() - owner.TTL_S - 5
    marker = _write_marker(owner, db_path, host='host-a', ts=old)
    os.utime(marker, (old, old))
    monkeypatch.setenv('TOFU_DB_HOST_ID', 'host-b')

    claim = owner.claim_owner(str(db_path))
    assert claim['host'] == 'host-b'
    assert json.loads(marker.read_text())['host'] == 'host-b'


def test_same_host_processes_share_membership(owner, tmp_path, monkeypatch):
    db_path = tmp_path / 'tofu.db'
    marker = _write_marker(owner, db_path, host='same-host', instance='peer-a')
    monkeypatch.setenv('TOFU_DB_HOST_ID', 'same-host')

    claim = owner.claim_owner(str(db_path))
    assert 'peer-a' in claim['members']
    assert owner._instance_id in claim['members']

    owner.release_owner()
    remaining = json.loads(marker.read_text())
    assert set(remaining['members']) == {'peer-a'}


def test_resumed_old_host_detects_takeover_before_write(
        owner, tmp_path, monkeypatch):
    db_path = tmp_path / 'tofu.db'
    monkeypatch.setenv('TOFU_DB_HOST_ID', 'host-a')
    owner.claim_owner(str(db_path))

    _write_marker(owner, db_path, host='host-b')
    owner._last_verified_wall = 0.0
    with pytest.raises(owner.SQLiteOwnershipError, match='host-b'):
        owner.assert_owner(str(db_path), str(db_path))


def test_guard_scope_covers_canonical_path_and_server_custom_path(
        owner, tmp_path, monkeypatch):
    canonical = tmp_path / 'tofu.db'
    custom = tmp_path / 'migration.db'
    monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)
    assert owner.guard_required(str(canonical), str(canonical)) is True
    assert owner.guard_required(str(custom), str(canonical)) is False
    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    assert owner.guard_required(str(custom), str(canonical)) is True


def test_ambient_non_server_process_cannot_write_canonical_db(
        owner, tmp_path, monkeypatch):
    canonical = tmp_path / 'tofu.db'
    monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)

    with pytest.raises(owner.SQLiteOwnershipError, match='restricted'):
        owner.assert_owner(str(canonical), str(canonical))


def test_explicit_maintenance_scope_expires_on_exit(
        owner, tmp_path, monkeypatch):
    canonical = tmp_path / 'tofu.db'
    monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)

    with owner.maintenance_write_authority('unit maintenance'):
        owner.assert_owner(str(canonical), str(canonical))
    with pytest.raises(owner.SQLiteOwnershipError, match='restricted'):
        owner.assert_owner(str(canonical), str(canonical))

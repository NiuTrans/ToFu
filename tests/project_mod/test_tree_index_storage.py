"""Disk pressure drops only reconstructible blobs, never complete search work."""
import os
from pathlib import Path

import pytest

from lib.project_mod import tree_index, tree_index_storage as storage
from serverctl_pkg.local_placement import Placement, PURPOSE_INDEXES

pytestmark = pytest.mark.unit
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(storage, 'resolve_resource_budget', lambda *a, **k: 1)
    monkeypatch.setattr(storage, 'has_free_space', lambda *a: True)


def blob(root, number, size=1, modified=100):
    path = root / f'{number:020x}.tfix'
    with path.open('wb') as stream:
        stream.truncate(size)
    os.utime(path, (modified, modified))
    return path


def test_private_local_namespaces_separate_installations(tmp_path, monkeypatch):
    root = tmp_path / 'local'
    monkeypatch.setattr(storage, 'index_cache_placement', lambda source: Placement(
        True, PURPOSE_INDEXES, 'auto', 'selected', root=str(root)))
    one = storage.index_directory(str(tmp_path / 'installation-one'))
    two = storage.index_directory(str(tmp_path / 'installation-two'))
    assert one != two
    assert Path(one).is_relative_to(root) and Path(two).is_relative_to(root)
    assert Path(one).stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / 'installation-one').exists()


def test_local_namespace_symlink_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / 'local'
    root.mkdir(mode=0o700)
    target = tmp_path / 'user-data'
    target.mkdir()
    (root / 'project-indexes').symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(storage, 'index_cache_placement', lambda source: Placement(
        True, PURPOSE_INDEXES, 'auto', 'selected', root=str(root)))
    with pytest.raises(PermissionError):
        storage.index_directory(str(tmp_path / 'sessions'))
    assert list(target.iterdir()) == []


def test_lru_eviction_keeps_newest_and_never_touches_unrelated_or_staged_files(tmp_path):
    old = blob(tmp_path, 1, MIB // 2, modified=100)
    recent = blob(tmp_path, 2, MIB // 2, modified=200)
    authority = tmp_path / 'data.wal'
    authority.write_bytes(b'durable')
    staging = tmp_path / ('3' * 20 + '.tfix.tmp.123')
    staging.write_bytes(b'in-progress')
    target = tmp_path / ('4' * 20 + '.tfix')
    assert storage.prepare_write(str(target), MIB // 2, now=300)
    assert not old.exists() and recent.exists()
    assert authority.read_bytes() == b'durable' and staging.read_bytes() == b'in-progress'


def test_replacement_budget_counts_old_blob_once_and_leaves_headroom_for_staging(tmp_path, monkeypatch):
    target = blob(tmp_path, 1, MIB)
    observed = []
    monkeypatch.setattr(storage, 'has_free_space', lambda path, required: observed.append(required) or True)
    assert storage.prepare_write(str(target), MIB, now=200)
    assert target.exists() and observed == [MIB]


def test_legacy_cache_retention_works_without_posix_user_ids(tmp_path, monkeypatch):
    monkeypatch.delattr(os, 'getuid')
    target = blob(tmp_path, 1)
    assert storage.prepare_write(str(target), 20, now=200)


def test_oversized_blob_is_not_persisted_but_does_not_evict_others(tmp_path):
    kept = blob(tmp_path, 1)
    assert not storage.prepare_write(str(tmp_path / ('2' * 20 + '.tfix')), MIB + 1)
    assert kept.exists()


def test_pressure_reclaims_unused_blobs_until_room_returns(tmp_path, monkeypatch):
    old, new = blob(tmp_path, 1), blob(tmp_path, 2, modified=200)
    monkeypatch.setattr(storage, 'has_free_space', lambda *a: not old.exists())
    assert storage.prepare_write(str(tmp_path / ('3' * 20 + '.tfix')), 1, now=300)
    assert not old.exists() and new.exists()


def test_expired_cache_removed_without_touching_symlink_or_hardlink(tmp_path):
    old = blob(tmp_path, 1)
    original = tmp_path / 'durable'
    original.write_bytes(b'preserved')
    alias = tmp_path / ('2' * 20 + '.tfix')
    alias.symlink_to(original)
    hardlink = tmp_path / ('3' * 20 + '.tfix')
    os.link(original, hardlink)
    assert storage.prepare_write(str(tmp_path / ('4' * 20 + '.tfix')), 1, now=8 * 86400)
    assert not old.exists() and alias.is_symlink() and hardlink.exists()
    assert original.read_bytes() == b'preserved'


def test_storage_full_keeps_complete_index_and_write_freshness(tmp_path, monkeypatch):
    source, cache = tmp_path / 'source', tmp_path / 'cache'
    source.mkdir()
    cache.mkdir()
    (source / 'one.txt').write_text('first')
    monkeypatch.setattr(tree_index, '_index_dir', lambda: str(cache))
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(storage, 'has_free_space', lambda *a: False)
    entry = tree_index._build_sync(str(source))
    entry = tree_index.acquire(str(source))
    assert entry is not None and entry.complete and list(entry.paths) == ['one.txt']
    assert not list(cache.iterdir())
    (source / 'two.txt').write_text('second')
    tree_index.note_write(str(source / 'two.txt'))
    assert list(tree_index.acquire(str(source)).paths) == ['one.txt', 'two.txt']


def test_abandoned_staging_is_reclaimed_but_live_or_recent_staging_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, '_process_is_gone', lambda pid: pid == 123)
    old_dead = tmp_path / ('1' * 20 + '.tfix.tmp.123.4.5')
    old_live = tmp_path / ('2' * 20 + '.tfix.tmp.456.4.5')
    recent_dead = tmp_path / ('3' * 20 + '.tfix.tmp.123.4.5')
    for path, modified in ((old_dead, 0), (old_live, 0), (recent_dead, 2 * 86400)):
        path.write_bytes(b'partial')
        os.utime(path, (modified, modified))
    target = tmp_path / ('4' * 20 + '.tfix')
    assert storage.prepare_write(str(target), 1, now=2 * 86400)
    assert not old_dead.exists() and old_live.exists() and recent_dead.exists()

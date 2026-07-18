"""Tests for lib/database/db_paths.py — the local-primary / FUSE-backup split.

Pure unit tests: no PG, no real FUSE mount, no DB import. They pin the two
load-bearing invariants of the Tier-A durability redesign:

  1. VANILLA-BOX NO-OP: when the data root is on local disk, the resolved
     pgdata path is BYTE-IDENTICAL to the legacy ``<data>/pgdata``.
  2. TRIGGER ENGAGES on a network mount (the negative control): forcing the
     mount classification True must flip pgdata to the local root — proving the
     no-op above is the trigger's doing, not an accident of the default.
"""
import importlib
import os

import pytest

import lib.database.db_paths as db_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ('TOFU_DB_LOCAL_SPLIT', 'TOFU_DB_LOCAL_ROOT', 'TOFU_DB_BACKUP_ROOT'):
        monkeypatch.delenv(var, raising=False)
    db_paths._LOGGED_RESOLUTIONS.clear()  # the log-dedup cache is per-process; reset per test
    yield


def test_vanilla_local_disk_is_byte_identical_noop():
    """Local-disk data root → pgdata unchanged from legacy <data>/pgdata."""
    data_dir = '/home/user/tofu/data'
    assert db_paths.local_data_split_enabled(data_dir) is False
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(data_dir, 'pgdata')


def test_nc_network_mount_engages_split():
    """NC: a /mnt/ data root engages the split → pgdata moves to the local root.

    This is the biting negative control for the no-op test above — if the
    trigger were dead, this would still return the legacy FUSE path.
    """
    data_dir = '/mnt/your-fs'
    assert db_paths.local_data_split_enabled(data_dir) is True
    pgdata = db_paths.resolve_pgdata_dir(data_dir)
    assert pgdata == '/tmp/tofu/pgdata'
    assert not pgdata.startswith('/mnt/')


def test_env_force_split_on_local_disk(monkeypatch):
    """TOFU_DB_LOCAL_SPLIT=1 forces the split even on local disk."""
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    data_dir = '/home/user/tofu/data'
    assert db_paths.local_data_split_enabled(data_dir) is True
    assert db_paths.resolve_pgdata_dir(data_dir) == '/tmp/tofu/pgdata'


def test_env_force_split_off_on_network_mount(monkeypatch):
    """TOFU_DB_LOCAL_SPLIT=0 forces legacy behaviour even on a /mnt/ mount."""
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '0')
    data_dir = '/mnt/your-fs'
    assert db_paths.local_data_split_enabled(data_dir) is False
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(data_dir, 'pgdata')


def test_local_root_override(monkeypatch):
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', '/data/local_pg')
    data_dir = '/mnt/your-fs'
    assert db_paths.resolve_pgdata_dir(data_dir) == '/data/local_pg/pgdata'


def test_backup_root_defaults_to_legacy_fuse_location():
    """Backup root defaults to <data>/pg_backups — durable FUSE when split is on."""
    data_dir = '/mnt/your-fs'
    assert db_paths.resolve_backup_root(data_dir) == os.path.join(data_dir, 'pg_backups')


def test_backup_root_env_override(monkeypatch):
    monkeypatch.setenv('TOFU_DB_BACKUP_ROOT', '/mnt/your-fs')
    data_dir = '/home/user/tofu/data'
    assert db_paths.resolve_backup_root(data_dir) == '/mnt/your-fs'


def test_is_network_mount_predicate():
    assert db_paths.is_network_mount('/mnt/your-fs') is True
    assert db_paths.is_network_mount('/home/user/x') is False
    assert db_paths.is_network_mount('/tmp/tofu') is False
    assert db_paths.is_network_mount('') is False


def test_module_reimport_is_stable():
    """Re-importing must not change behaviour (no import-time env capture)."""
    importlib.reload(db_paths)
    assert db_paths.resolve_pgdata_dir('/home/u/data') == '/home/u/data/pgdata'


# ── Ordering-hazard gate (the active data-loss risk) ────────────────────────

def _make_populated_pgdata(root):
    """Create a dir that pgdata_is_populated() accepts."""
    pg = os.path.join(root, 'pgdata')
    os.makedirs(pg, exist_ok=True)
    with open(os.path.join(pg, 'PG_VERSION'), 'w') as f:
        f.write('16\n')
    return pg


def test_gate_empty_local_populated_legacy_stays_on_legacy(tmp_path, monkeypatch):
    """THE hazard: split on, empty local, populated legacy FUSE pgdata →
    resolution MUST stay on the populated legacy cluster (never empty-start)."""
    data_dir = str(tmp_path / 'data')            # legacy data root
    _make_populated_pgdata(data_dir)             # 11G-equivalent live history
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')   # force split on (tmp_path isn't /mnt)
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', str(tmp_path / 'local'))  # empty local

    resolved = db_paths.resolve_pgdata_dir(data_dir)
    assert resolved == os.path.join(data_dir, 'pgdata'), (
        'GATE FAILED — resolved to empty local root, a restart would initdb '
        'empty and lose history')


def test_gate_empty_local_dump_present_stays_on_legacy(tmp_path, monkeypatch):
    """Even with NO legacy cluster, a logical dump on FUSE is recoverable →
    stay on legacy path (the seed will restore the dump) rather than empty-start."""
    data_dir = str(tmp_path / 'data')
    backups = os.path.join(data_dir, 'pg_backups')
    os.makedirs(backups, exist_ok=True)
    with open(os.path.join(backups, 'pg_dumpall_20260702_020016.sql'), 'w') as f:
        f.write('-- dump\n')
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', str(tmp_path / 'local'))
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(data_dir, 'pgdata')


def test_gate_populated_local_uses_local(tmp_path, monkeypatch):
    """Once the seed has POPULATED local, resolution flips to local (the goal)."""
    data_dir = str(tmp_path / 'data')
    _make_populated_pgdata(data_dir)             # legacy still there
    local_root = str(tmp_path / 'local')
    _make_populated_pgdata(local_root)           # seed completed → local populated
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(local_root, 'pgdata')


def test_gate_genuine_first_boot_uses_local(tmp_path, monkeypatch):
    """No legacy, no dump (genuine first-ever boot) → use local; nothing to lose."""
    data_dir = str(tmp_path / 'data')
    os.makedirs(data_dir, exist_ok=True)
    local_root = str(tmp_path / 'local')
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(local_root, 'pgdata')


# ── Log-spam de-duplication (the 3143×/day WARNING that buried real errors) ──

def test_split_fallback_warning_logged_at_most_once(tmp_path, monkeypatch, caplog):
    """resolve_pgdata_dir is called on many hot paths; the split-fallback
    WARNING (split on + local not yet seeded) must be emitted ONCE per process,
    not once per call — otherwise it spams thousands of identical lines/day and
    buries real errors. The RETURN value must still be correct on every call."""
    data_dir = str(tmp_path / 'data')
    _make_populated_pgdata(data_dir)             # populated legacy → recoverable
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', str(tmp_path / 'local'))  # empty local

    legacy = os.path.join(data_dir, 'pgdata')
    with caplog.at_level('WARNING', logger='lib.database.db_paths'):
        for _ in range(50):
            assert db_paths.resolve_pgdata_dir(data_dir) == legacy  # every call correct

    fallback_warnings = [r for r in caplog.records
                         if 'Split engaged but local pgdata' in r.getMessage()]
    assert len(fallback_warnings) == 1, (
        f'expected exactly 1 fallback warning across 50 calls, '
        f'got {len(fallback_warnings)} — log de-dup regressed')

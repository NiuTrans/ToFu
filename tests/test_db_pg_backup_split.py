"""Decoupling D sub-cut 3 — backup/PITR/self-heal relocated to _pg_backup.py.

The backup/base-backup/PITR/self-heal cluster (Tier A logical dump, Tier B
pg_basebackup, §3a restore-channel selector, cold-start PITR replay, and the
2026-06-04-class corrupt-cluster self-heal) moved OUT of the 1800-line
``lib/database/_bootstrap.py`` into ``lib/database/_pg_backup.py`` — mirroring
the ``_pg_ownership`` (sub-cut 1) and ``_pg_seed`` (sub-cut 2) pattern.

These tests pin the RELOCATION CONTRACT (not the behaviour — that stays covered
by test_db_tier_b.py / test_db_seed_local_migration.py):

  1. ``_pg_backup`` is a real module defining all 11 relocated functions.
  2. ``_bootstrap`` re-exports them (explicit facade) so every existing caller
     — ``_bootstrap.<name>`` / ``from lib.database._bootstrap import <name>`` —
     keeps resolving byte-for-byte.
  3. The public top-level facade ``from lib.database import backup_pg_database``
     still resolves (scheduler + external callers).
  4. The scheduler's ``from lib.database._bootstrap import basebackup_pg_cluster``
     path still resolves.
  5. ``_pg_seed``'s lazy shims (``_latest_pg_backup`` / ``_recover_via_pitr`` /
     ``_select_restore_channel`` / ``_quarantine_corrupt_pgdata``) still resolve
     THROUGH the ``_bootstrap`` facade to the relocated implementations — so the
     seed pipeline is unaffected.
  6. No import cycle: importing ``_pg_backup`` bare succeeds.
"""

import importlib

import pytest


# The functions that MUST live in the new backup module.
_RELOCATED = [
    'backup_pg_database',
    '_latest_pg_backup',
    '_tier_b_wal_end_ts',
    '_tier_a_dump_end_ts',
    '_select_restore_channel',
    '_base_backup_dir',
    '_latest_base_backup',
    'basebackup_pg_cluster',
    '_recover_via_pitr',
    '_quarantine_corrupt_pgdata',
    '_try_self_heal_corrupt_pg',
]


def test_pg_backup_module_defines_all_relocated_fns():
    backupmod = importlib.import_module('lib.database._pg_backup')
    for name in _RELOCATED:
        fn = getattr(backupmod, name, None)
        assert callable(fn), f'{name} missing from lib.database._pg_backup'
        # It must be DEFINED here, not a lazy shim re-imported from _bootstrap.
        assert fn.__module__ == 'lib.database._pg_backup', (
            f'{name} should be defined IN _pg_backup, not imported from '
            f'{fn.__module__}')


def test_bootstrap_facade_reexports_relocated_fns():
    boot = importlib.import_module('lib.database._bootstrap')
    backupmod = importlib.import_module('lib.database._pg_backup')
    for name in _RELOCATED:
        assert hasattr(boot, name), f'_bootstrap facade lost {name}'
        # The facade object IS the relocated implementation (same identity).
        assert getattr(boot, name) is getattr(backupmod, name), (
            f'_bootstrap.{name} is not the relocated _pg_backup.{name}')


def test_bootstrap_from_import_still_resolves():
    # The exact form the scheduler uses for base backup.
    from lib.database._bootstrap import basebackup_pg_cluster  # noqa: F401
    from lib.database._bootstrap import backup_pg_database  # noqa: F401
    from lib.database._bootstrap import _try_self_heal_corrupt_pg  # noqa: F401
    assert callable(basebackup_pg_cluster)


def test_public_top_level_facade_resolves():
    from lib.database import backup_pg_database
    assert callable(backup_pg_database)


def test_pg_seed_shims_resolve_through_facade_to_backup():
    """_pg_seed's lazy shims import from _bootstrap; that must land on the
    relocated _pg_backup implementations (identity check)."""
    backupmod = importlib.import_module('lib.database._pg_backup')
    # Resolve each shim the way _pg_seed does at call time.
    from lib.database._bootstrap import _latest_pg_backup as seed_latest
    from lib.database._bootstrap import _recover_via_pitr as seed_pitr
    from lib.database._bootstrap import _select_restore_channel as seed_sel
    from lib.database._bootstrap import _quarantine_corrupt_pgdata as seed_q
    assert seed_latest is backupmod._latest_pg_backup
    assert seed_pitr is backupmod._recover_via_pitr
    assert seed_sel is backupmod._select_restore_channel
    assert seed_q is backupmod._quarantine_corrupt_pgdata


def test_pg_backup_imports_bare_no_cycle():
    # A bare import must not deadlock/raise on an import cycle.
    mod = importlib.import_module('lib.database._pg_backup')
    assert mod is not None


def test_backup_cluster_no_longer_defined_in_bootstrap_source():
    """The relocated function BODIES must be gone from _bootstrap.py (only the
    facade import remains) — otherwise the split didn't actually shrink the file
    and two divergent copies could drift."""
    import lib.database._bootstrap as boot
    with open(boot.__file__, 'r') as fh:
        src = fh.read()
    for name in _RELOCATED:
        assert f'def {name}(' not in src, (
            f'{name} still has a def body in _bootstrap.py — move it to '
            f'_pg_backup.py and keep only the facade import')

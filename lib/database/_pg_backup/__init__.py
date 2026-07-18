"""PostgreSQL durability: logical backup, Tier-B base backup, PITR, self-heal.

Extracted from lib/database/_bootstrap.py (2026-07-11, Decoupling D sub-cut 3,
§10 signed off), then split into a facade-preserving package. The
backup/restore/recovery cluster:
  • Tier A logical dump (`backup_pg_database` → data/pg_backups/pg_dumpall_*.sql)
    + `_latest_pg_backup` selector.
  • §3a newest-wins restore-channel selector (`_select_restore_channel`,
    `_tier_a_dump_end_ts`, `_tier_b_wal_end_ts`).
  • Tier B self-contained base backup (`basebackup_pg_cluster`,
    `_base_backup_dir`, `_latest_base_backup`).
  • Cold-start PITR replay (`_recover_via_pitr`).
  • Corrupt-cluster self-heal / quarantine (`_quarantine_corrupt_pgdata`,
    `_try_self_heal_corrupt_pg`) — the 2026-06-04 WAL-redo-PANIC recovery class.

Its call-outs into core/ownership (start/verify/config/port/db-exists/owner
markers/binaries/bootstrap) resolve LAZILY via in-body import to avoid an
import cycle (see ``_shims``). _bootstrap re-imports these fns (explicit facade)
so ``_bootstrap.<name>`` / ``from lib.database._bootstrap import <name>`` keep
resolving for _core.py, the scheduler, _pg_seed's own lazy shims, and the
pg_*/tier_b/seed test suites.

This ``__init__`` re-exports EVERY symbol so all
``from lib.database._pg_backup import X`` keep working byte-identically; the
import path is UNCHANGED.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Lazy shims: core/ownership helpers this cluster calls (see _shims). ──
from lib.database._pg_backup._shims import (  # noqa: E402,F401
    _bootstrap_pg,
    _ensure_database_exists,
    _ensure_managed_pg_config,
    _find_pg_binary,
    _get_username,
    _mark_pg_owned_locally,
    _pg_binaries_present,
    _read_our_pg_port,
    _stop_local_pg_quietly,
    _tier_b_enabled,
    _verify_pg_after_start,
    _write_owner_host,
)

# ── Tier A logical dump ──
from lib.database._pg_backup._dump import (  # noqa: E402,F401
    _latest_pg_backup,
    _tier_a_dump_end_ts,
    backup_pg_database,
)

# ── Tier B base backup ──
from lib.database._pg_backup._basebackup import (  # noqa: E402,F401
    _base_backup_dir,
    _latest_base_backup,
    _tier_b_wal_end_ts,
    basebackup_pg_cluster,
)

# ── Restore/recovery: channel selector + cold-start PITR ──
from lib.database._pg_backup._restore import (  # noqa: E402,F401
    _recover_via_pitr,
    _select_restore_channel,
)

# ── Corrupt-cluster self-heal / quarantine ──
from lib.database._pg_backup._selfheal import (  # noqa: E402,F401
    _quarantine_corrupt_pgdata,
    _try_self_heal_corrupt_pg,
)


# The relocation-contract tests assert the 11 relocated functions report
# ``__module__ == 'lib.database._pg_backup'`` (they are logically DEFINED by
# this package, not lazy shims re-imported from _bootstrap). Normalise the
# attribute so the facade split is transparent to callers and tests alike.
for _name in (
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
):
    try:
        globals()[_name].__module__ = 'lib.database._pg_backup'
    except (AttributeError, TypeError):
        pass
del _name


__all__ = [
    # Tier A logical dump
    'backup_pg_database',
    '_latest_pg_backup',
    '_tier_a_dump_end_ts',
    # Tier B base backup
    'basebackup_pg_cluster',
    '_base_backup_dir',
    '_latest_base_backup',
    '_tier_b_wal_end_ts',
    # Restore/recovery
    '_select_restore_channel',
    '_recover_via_pitr',
    # Self-heal / quarantine
    '_quarantine_corrupt_pgdata',
    '_try_self_heal_corrupt_pg',
    # Lazy shims (re-exported for facade compatibility)
    '_find_pg_binary',
    '_get_username',
    '_tier_b_enabled',
    '_ensure_managed_pg_config',
    '_read_our_pg_port',
    '_verify_pg_after_start',
    '_ensure_database_exists',
    '_write_owner_host',
    '_mark_pg_owned_locally',
    '_pg_binaries_present',
    '_stop_local_pg_quietly',
    '_bootstrap_pg',
]

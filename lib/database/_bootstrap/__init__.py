"""PostgreSQL server management — auto-bootstrap, start, stop, remote discovery.

Facade package (facade-preserving split of the former single-module
``_bootstrap.py``). Every public/private symbol AND every back-compat
re-export the old module exposed is re-exported here, so that
``from lib.database._bootstrap import X`` and ``import lib.database._bootstrap
as b; b.X`` behave byte-identically to the pre-split layout. The import path
``lib.database._bootstrap`` is UNCHANGED.

Extracted from _core.py for modularity. Called from _core at import time.
Cross-platform: works on Linux, macOS, and Windows.

Sub-modules
-----------
  _config       — managed postgresql.conf tuning block (restart staged only)
  _verify       — identity / health / binary-presence probes
  _process      — port read, quiet stop, port scan, shutdown _stop_pg
  _database     — createdb + SQL-dump restore
  _orchestrate  — _bootstrap_pg / _try_explicit_pg_target / _ensure_pg_running

BACK-COMPAT RE-EXPORT CHAINS (do not remove)
--------------------------------------------
Ownership / lock / heartbeat / host-identity live in ``_pg_ownership``;
seed/dump/legacy migration live in ``_pg_seed``; backup / base-backup / PITR /
self-heal live in ``_pg_backup``. All three are re-imported here so every
caller of ``_bootstrap.<name>`` (incl. _core.py, pg_admin.py, the scheduler,
desktop/, and the pg_*/tier_b/seed test suites) keeps resolving. Each sibling
resolves its own call-outs back into core/bootstrap lazily → no import cycle.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Ownership / lock / heartbeat / host-identity relocated to
#    _pg_ownership.py (2026-07-11, Decoupling D). Re-imported here so every
#    caller of `_bootstrap.<name>` / `from lib.database._bootstrap import <name>`
#    (incl. _core.py, pg_admin.py, desktop/, and the pg_* test suites that do
#    `_bootstrap as b; b._<private>`) keeps resolving. The sibling resolves its
#    two core call-outs (_audit, _pg_real_connect_ok) lazily → no import cycle.
from lib.database import _pg_ownership as _pg_ownership  # noqa: F401,E402
from lib.database._pg_ownership import (  # noqa: F401,E402
    _PG_STARTED_BY_US, _HEARTBEAT_FILE, _HEARTBEAT_TTL_S, _HEARTBEAT_REFRESH_S,
    _heartbeat_thread, _heartbeat_stop_event, _heartbeat_lock,
    _STARTUP_LOCK_FILE, _startup_lock_fd, _startup_lock_mu, _startup_lock_path,
    _try_acquire_startup_lock, _release_startup_lock, _flock_enforced,
    _flock_probe_mu, _probe_flock_enforced, _flock_required,
    _verify_flock_support_or_warn, _heartbeat_path, _read_heartbeat,
    _heartbeat_is_fresh, _write_heartbeat, _clear_heartbeat, _heartbeat_loop,
    _start_heartbeat_thread, stop_heartbeat, _INSTANCE_ID_FILE, _instance_id_path,
    _canonical_pgdata_path, _read_instance_stamp, _write_instance_stamp,
    _pgdata_was_copied, _clear_ownership_markers, _heal_if_copied,
    _standalone_mode, _heal_if_standalone_remote_owner, _mark_pg_owned_locally,
    is_pg_owned_locally, _find_pg_binary, _get_username, _read_pg_host_from_pidfile,
    _pidfile_pid_is_live_local_postgres, _get_local_ip, _HOST_IDENTITY_CACHE,
    _OWNER_ID_FILE, _get_host_identity, _owner_is_self, _write_owner_host,
    _pg_already_running_on_another_machine, _find_free_port, _fix_unix_socket_conf,
)
# _PG_STARTED_BY_US + its accessors (_mark_pg_owned_locally / is_pg_owned_locally)
# now live in _pg_ownership; shutdown_pool() in _core.py reads them via the
# is_pg_owned_locally() FUNCTION (re-exported above), never the raw global.

# ── Seed/dump/legacy migration relocated to _pg_seed.py (2026-07-11,
#    Decoupling D sub-cut 2). Re-imported here so _bootstrap.<name> keeps
#    resolving. _pg_seed calls back into core lazily (no import cycle).
from lib.database import _pg_seed as _pg_seed  # noqa: F401,E402
from lib.database._pg_seed import (  # noqa: F401,E402
    _pgdata_major_compatible, _dump_live_cluster, _count_convs,
    _ensure_legacy_up_for_seed, _seed_local_pgdata_from_legacy,
)


# ── Backup / base-backup / PITR / self-heal relocated to _pg_backup.py
#    (2026-07-11, Decoupling D sub-cut 3). Re-imported here so _bootstrap.<name>
#    / `from lib.database._bootstrap import <name>` keep resolving for _core.py,
#    the scheduler, _pg_seed's own lazy shims, and the pg_*/tier_b/seed suites.
#    _pg_backup calls back into core lazily (no import cycle).
from lib.database import _pg_backup as _pg_backup  # noqa: F401,E402
from lib.database._pg_backup import (  # noqa: F401,E402
    backup_pg_database, _latest_pg_backup, _tier_b_wal_end_ts,
    _tier_a_dump_end_ts, _select_restore_channel, _base_backup_dir,
    _latest_base_backup, basebackup_pg_cluster, _recover_via_pitr,
    _quarantine_corrupt_pgdata, _try_self_heal_corrupt_pg,
)


# ── This package's own implementation, split across cohesive sub-modules ──
from lib.database._bootstrap._config import (  # noqa: F401,E402
    _MANAGED_BLOCK_BEGIN, _MANAGED_BLOCK_END, _MANAGED_PG_MAX_CONNECTIONS,
    _tier_b_enabled, _pgdata_is_resolved_primary, _build_managed_pg_config,
    _ensure_managed_pg_config, _restart_local_pg,
)
from lib.database._bootstrap._verify import (  # noqa: F401,E402
    _verify_pg_data_directory, _pg_has_database, _pg_real_connect_ok,
    _verify_pg_after_start, _pg_binaries_present,
)
from lib.database._bootstrap._process import (  # noqa: F401,E402
    _read_our_pg_port, _stop_local_pg_quietly, _boot_stop_pg_quietly,
    _scan_for_our_pg, _stop_pg,
)
from lib.database._bootstrap._database import (  # noqa: F401,E402
    _ensure_database_exists, _restore_from_sql_dump_if_present,
)
from lib.database._bootstrap._orchestrate import (  # noqa: F401,E402
    _bootstrap_pg, _try_explicit_pg_target, _ensure_pg_running,
)

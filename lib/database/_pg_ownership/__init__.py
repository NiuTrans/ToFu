"""PostgreSQL ownership / lock / heartbeat / host-identity (facade package).

Extracted from lib/database/_bootstrap.py (2026-07-11, Decoupling D). Split
into cohesive submodules (2026-07-14) while preserving the import path: this
package is a PURE RE-EXPORT FACADE, so ``from lib.database._pg_ownership import X``
and ``lib.database._pg_ownership.X`` keep resolving byte-identically.

All FIVE mutable module globals are grouped with ALL their accessors so no
``global`` mutation straddles a module boundary:
  • ``_PG_STARTED_BY_US``   → ._ownership
  • ``_startup_lock_fd``     → ._lock
  • ``_flock_enforced``      → ._flock
  • ``_heartbeat_thread``    → ._heartbeat
  • ``_HOST_IDENTITY_CACHE`` → ._hostid

Because tests patch internals on THIS package (e.g.
``monkeypatch.setattr(pg_ownership, '_probe_flock_enforced', ...)``), every
cross-submodule callee that a test may patch is resolved THROUGH this facade
module at call time (``import lib.database._pg_ownership as _pkg; _pkg._name(...)``).

The two core call-outs (``_audit``, ``_pg_real_connect_ok``) live in ._binaries
and resolve their targets LAZILY (in-body import) to avoid an import cycle with
_bootstrap. They are re-exported here so ``_bootstrap.<name>`` keeps resolving,
but are deliberately NOT listed in ``__all__`` (they belong to core).
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── ._lock — cross-host startup flock (owns _startup_lock_fd) ────────────
from lib.database._pg_ownership._lock import (  # noqa: E402,F401
    _STARTUP_LOCK_FILE, _startup_lock_fd, _startup_lock_mu, _startup_lock_path,
    _try_acquire_startup_lock, _release_startup_lock,
)

# ── ._flock — advisory-lock enforcement probe (owns _flock_enforced) ─────
from lib.database._pg_ownership._flock import (  # noqa: E402,F401
    _flock_enforced, _flock_probe_mu, _probe_flock_enforced, _flock_required,
    _verify_flock_support_or_warn,
)

# ── ._heartbeat — tofu heartbeat (owns _heartbeat_thread) ────────────────
from lib.database._pg_ownership._heartbeat import (  # noqa: E402,F401
    _HEARTBEAT_FILE, _HEARTBEAT_TTL_S, _HEARTBEAT_REFRESH_S,
    _heartbeat_thread, _heartbeat_stop_event, _heartbeat_lock,
    _heartbeat_path, _read_heartbeat, _heartbeat_is_fresh, _write_heartbeat,
    _clear_heartbeat, _heartbeat_loop, _start_heartbeat_thread, stop_heartbeat,
)

# ── ._identity — instance-id stamp + copy/move self-heal ─────────────────
from lib.database._pg_ownership._identity import (  # noqa: E402,F401
    _INSTANCE_ID_FILE, _instance_id_path, _canonical_pgdata_path,
    _read_instance_stamp, _write_instance_stamp, _pgdata_was_copied,
    _clear_ownership_markers, _heal_if_copied,
)

# ── ._ownership — process-ownership flag (owns _PG_STARTED_BY_US) ────────
from lib.database._pg_ownership._ownership import (  # noqa: E402,F401
    _PG_STARTED_BY_US, _standalone_mode, _heal_if_standalone_remote_owner,
    _mark_pg_owned_locally, is_pg_owned_locally,
)

# ── ._hostid — host identity / local IP / owner-host markers ─────────────
from lib.database._pg_ownership._hostid import (  # noqa: E402,F401
    _get_local_ip, _HOST_IDENTITY_CACHE, _OWNER_ID_FILE, _get_host_identity,
    _owner_is_self, _write_owner_host,
)

# ── ._binaries — binary discovery + pidfile / remote-owner probes ────────
from lib.database._pg_ownership._binaries import (  # noqa: E402,F401
    _find_pg_binary, _get_username, _read_pg_host_from_pidfile,
    _pidfile_pid_is_live_local_postgres, _pg_already_running_on_another_machine,
    _find_free_port, _fix_unix_socket_conf,
    _audit, _pg_real_connect_ok,
)


# Explicit re-export surface (import * skips _underscore names without this).
# Every moved symbol the _bootstrap facade must expose so `_bootstrap.<name>`
# and `b._<private>` (pg_* tests) resolve. The two lazy shims (_audit,
# _pg_real_connect_ok) are NOT listed (they belong to core; re-exporting via
# __all__ would shadow the real ones on `from ... import *`).
__all__ = [
    '_PG_STARTED_BY_US', '_HEARTBEAT_FILE', '_HEARTBEAT_TTL_S', '_HEARTBEAT_REFRESH_S',
    '_heartbeat_thread', '_heartbeat_stop_event', '_heartbeat_lock',
    '_STARTUP_LOCK_FILE', '_startup_lock_fd', '_startup_lock_mu', '_startup_lock_path',
    '_try_acquire_startup_lock', '_release_startup_lock', '_flock_enforced',
    '_flock_probe_mu', '_probe_flock_enforced', '_flock_required',
    '_verify_flock_support_or_warn', '_heartbeat_path', '_read_heartbeat',
    '_heartbeat_is_fresh', '_write_heartbeat', '_clear_heartbeat', '_heartbeat_loop',
    '_start_heartbeat_thread', 'stop_heartbeat', '_INSTANCE_ID_FILE', '_instance_id_path',
    '_canonical_pgdata_path', '_read_instance_stamp', '_write_instance_stamp',
    '_pgdata_was_copied', '_clear_ownership_markers', '_heal_if_copied',
    '_standalone_mode', '_heal_if_standalone_remote_owner', '_mark_pg_owned_locally',
    'is_pg_owned_locally', '_find_pg_binary', '_get_username', '_read_pg_host_from_pidfile',
    '_pidfile_pid_is_live_local_postgres', '_get_local_ip', '_HOST_IDENTITY_CACHE',
    '_OWNER_ID_FILE', '_get_host_identity', '_owner_is_self', '_write_owner_host',
    '_pg_already_running_on_another_machine', '_find_free_port', '_fix_unix_socket_conf',
]

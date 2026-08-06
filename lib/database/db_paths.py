"""lib/database/db_paths.py — Where the live cluster and its backups live.

Single source of truth for the DB durability topology, isolated here so the
policy is unit-testable WITHOUT a live PG, a real FUSE mount, or importing the
heavy ``_core`` / ``_bootstrap`` modules. See the JOURNAL design entry
"DB durability redesign: local-disk-primary PG + DolphinFS-as-replication-target".

Background
----------
On this cluster the project (hence ``data_root()``) sits on a DolphinFS FUSE
mount. Running the LIVE PostgreSQL cluster off a network filesystem is
unsupported (WAL needs a ``-shm`` mmap; POSIX locking is unreliable) and has
caused real corruption incidents. The fix is **local-disk-primary +
FUSE-as-backup-target**:

  * The live ``pgdata`` moves to fast, POSIX-correct local disk
    (``$TOFU_DB_LOCAL_ROOT/pgdata``, default ``/tmp/tofu``).
  * Backups (Tier A logical dumps now; Tier B base+WAL later) are written to
    ``$TOFU_DB_BACKUP_ROOT`` on the durable FUSE mount.

Default-safe
------------
The split ONLY engages when ``data_root()`` is detected on a network mount.
On a vanilla local-disk box the resolved pgdata path is BYTE-IDENTICAL to the
legacy ``<data_root>/pgdata`` — the feature is a no-op where it isn't needed.
Explicit env vars always win over auto-detection.
"""

import os

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'is_network_mount',
    'local_data_split_enabled',
    'resolve_pgdata_dir',
    'resolve_backup_root',
    'legacy_pgdata_dir',
    'pgdata_is_populated',
    'has_recoverable_source',
]

# Default parent dir for the live cluster when the split engages. ``/tmp`` on
# this cluster is a distinct local xfs volume (``/dev/md0p1``), NOT the volatile
# overlay root — see the JOURNAL §1c mount probe. Overridable via env.
_DEFAULT_LOCAL_ROOT = '/tmp/tofu'

# resolve_pgdata_dir() is a PURE, deterministic resolver called on many hot
# paths (the _core import, the Tier-B archiving probe, bootstrap gates …), so
# its decision log line would otherwise repeat identically thousands of times a
# day and bury real errors. The decision is a function of the resolved
# (pgdata, legacy) pair, which is stable for a given deployment — so log each
# distinct decision at most ONCE per process. Set, not memoized return value:
# the resolution itself stays live (a mid-life seed flips it), only the LOG is
# de-duplicated.
_LOGGED_RESOLUTIONS: set = set()


def is_network_mount(path: str) -> bool:
    """True when *path* looks like it lives on a network / FUSE mount.

    Uses the same ``/mnt/`` heuristic as ``lib.fs_keepalive._is_network_mount``
    (DolphinFS/BeeGFS/NFS convention on Linux). Kept as a tiny local predicate
    rather than importing fs_keepalive so this module has no import weight and
    the trigger is trivially forced True/False in a unit test.

    Args:
        path: Absolute filesystem path to classify.

    Returns:
        True if the path is on a network mount (split should engage).
    """
    try:
        return bool(path) and os.path.abspath(path).startswith('/mnt/')
    except (OSError, ValueError) as e:
        logger.debug('[db_paths] is_network_mount(%r) failed: %s', path, e)
        return False


def local_data_split_enabled(data_dir: str) -> bool:
    """Whether the local-primary / FUSE-backup split should engage.

    The split engages iff ``data_dir`` (the legacy ``data/`` root, normally
    ``data_root()``) is on a network mount — UNLESS overridden:

      * ``TOFU_DB_LOCAL_SPLIT=1/0`` forces the decision either way.

    Args:
        data_dir: The legacy data root (parent of the legacy ``pgdata``).

    Returns:
        True → resolve pgdata to local disk; False → legacy behaviour.
    """
    forced = getenv_compat('TOFU_DB_LOCAL_SPLIT', default='').strip().lower()
    if forced in ('1', 'true', 'yes'):
        return True
    if forced in ('0', 'false', 'no'):
        return False
    return is_network_mount(data_dir)


def resolve_pgdata_dir(data_dir: str) -> str:
    """Resolve where the LIVE pgdata cluster should live.

    Args:
        data_dir: The legacy data root (``data_root()``); the legacy pgdata is
            ``<data_dir>/pgdata``.

    Returns:
        ``<data_dir>/pgdata`` (byte-identical to legacy) when the split is off,
        else ``$TOFU_DB_LOCAL_ROOT/pgdata`` (default ``/tmp/tofu/pgdata``).
    """
    legacy = os.path.join(data_dir, 'pgdata')
    if not local_data_split_enabled(data_dir):
        return legacy

    local_root = getenv_compat('TOFU_DB_LOCAL_ROOT', default='').strip() \
        or _DEFAULT_LOCAL_ROOT
    pgdata = os.path.join(os.path.abspath(local_root), 'pgdata')

    # ── Ordering-hazard gate (anti-silent-loss) ──────────────────────────
    # The split moves the LIVE cluster to `pgdata` (local). Resolving to it is
    # only safe once it is actually POPULATED. If it is empty/absent WHILE a
    # recoverable source exists on FUSE (a populated legacy pgdata OR a logical
    # dump), pointing at it would make the next start `initdb` a FRESH EMPTY
    # cluster and silently lose that history. So: while local is unpopulated,
    # stay on the legacy FUSE pgdata. The one-time seed migration
    # (_bootstrap._seed_local_pgdata_from_legacy) is what POPULATES local; only
    # then does resolution flip to it. This makes the flip a CONSEQUENCE of a
    # successful seed, never a precondition — a restart can never come up empty
    # while recoverable data exists. (If local is empty AND there is nothing to
    # recover — a genuine first-ever boot — we DO use local: nothing to lose.)
    if not pgdata_is_populated(pgdata) and has_recoverable_source(data_dir):
        _key = ('fallback', pgdata, legacy)
        if _key not in _LOGGED_RESOLUTIONS:
            _LOGGED_RESOLUTIONS.add(_key)
            logger.warning('[db_paths] Split engaged but local pgdata=%s not yet '
                           'populated while recoverable history exists (legacy=%s, '
                           'backups=%s) — staying on legacy FUSE pgdata. The /tmp '
                           'local-primary seed was WITHDRAWN 2026-08-06 (owner: DB '
                           'never deploys outside the project dir); this state is '
                           'the steady state. (Logged once per process.)', pgdata,
                           legacy, resolve_backup_root(data_dir))
        return legacy

    _key = ('engaged', pgdata, legacy)
    if _key not in _LOGGED_RESOLUTIONS:
        _LOGGED_RESOLUTIONS.add(_key)
        logger.info('[db_paths] Local-primary split ENGAGED: live pgdata=%s '
                    '(legacy would have been %s on the network mount). '
                    '(Logged once per process.)', pgdata, legacy)
    return pgdata


def legacy_pgdata_dir(data_dir: str) -> str:
    """The legacy (pre-split) pgdata location: ``<data_dir>/pgdata`` on FUSE."""
    return os.path.join(data_dir, 'pgdata')


def pgdata_is_populated(pgdata: str) -> bool:
    """True if *pgdata* looks like a real initialized cluster (not empty/absent)."""
    try:
        return (os.path.isfile(os.path.join(pgdata, 'PG_VERSION'))
                or os.path.isfile(os.path.join(pgdata, 'postgresql.conf')))
    except OSError as e:
        logger.debug('[db_paths] pgdata_is_populated(%r) failed: %s', pgdata, e)
        return False


def has_recoverable_source(data_dir: str) -> bool:
    """True if history can be recovered to seed a fresh local cluster.

    A recoverable source is EITHER a populated legacy FUSE ``<data>/pgdata``
    OR at least one logical dump under the backup root. This is the gate that
    lets ``resolve_pgdata_dir`` decide whether pointing at an empty local root
    is safe (a seed exists) or a data-loss trap (nothing to restore from).
    """
    if pgdata_is_populated(legacy_pgdata_dir(data_dir)):
        return True
    backup_root = resolve_backup_root(data_dir)
    try:
        for fn in os.listdir(backup_root):
            if fn.startswith('pg_dumpall_') and fn.endswith('.sql'):
                fp = os.path.join(backup_root, fn)
                try:
                    if os.path.getsize(fp) > 0:
                        return True
                except OSError as e:
                    logger.debug('[db_paths] getsize(%r) probe failed: %s', fp, e)
                    continue
    except OSError as e:
        logger.debug('[db_paths] has_recoverable_source scan of %r failed: %s',
                     backup_root, e)
    return False


def resolve_backup_root(data_dir: str) -> str:
    """Resolve the durability target directory for DB backups.

    Args:
        data_dir: The legacy data root (``data_root()``).

    Returns:
        ``$TOFU_DB_BACKUP_ROOT`` when set, else the legacy
        ``<data_dir>/pg_backups`` (which is on the durable FUSE mount when the
        split is engaged — exactly where we want backups to persist).
    """
    explicit = getenv_compat('TOFU_DB_BACKUP_ROOT', default='').strip()
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(data_dir, 'pg_backups')

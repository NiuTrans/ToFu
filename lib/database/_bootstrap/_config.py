"""Managed PostgreSQL tuning config — code-managed postgresql.conf block.

Historically the durability + sizing settings (max_connections, wal_level,
fsync, …) were appended to postgresql.conf MANUALLY, once, under a
"# ── ChatUI Custom Config ──" header. Nothing in the codebase maintained
them, so bumping the app-side TOFU_DB_MAX_CONNS did NOT raise PG's own
max_connections ceiling, and durability settings could silently drift.

This module makes the config code-managed: every owned-PG startup rewrites a
single delimited block (idempotently). PG reads the LAST occurrence of a
setting in the file, so appending our block also overrides any older manual
entries above it.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import subprocess
import sys

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_ownership import _find_pg_binary

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Managed PostgreSQL tuning block
# ─────────────────────────────────────────────────────────────────────

_MANAGED_BLOCK_BEGIN = '# ── Tofu managed config (auto-generated; do not edit) BEGIN ──'
_MANAGED_BLOCK_END = '# ── Tofu managed config END ──'

# PG server-side max_connections. Provisioned ABOVE the app-side semaphore
# ceiling (_MAX_TOTAL_CONNS, default 1000) so the application — not PG's
# hard FATAL limit — is always the binding constraint. The extra headroom
# absorbs superuser/maintenance/replication connections. Override via env.
_MANAGED_PG_MAX_CONNECTIONS = int(
    getenv_compat('TOFU_PG_MAX_CONNECTIONS', default='1100'))


def _tier_b_enabled():
    """True when Tier B (WAL archive + PITR) is opted in."""
    return getenv_compat('TOFU_DB_TIER_B', default='0').lower() in ('1', 'true', 'yes')


def _pgdata_is_resolved_primary(pgdata):
    """True when *pgdata* is the LOCAL primary the split resolves to (post-flip).

    Tier B archiving must engage ONLY against the local primary — never the
    legacy FUSE cluster while resolution is still on legacy (pre-seed). Writing
    archive_mode into the soon-to-be-retired legacy cluster would waste FUSE
    writes and muddy the §3a WAL-tail timestamp.
    """
    try:
        from lib.database.db_paths import resolve_pgdata_dir
        from lib.runtime_paths import data_root
        return os.path.abspath(pgdata) == os.path.abspath(resolve_pgdata_dir(data_root()))
    except Exception as e:
        logger.debug('[DB] primary-resolution probe failed: %s', e)
        return False


def _build_managed_pg_config(archive_enabled=False):
    """Return the body (settings only) of the managed postgresql.conf block.

    Args:
        archive_enabled: When True, emit Tier B ``archive_mode=on`` +
            ``archive_command``. Caller passes this only for the resolved LOCAL
            primary when TOFU_DB_TIER_B is opted in (never the legacy cluster).

    Durability is deliberately kept SAFE (fsync + synchronous_commit on,
    full_page_writes on) — the cluster lives on a shared FUSE mount where a
    torn page on crash would corrupt the whole cluster, exactly the failure
    that produced the 'lost conversations' incident. ``wal_level=replica``
    (up from the old ``minimal``) is what makes PITR / base-backup-based
    recovery possible, so a future corrupt primary is recoverable to a
    point-in-time instead of needing a data-losing ``pg_resetwal -f``.
    """
    return [
        f'max_connections = {_MANAGED_PG_MAX_CONNECTIONS}',
        'superuser_reserved_connections = 10',
        # Server-side backstop for leaked transactions: PG kills any backend
        # left 'idle in transaction' past this. Matched to the app-side idle
        # reaper (TOFU_DB_IDLE_RELEASE_S, default 120s) so a connection parked
        # mid-transaction by a long-lived worker is reclaimed even though the
        # app-side reaper deliberately skips non-IDLE connections.
        'idle_in_transaction_session_timeout = 120s',
        # ── Durability (do NOT relax on a FUSE-mounted cluster) ──
        'fsync = on',
        'synchronous_commit = on',
        'full_page_writes = on',
        # ── WAL: replica level enables base-backup + PITR recovery ──
        'wal_level = replica',
        'max_wal_senders = 10',
        'wal_compression = on',
        'max_wal_size = 2GB',
        'min_wal_size = 160MB',
        'checkpoint_completion_target = 0.9',
        # ── Memory sizing for a ~1000-connection workload ──
        'shared_buffers = 512MB',
        'effective_cache_size = 2GB',
        'work_mem = 8MB',
        'maintenance_work_mem = 128MB',
    ] + ([
        # ── Tier B: continuous WAL archiving to the DolphinFS durability
        # target (only on the resolved local primary; see B1/B2). The shim is
        # FUSE-stall-safe (hard timeout, non-zero-to-retain). Invoked as a
        # module so it inherits our env/paths and takes no shell string.
        'archive_mode = on',
        "archive_command = '%s -m lib.database.wal_archive archive %%p %%f'"
        % (sys.executable or 'python3'),
    ] if archive_enabled else [])


def _ensure_managed_pg_config(pgdata):
    """Idempotently write the managed tuning block into postgresql.conf.

    Returns:
        bool: True if the on-disk config CHANGED (caller should restart PG
        for ``max_connections`` / ``wal_level`` — which need a restart — to
        take effect), False if it was already up to date or on error.
    """
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return False

    # Tier B archiving engages ONLY when opted in AND this pgdata is the
    # resolved local primary (never the legacy cluster pre-flip).
    archive_enabled = _tier_b_enabled() and _pgdata_is_resolved_primary(pgdata)
    settings = _build_managed_pg_config(archive_enabled=archive_enabled)
    block_lines = [_MANAGED_BLOCK_BEGIN, *settings, _MANAGED_BLOCK_END]
    new_block = '\n'.join(block_lines) + '\n'

    try:
        with open(conf_path, encoding='utf-8') as f:
            content = f.read()

        import re
        # Strip any prior managed block (between the BEGIN/END markers).
        pattern = re.compile(
            re.escape(_MANAGED_BLOCK_BEGIN) + r'.*?' + re.escape(_MANAGED_BLOCK_END) + r'\n?',
            re.DOTALL)
        stripped = pattern.sub('', content)

        desired = stripped.rstrip('\n') + '\n\n' + new_block
        if desired == content:
            logger.debug('[DB] Managed PG config already current — no change')
            return False

        # Write atomically (temp file + replace) so a crash mid-write can't
        # leave a truncated postgresql.conf that bricks startup.
        tmp_path = conf_path + '.tofu.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(desired)
        os.replace(tmp_path, conf_path)
        logger.info('[DB] Wrote managed PG config block '
                    '(max_connections=%d, wal_level=replica, fsync=on) — '
                    'restart required for connection/WAL settings',
                    _MANAGED_PG_MAX_CONNECTIONS)
        return True
    except Exception as e:
        logger.warning('[DB] Could not write managed PG config block: %s', e)
        return False


def _restart_local_pg(pgdata, base_dir):
    """Restart the locally-owned PG with ``pg_ctl restart -m fast``.

    Used after _ensure_managed_pg_config reports a change to a restart-only
    setting (max_connections / wal_level). Best-effort: on failure the
    running PG keeps its previous (still-valid) config.

    Returns:
        bool: True on a successful restart.
    """
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    try:
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
             'restart', '-m', 'fast', '-w', '-t', '30'],
            capture_output=True, text=True, timeout=45
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl restart (for managed config) failed: %s',
                         (result.stderr or '').strip()[:300])
            return False
        logger.info('[DB] Restarted local PG to apply managed config')
        return True
    except Exception as e:
        logger.error('[DB] pg_ctl restart raised: %s', e, exc_info=True)
        return False

"""Tier A logical dump: ``backup_pg_database`` + latest/end-ts selectors.

The single biggest durability win for the 'lost conversations on crash'
problem — a SCHEDULED ``pg_dumpall`` (see lib/scheduler/manager.py) so a corrupt
primary can always be restored from a recent logical dump instead of a
data-losing ``pg_resetwal -f``.
"""

import os
import shutil
import subprocess
import time

from lib.env_compat import getenv_compat
from lib.log import get_logger, log_context

from lib.database._pg_backup._shims import _find_pg_binary, _get_username

logger = get_logger(__name__)


def backup_pg_database(retention_days=None):
    """Dump the live PG cluster to a timestamped SQL file under data/pg_backups/.

    This is the single biggest durability win for the 'lost conversations on
    crash' problem: the only reason the 2026-06-04 WAL-corruption incident was
    recoverable at all is that a manual ``pg_dumpall`` happened to exist. This
    makes that dump SCHEDULED (see the 'PostgreSQL Backup' task auto-registered
    in lib/scheduler/manager.py) so a corrupt primary can always be restored
    from a recent logical dump instead of a data-losing ``pg_resetwal -f``.

    PG-only; on SQLite the on-disk .db file already IS the durable artifact, so
    this is a silent no-op there.

    Args:
        retention_days: Delete dumps older than this many days. Defaults to
            env ``TOFU_PG_BACKUP_RETENTION_DAYS`` or 7.

    Returns:
        dict summary {ok, path, size_mb, pruned} — ok=False with a 'reason'
        when the dump was skipped or failed.
    """
    from lib.database._core import BASE_DIR, _BACKEND, PG_PORT, PG_USER
    from lib.database.db_paths import resolve_backup_root

    if _BACKEND != 'pg':
        return {'ok': False, 'reason': 'not_pg'}
    if PG_PORT == 0:
        return {'ok': False, 'reason': 'pg_unavailable'}

    if retention_days is None:
        try:
            retention_days = int(getenv_compat('TOFU_PG_BACKUP_RETENTION_DAYS',
                                                default='7'))
        except (TypeError, ValueError) as e:
            logger.debug('[DB-Backup] Invalid TOFU_PG_BACKUP_RETENTION_DAYS, defaulting to 7: %s', e)
            retention_days = 7

    pg_dumpall = shutil.which('pg_dumpall') or _find_pg_binary('pg_dumpall')
    if not pg_dumpall or not os.path.isabs(pg_dumpall):
        # _find_pg_binary returns the bare name when not found.
        if not shutil.which('pg_dumpall'):
            logger.warning('[DB-Backup] pg_dumpall not found on PATH — skipping backup')
            return {'ok': False, 'reason': 'pg_dumpall_missing'}

    backup_dir = resolve_backup_root(os.path.join(BASE_DIR, 'data'))
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        logger.error('[DB-Backup] Cannot create backup dir %s: %s', backup_dir, e)
        return {'ok': False, 'reason': f'mkdir_failed: {e}'}

    stamp = time.strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(backup_dir, f'pg_dumpall_{stamp}.sql')
    user = PG_USER or _get_username()

    try:
        with log_context('pg_backup', logger=logger):
            result = subprocess.run(
                [pg_dumpall, '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', user,
                 '--clean', '--if-exists', '-f', out_path],
                capture_output=True, text=True,
                env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            )
        if result.returncode != 0:
            logger.error('[DB-Backup] pg_dumpall failed (rc=%d): %s',
                         result.returncode, (result.stderr or '').strip()[:500])
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError as e:
                logger.debug('[DB-Backup] could not remove partial dump %s: %s', out_path, e)
            return {'ok': False, 'reason': f'dump_failed_rc{result.returncode}'}
    except Exception as e:
        logger.error('[DB-Backup] pg_dumpall invocation failed: %s', e, exc_info=True)
        return {'ok': False, 'reason': f'exception: {e}'}

    try:
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
    except OSError as e:
        logger.debug('[DB-Backup] could not stat dump %s: %s', out_path, e)
        size_mb = 0.0
    logger.info('[DB-Backup] Wrote %s (%.1f MB)', out_path, size_mb)

    # ── Prune old dumps past the retention window ──
    pruned = 0
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        try:
            for fn in os.listdir(backup_dir):
                if not (fn.startswith('pg_dumpall_') and fn.endswith('.sql')):
                    continue
                fpath = os.path.join(backup_dir, fn)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        pruned += 1
                except OSError as e:
                    logger.debug('[DB-Backup] Could not prune %s: %s', fpath, e)
        except OSError as e:
            logger.debug('[DB-Backup] Prune scan failed: %s', e)
    if pruned:
        logger.info('[DB-Backup] Pruned %d dump(s) older than %d days',
                    pruned, retention_days)

    try:
        from lib.log import audit_log
        audit_log('pg_backup', path=out_path, size_mb=round(size_mb, 1),
                  pruned=pruned, retention_days=retention_days)
    except Exception as _ae:
        logger.debug('[DB-Backup] audit_log failed: %s', _ae)

    return {'ok': True, 'path': out_path, 'size_mb': round(size_mb, 1),
            'pruned': pruned}


def _latest_pg_backup(base_dir):
    """Return the path to the most recent data/pg_backups/pg_dumpall_*.sql, or None."""
    from lib.database.db_paths import resolve_backup_root
    backup_dir = resolve_backup_root(os.path.join(base_dir, 'data'))
    try:
        candidates = [
            os.path.join(backup_dir, fn) for fn in os.listdir(backup_dir)
            if fn.startswith('pg_dumpall_') and fn.endswith('.sql')
        ]
    except OSError as e:
        logger.debug('[DB-Backup] no backup dir %s: %s', backup_dir, e)
        return None
    candidates = [p for p in candidates if os.path.getsize(p) > 0]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _tier_a_dump_end_ts(base_dir):
    """Recoverable end-timestamp of the latest Tier A logical dump, or None."""
    latest = _latest_pg_backup(base_dir)
    if not latest:
        return None
    try:
        return os.path.getmtime(latest)
    except OSError as e:
        logger.debug('[DB-Restore] no dump end-ts: %s', e)
        return None

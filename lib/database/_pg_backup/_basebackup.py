"""Tier B self-contained base backup: ``basebackup_pg_cluster`` + selectors.

``-X stream`` (owner decision) makes each base INDEPENDENTLY restorable even if
the WAL archive has a gap; archived WAL is layered on top for the seconds-RPO
tail. The WAL-archive end-ts (last replayable segment) is the true PITR replay
ceiling used by the §3a restore-channel selector.
"""

import os
import shutil
import subprocess
import time

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_backup._shims import _find_pg_binary, _tier_b_enabled

logger = get_logger(__name__)


def _tier_b_wal_end_ts(base_dir):
    """Recoverable end-timestamp of the Tier B WAL archive, or None if absent.

    This is the mtime of the LAST archived WAL segment in
    ``$TOFU_DB_BACKUP_ROOT/wal/`` — the true PITR replay ceiling (NOT the base
    backup time; recovery reaches the last replayable segment).
    """
    try:
        from lib.database.db_paths import resolve_backup_root
        wal_dir = os.path.join(resolve_backup_root(os.path.join(base_dir, 'data')), 'wal')
        segs = [os.path.join(wal_dir, fn) for fn in os.listdir(wal_dir)
                if not fn.startswith('.') and not fn.endswith('.tmp')]
        segs = [s for s in segs if os.path.isfile(s)]
        if not segs:
            return None
        return max(os.path.getmtime(s) for s in segs)
    except OSError as e:
        logger.debug('[DB-Restore] no WAL archive end-ts: %s', e)
        return None


def _base_backup_dir(base_dir):
    from lib.database.db_paths import resolve_backup_root
    return os.path.join(resolve_backup_root(os.path.join(base_dir, 'data')), 'base')


def _latest_base_backup(base_dir):
    """Path to the most recent pg_basebackup dir under $BACKUP_ROOT/base/, or None."""
    bdir = _base_backup_dir(base_dir)
    try:
        cands = [os.path.join(bdir, d) for d in os.listdir(bdir)
                 if os.path.isdir(os.path.join(bdir, d))
                 and os.path.isfile(os.path.join(bdir, d, 'PG_VERSION'))]
    except OSError as e:
        logger.debug('[DB-BaseBackup] no base dir %s: %s', bdir, e)
        return None
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def basebackup_pg_cluster():
    """Tier B: take a self-contained ``pg_basebackup -X stream`` of the primary.

    ``-X stream`` (owner decision) makes each base INDEPENDENTLY restorable even
    if the WAL archive has a gap; archived WAL is layered on top for the
    seconds-RPO tail. Engaged only when Tier B is opted in AND the split is
    active. PG-only; no-op otherwise. Returns a dict summary.
    """
    from lib.database._core import BASE_DIR, _BACKEND, PG_PORT, PG_USER
    from lib.database.db_paths import local_data_split_enabled
    if _BACKEND != 'pg':
        return {'ok': False, 'reason': 'not_pg'}
    if not _tier_b_enabled():
        return {'ok': False, 'reason': 'tier_b_off'}
    if not local_data_split_enabled(os.path.join(BASE_DIR, 'data')):
        return {'ok': False, 'reason': 'split_inactive'}
    pg_basebackup = _find_pg_binary('pg_basebackup')
    if not shutil.which(pg_basebackup) and not os.path.isfile(pg_basebackup):
        logger.warning('[DB-BaseBackup] pg_basebackup not found — skipping')
        return {'ok': False, 'reason': 'binary_missing'}
    bdir = _base_backup_dir(BASE_DIR)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out = os.path.join(bdir, stamp)
    try:
        os.makedirs(bdir, exist_ok=True)
        proc = subprocess.run(
            [pg_basebackup, '-h', '127.0.0.1', '-p', str(PG_PORT), '-U', PG_USER,
             '-D', out, '-X', 'stream', '--checkpoint=fast', '--no-password'],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            timeout=int(getenv_compat('TOFU_DB_BASEBACKUP_TIMEOUT', default='3600')),
        )
        if proc.returncode != 0:
            logger.error('[DB-BaseBackup] pg_basebackup failed (rc=%d): %s',
                         proc.returncode, (proc.stderr or '').strip()[:300])
            shutil.rmtree(out, ignore_errors=True)
            return {'ok': False, 'reason': 'basebackup_failed'}
    except Exception as e:
        logger.error('[DB-BaseBackup] pg_basebackup raised: %s', e, exc_info=True)
        shutil.rmtree(out, ignore_errors=True)
        return {'ok': False, 'reason': str(e)}
    logger.info('[DB-BaseBackup] Wrote base backup %s', out)
    return {'ok': True, 'path': out}

"""Backend-neutral database backups.

PostgreSQL keeps its existing logical dump while SQLite uses ``VACUUM INTO``
to create a compact, transactionally consistent, directly reopenable copy.
The SQLite path deliberately has no third-party dependency and keeps every
artifact below the project ``data/`` directory.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import time
import uuid

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.database.sqlite_driver_guard import allow_sqlite_driver_connection

logger = get_logger(__name__)

_SNAPSHOT_PREFIX = 'tofu-'
_SNAPSHOT_SUFFIX = '.sqlite3'
_LOCK_NAME = '.snapshot.lock'
_LOCK_STALE_S = 12 * 60 * 60


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(getenv_compat(name, default=str(default))))
    except (TypeError, ValueError):
        logger.warning('[DB-Backup] Invalid %s; using %d', name, default)
        return default


@contextmanager
def _snapshot_lock(snapshot_dir: Path):
    """Best-effort cross-process exclusion for an expensive full snapshot."""
    lock = snapshot_dir / _LOCK_NAME
    acquired = False
    try:
        try:
            lock.mkdir()
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > _LOCK_STALE_S
            except FileNotFoundError as exc:
                logger.debug('[DB-Backup] Snapshot lock vanished during stat: %s',
                             exc)
                stale = False
            if stale:
                displaced = snapshot_dir / (
                    f'{_LOCK_NAME}.stale-{os.getpid()}-{uuid.uuid4().hex}')
                try:
                    os.replace(lock, displaced)
                    displaced.rmdir()
                    lock.mkdir()
                    acquired = True
                except (FileExistsError, FileNotFoundError, OSError) as exc:
                    logger.debug('[DB-Backup] Snapshot stale-lock takeover lost race: %s',
                                 exc)
                    acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                lock.rmdir()
            except (FileNotFoundError, OSError) as exc:
                logger.debug('[DB-Backup] Could not release snapshot lock: %s', exc)


def _fsync_file_and_dir(path: Path) -> None:
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        # Some network filesystems reject directory fsync even though rename
        # and file fsync are supported. The completed snapshot remains valid.
        logger.debug('[DB-Backup] Directory fsync unsupported for %s: %s',
                     path.parent, exc)


def _integrity_check(path: Path) -> tuple[bool, str]:
    uri = path.as_uri() + '?mode=ro'
    with allow_sqlite_driver_connection('verify SQLite snapshot'):
        conn = sqlite3.connect(
            uri, uri=True, timeout=5.0, isolation_level=None)
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    finally:
        conn.close()
    messages = [str(row[0]) for row in rows]
    return messages == ['ok'], '; '.join(messages[:10])


def _prune_snapshots(snapshot_dir: Path, retention_count: int,
                     *, preserve: Path) -> int:
    try:
        candidates = sorted(
            (path for path in snapshot_dir.iterdir()
             if path.is_file()
             and path.name.startswith(_SNAPSHOT_PREFIX)
             and path.name.endswith(_SNAPSHOT_SUFFIX)),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError as exc:
        logger.warning('[DB-Backup] Snapshot retention scan failed: %s', exc)
        return 0
    keep = set(candidates[:retention_count])
    keep.add(preserve)
    pruned = 0
    for path in candidates:
        if path in keep:
            continue
        try:
            path.unlink()
            pruned += 1
        except OSError as exc:
            logger.warning('[DB-Backup] Could not prune %s: %s', path, exc)
    return pruned


def backup_sqlite_database(*, db_path: str | None = None,
                           snapshot_dir: str | None = None,
                           retention_count: int | None = None) -> dict:
    """Create and verify one compact online SQLite snapshot.

    ``VACUUM INTO`` reads a consistent source transaction while normal WAL
    readers and writers continue. The destination is first created under a
    unique temporary name, reopened for ``integrity_check``, fsynced, and only
    then atomically renamed into the retention set.
    """
    if db_path is None:
        from lib.database._core import DB_PATH
        db_path = DB_PATH
    source = Path(db_path).resolve()
    if not source.is_file():
        return {'ok': False, 'reason': 'sqlite_source_missing', 'path': str(source)}
    if snapshot_dir is None:
        snapshot_dir = (getenv_compat('TOFU_SQLITE_SNAPSHOT_DIR', default='').strip()
                        or str(source.parent / 'db_snapshots'))
        snapshot_dir = os.path.expandvars(os.path.expanduser(snapshot_dir))
    out_dir = Path(snapshot_dir).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error('[DB-Backup] Cannot create SQLite snapshot directory %s: %s',
                     out_dir, exc)
        return {'ok': False, 'reason': f'mkdir_failed: {exc}'}
    if retention_count is None:
        # A production authority is tens of GiB. Seven full VACUUM copies
        # would recreate the 300+ GiB dump hoard this redesign removes. Keep
        # the latest plus one prior recovery point by default; operators with
        # a larger storage budget can raise the count explicitly.
        retention_count = _positive_int_env('TOFU_SQLITE_SNAPSHOT_RETENTION', 2)
    retention_count = max(1, int(retention_count))

    with _snapshot_lock(out_dir) as acquired:
        if not acquired:
            return {'ok': False, 'reason': 'snapshot_in_progress'}
        stamp = time.strftime('%Y%m%d_%H%M%S')
        final = out_dir / (
            f'{_SNAPSHOT_PREFIX}{stamp}-{os.getpid()}-'
            f'{uuid.uuid4().hex[:8]}{_SNAPSHOT_SUFFIX}')
        temp = out_dir / f'.{final.name}.tmp-{uuid.uuid4().hex}'
        try:
            # URI read-only mode guarantees this backup path can never mutate
            # the authority. VACUUM INTO itself creates the destination.
            uri = source.as_uri() + '?mode=ro'
            with allow_sqlite_driver_connection(
                    'read canonical SQLite for snapshot'):
                conn = sqlite3.connect(
                    uri, uri=True, timeout=30.0, isolation_level=None)
            try:
                conn.execute('PRAGMA busy_timeout=30000')
                conn.execute('VACUUM INTO ?', (str(temp),))
            finally:
                conn.close()
            ok, detail = _integrity_check(temp)
            if not ok:
                return {'ok': False, 'reason': f'integrity_check_failed: {detail}'}
            _fsync_file_and_dir(temp)
            os.replace(temp, final)
            _fsync_file_and_dir(final)
            pruned = _prune_snapshots(out_dir, retention_count, preserve=final)
            size_mb = round(final.stat().st_size / (1024 * 1024), 1)
            logger.info('[DB-Backup] SQLite snapshot verified: %s (%.1f MB)',
                        final, size_mb)
            try:
                from lib.log import audit_log
                audit_log('sqlite_snapshot', path=str(final), size_mb=size_mb,
                          pruned=pruned, retention_count=retention_count)
            except Exception as exc:
                logger.debug('[DB-Backup] snapshot audit_log failed: %s', exc)
            return {'ok': True, 'backend': 'sqlite', 'path': str(final),
                    'size_mb': size_mb, 'pruned': pruned, 'verified': True}
        except sqlite3.Error as exc:
            logger.error('[DB-Backup] SQLite snapshot failed: %s', exc,
                         exc_info=True)
            return {'ok': False, 'reason': f'sqlite_error: {exc}'}
        except OSError as exc:
            logger.error('[DB-Backup] SQLite snapshot filesystem failure: %s',
                         exc, exc_info=True)
            return {'ok': False, 'reason': f'filesystem_error: {exc}'}
        finally:
            try:
                temp.unlink()
            except FileNotFoundError as exc:
                logger.debug('[DB-Backup] No partial snapshot cleanup needed: %s',
                             exc)
                pass
            except OSError as exc:
                logger.warning('[DB-Backup] Could not remove partial snapshot %s: %s',
                               temp, exc)


def backup_database(**kwargs) -> dict:
    """Back up the active backend through one stable scheduler entry point."""
    from lib.database._core import _BACKEND
    if _BACKEND == 'sqlite':
        return backup_sqlite_database(**kwargs)
    if kwargs:
        return {'ok': False, 'reason': 'sqlite_options_on_pg'}
    from lib.database._pg_backup import backup_pg_database
    return backup_pg_database()


__all__ = ['backup_database', 'backup_sqlite_database']

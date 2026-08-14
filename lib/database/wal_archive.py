"""lib/database/wal_archive.py — Tier B WAL archive/restore shim (PITR).

Invoked by PostgreSQL as:
  archive_command = 'python -m lib.database.wal_archive archive %p %f'
  restore_command = 'python -m lib.database.wal_archive restore %f %p'

Design (see JOURNAL §7f/B2 — the FUSE-stall-safe rules):
  * ARCHIVE copies a COMPLETED, immutable WAL segment to
    ``$TOFU_DB_BACKUP_ROOT/wal/<seg>`` atomically (temp + os.replace).
  * Idempotent: a destination already present with a byte-matching size is
    treated as success (PG may legitimately retry the same segment).
  * HARD TIMEOUT (TOFU_DB_WAL_ARCHIVE_TIMEOUT, default 30s) via a watchdog
    thread. On timeout the shim returns NON-ZERO so PG RETAINS the segment and
    retries later — never a false "archived". A blocked DolphinFS write can
    therefore never wedge WAL recycling into reporting success.
  * A persistent fail counter surfaces "archiving fallen behind" past
    TOFU_DB_WAL_ARCHIVE_MAX_FAILS so the operator sees it BEFORE local pg_wal
    fills the disk (consistent with fs_keepalive's loud-but-can't-fix stance).

Exit codes: 0 = archived/restored (or idempotent no-op); non-zero = failure
(PG retains + retries for archive; PG treats as end-of-WAL for restore).
"""

import os
import shutil
import sys
import threading
import uuid

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 30
_FAILCOUNT_FILE = '.tofu_wal_archive_fails'


def _bounded_env_int(name, default, minimum, maximum):
    raw = getenv_compat(name, default=str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[WAL-Archive] invalid %s=%r; using %d',
                       name, raw, default)
        return default
    if value < minimum or value > maximum:
        clamped = min(max(value, minimum), maximum)
        logger.warning('[WAL-Archive] %s=%d outside [%d,%d]; clamped to %d',
                       name, value, minimum, maximum, clamped)
        return clamped
    return value


def _safe_segment_name(name):
    """PG supplies a basename via %f; never let a caller escape wal/."""
    return bool(name and name not in ('.', '..')
                and os.path.basename(name) == name
                and '/' not in name and '\\' not in name
                and '\x00' not in name)


def _wal_dir():
    """Resolve $TOFU_DB_BACKUP_ROOT/wal (created on demand)."""
    from lib.database.db_paths import resolve_backup_root
    from lib.runtime_paths import data_root
    backup_root = resolve_backup_root(data_root())
    wal = os.path.join(backup_root, 'wal')
    os.makedirs(wal, exist_ok=True)
    return wal


def _copy_with_timeout(src, dst, timeout_s):
    """Copy src→dst atomically within timeout_s. Returns True on verified copy.

    The copy runs in a watchdog sub-thread; if it does not complete within
    timeout_s (a stalled FUSE write) we return False WITHOUT blocking the
    caller indefinitely — the caller then returns non-zero so PG retains the
    segment. The orphaned thread is a daemon; the temp file is cleaned on the
    next attempt (idempotent temp name per pid).
    """
    result = {'ok': False, 'err': None}
    tmp = dst + '.tmp.%d.%s' % (os.getpid(), uuid.uuid4().hex)

    def _do_copy():
        try:
            shutil.copyfile(src, tmp)
            if os.path.getsize(tmp) != os.path.getsize(src):
                result['err'] = 'size mismatch after copy'
                return
            # Returning success lets PostgreSQL recycle the source WAL.  The
            # archive copy must therefore cross a real durability boundary,
            # not merely reach the kernel/FUSE page cache.
            with open(tmp, 'rb') as copied:
                os.fsync(copied.fileno())
            os.replace(tmp, dst)   # atomic publish
            try:
                directory_fd = os.open(os.path.dirname(dst), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                logger.debug('[WAL-Archive] directory fsync unsupported: %s', exc)
            result['ok'] = True
        except Exception as e:  # noqa: BLE001 — reported via result, logged by caller
            logger.debug('[WAL-Archive] copy %s→%s failed in worker: %s', src, dst, e)
            result['err'] = str(e)

    t = threading.Thread(target=_do_copy, daemon=True, name='wal-archive-copy')
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return False, 'timeout after %.0fs (FUSE stall?)' % timeout_s
    return result['ok'], result['err']


def _bump_failcount(wal_dir, reset=False):
    path = os.path.join(wal_dir, _FAILCOUNT_FILE)
    if reset:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.debug('[WAL-Archive] could not reset failcount: %s', e)
        return 0
    n = 0
    try:
        if os.path.exists(path):
            with open(path) as f:
                n = int((f.read() or '0').strip() or '0')
    except (OSError, ValueError) as e:
        logger.debug('[WAL-Archive] failcount read failed: %s', e)
    n += 1
    try:
        with open(path, 'w') as f:
            f.write(str(n))
    except OSError as e:
        logger.debug('[WAL-Archive] failcount write failed: %s', e)
    return n


def archive_segment(src_path, seg_name):
    """Archive one completed WAL segment. Returns process exit code (0 ok)."""
    if not _safe_segment_name(seg_name):
        logger.critical('[WAL-Archive] refusing unsafe segment name %r', seg_name)
        return 1
    timeout_s = _bounded_env_int(
        'TOFU_DB_WAL_ARCHIVE_TIMEOUT', _DEFAULT_TIMEOUT_S, 1, 600)
    max_fails = _bounded_env_int(
        'TOFU_DB_WAL_ARCHIVE_MAX_FAILS', 10, 1, 10_000)
    try:
        wal_dir = _wal_dir()
    except Exception as e:
        logger.critical('[WAL-Archive] cannot resolve WAL dir: %s — returning '
                        'non-zero so PG RETAINS %s', e, seg_name)
        return 1
    dst = os.path.join(wal_dir, seg_name)

    # Idempotent: already archived with matching size → success.
    try:
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src_path):
            logger.debug('[WAL-Archive] %s already present (idempotent ok)', seg_name)
            _bump_failcount(wal_dir, reset=True)
            return 0
    except OSError as e:
        logger.debug('[WAL-Archive] idempotency probe failed: %s', e)

    ok, err = _copy_with_timeout(src_path, dst, timeout_s)
    if ok:
        _bump_failcount(wal_dir, reset=True)
        logger.debug('[WAL-Archive] archived %s', seg_name)
        return 0

    fails = _bump_failcount(wal_dir)
    # Non-zero → PG RETAINS the segment and retries. Never a false success.
    logger.critical('[WAL-Archive] FAILED to archive %s (%s) — returning non-zero '
                    'so PG retains+retries (fail_streak=%d). WAL is accumulating '
                    'on local disk.', seg_name, err, fails)
    if fails >= max_fails:
        logger.critical('[WAL-Archive] archiving has fallen behind '
                        '(%d consecutive fails ≥ %d) — DolphinFS archive target '
                        'may be stalled; RPO is degrading toward the last base '
                        'backup and local pg_wal will grow until the mount '
                        'recovers.', fails, max_fails)
    return 1


def restore_segment(seg_name, dst_path):
    """Restore one WAL segment for PITR replay. Returns exit code (0 ok)."""
    if not _safe_segment_name(seg_name):
        logger.warning('[WAL-Restore] refusing unsafe segment name %r', seg_name)
        return 1
    try:
        wal_dir = _wal_dir()
    except Exception as e:
        logger.warning('[WAL-Restore] cannot resolve WAL dir: %s', e)
        return 1
    src = os.path.join(wal_dir, seg_name)
    if not os.path.exists(src):
        # Normal end-of-archive signal to PG (not an error): no more segments.
        logger.debug('[WAL-Restore] %s not in archive (end of WAL)', seg_name)
        return 1
    try:
        shutil.copyfile(src, dst_path)
        return 0
    except Exception as e:
        logger.warning('[WAL-Restore] failed to restore %s: %s', seg_name, e)
        return 1


def main(argv):
    if len(argv) < 2:
        logger.error('[WAL-Archive] usage: wal_archive <archive|restore> ...')
        return 2
    op = argv[1]
    if op == 'archive' and len(argv) >= 4:
        return archive_segment(argv[2], argv[3])
    if op == 'restore' and len(argv) >= 4:
        return restore_segment(argv[2], argv[3])
    logger.error('[WAL-Archive] bad args: %r', argv)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))

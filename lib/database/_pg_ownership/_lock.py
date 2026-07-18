"""Cross-host startup lock (anti-concurrent-pg_ctl race guard).

Owns the ``_startup_lock_fd`` module global (rebound by
``_try_acquire_startup_lock`` / ``_release_startup_lock``), its mutex
``_startup_lock_mu``, and the ``_STARTUP_LOCK_FILE`` name. All accessors of
``_startup_lock_fd`` live HERE so no ``global`` mutation straddles a module
boundary.

Because the pg_* test suite sets the lock fd on the PACKAGE facade
(``pg_ownership._startup_lock_fd = None`` in a forked child), the canonical
copy is kept on the ``lib.database._pg_ownership`` facade module and every
accessor here reads/writes it THROUGH the facade (``_pkg._startup_lock_fd`` /
``_pkg._startup_lock_mu``). The module-level definitions below seed the
facade's initial values at package import.

When two tofu hosts share the same FUSE-mounted pgdata, both can
simultaneously conclude "PG is down, I'll start it" and race to ``pg_ctl
start``. Each new postmaster sees the OTHER host's PID in ``postmaster.pid``,
immediate-shutdowns with "lock file contains wrong PID", and leaves truncated
WAL records. The heartbeat alone can't prevent this: it only defends
sequential handoff (A dies → B takes over), not concurrent startup within the
same 60–120s window. The advisory POSIX ``flock()`` on a lock file INSIDE
pgdata is the fix — visible to every host that could race with us.
"""

import os
import threading

from lib.compat import IS_WINDOWS
from lib.log import get_logger

logger = get_logger(__name__)


_STARTUP_LOCK_FILE = '.tofu_pg_start.lock'
_startup_lock_fd = None         # Lock fd, retained for process life (facade-canonical)
_startup_lock_mu = threading.Lock()


def _startup_lock_path(pgdata):
    return os.path.join(pgdata, _STARTUP_LOCK_FILE)


def _try_acquire_startup_lock(pgdata):
    """Try to acquire an exclusive cross-host lock on the pgdata startup lock.

    Returns:
        True  — lock held (or flock unsupported / degraded to no-op).
        False — another host holds the lock; caller MUST NOT call pg_ctl start.
    """
    import lib.database._pg_ownership as _pkg
    with _pkg._startup_lock_mu:
        if _pkg._startup_lock_fd is not None:
            # Already held by this process.
            return True

        path = _startup_lock_path(pgdata)
        try:
            os.makedirs(pgdata, exist_ok=True)
        except OSError as e:
            logger.warning('[DB] Could not ensure pgdata exists for startup lock: %s', e)
            return True  # Degrade to pre-fix behavior: let the caller try.

        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            logger.warning('[DB] Could not open startup-lock file %s: %s — '
                           'proceeding without cross-host lock', path, e)
            return True

        if IS_WINDOWS:
            # No portable fcntl.flock on Windows. Windows FUSE shares are
            # rare for pgdata, so we degrade to no-op rather than ship a
            # half-reliable msvcrt.locking code path.
            _pkg._startup_lock_fd = fd
            logger.debug('[DB] Startup lock: Windows — no flock, acquired fd=%d '
                         'as a no-op placeholder', fd)
            return True

        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Non-Linux/macOS POSIX without fcntl — degrade to no-op.
            _pkg._startup_lock_fd = fd
            logger.debug('[DB] Startup lock: fcntl unavailable — degraded to no-op')
            return True
        except (OSError, IOError) as e:
            # Two possible causes:
            #   1. EWOULDBLOCK — another process (possibly on another
            #      host) already holds it. Treat as concurrent-start.
            #   2. ENOLCK / EOPNOTSUPP — FUSE backend doesn't implement
            #      advisory locks. Degrade to no-op with a warning so the
            #      pre-fix behavior is preserved on unsupported backends.
            import errno as _errno
            err_code = getattr(e, 'errno', None)
            if err_code in (_errno.EWOULDBLOCK, _errno.EAGAIN, _errno.EACCES):
                logger.warning('[DB] Startup lock HELD by another process/host '
                               '(pgdata=%s): %s — skipping our pg_ctl start to '
                               'avoid WAL race', pgdata, e)
                try:
                    os.close(fd)
                except OSError as _ce:
                    logger.debug('[DB] Close after lock-contention failed: %s', _ce)
                try:
                    from lib.log import audit_log as _audit
                    _audit('pg_concurrent_start_detected',
                           pgdata=pgdata, our_host=_pkg._get_local_ip(),
                           err=str(e)[:200])
                except Exception as _audit_err:
                    logger.debug('[DB] audit_log for pg_concurrent_start_detected '
                                 'failed: %s', _audit_err)
                return False
            # flock not supported by this FS — keep behavior, log loudly once.
            logger.warning('[DB] flock() on %s not supported by filesystem '
                           '(errno=%s: %s) — cross-host race guard DISABLED. '
                           'Multiple tofu hosts sharing this pgdata may '
                           'corrupt WAL. Mount pgdata on a filesystem that '
                           'supports POSIX advisory locks (ext4/xfs/NFSv4/'
                           'most FUSE) to re-enable.',
                           path, err_code, e)
            _pkg._startup_lock_fd = fd
            return True

        _pkg._startup_lock_fd = fd
        logger.info('[DB] Acquired cross-host startup lock on %s', path)
        return True


def _release_startup_lock():
    """Release the startup lock if held. Safe to call multiple times."""
    import lib.database._pg_ownership as _pkg
    with _pkg._startup_lock_mu:
        fd = _pkg._startup_lock_fd
        _pkg._startup_lock_fd = None
    if fd is None:
        return
    try:
        if not IS_WINDOWS:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError) as e:
                logger.debug('[DB] flock release raised (harmless): %s', e)
    finally:
        try:
            os.close(fd)
        except OSError as e:
            logger.debug('[DB] Close of startup lock fd failed: %s', e)

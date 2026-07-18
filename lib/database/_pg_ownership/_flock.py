"""Flock-enforcement probe + policy.

Owns the ``_flock_enforced`` module global (rebound by
``_probe_flock_enforced``) and its mutex ``_flock_probe_mu``. All accessors of
``_flock_enforced`` live HERE.

Because the pg_* test suite sets the cached verdict on the PACKAGE facade
(``pg_ownership._flock_enforced = False``), the canonical copy is kept on the
``lib.database._pg_ownership`` facade module and every accessor here
reads/writes it THROUGH the facade (``_pkg._flock_enforced`` /
``_pkg._flock_probe_mu``). The module-level definitions below seed the
facade's initial values at package import.

``_probe_flock_enforced`` actively verifies the pgdata mount truly ENFORCES
POSIX advisory locks (some FUSE backends silently treat every ``flock`` as a
no-op, which would let two hosts both "hold" the startup lock). The probe
result is cached for the process. ``_verify_flock_support_or_warn`` surfaces
the verdict per ``_flock_required`` policy — resolving ``_probe_flock_enforced``
through the facade so a test that patches that name on the package is honoured.
"""

import os
import threading

from lib.compat import IS_WINDOWS
from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# Cached flock-enforcement verdict for the pgdata mount, set once per process
# by _probe_flock_enforced(): True = real advisory locks, False = silent no-op
# / unsupported, None = not yet probed. Canonical copy lives on the facade.
_flock_enforced = None
_flock_probe_mu = threading.Lock()


def _probe_flock_enforced(pgdata):
    """Actively verify the pgdata mount truly ENFORCES POSIX advisory locks.

    The plain ``flock(LOCK_EX)`` call in ``_try_acquire_startup_lock`` only
    catches filesystems that FAIL locking with an errno (ENOLCK/EOPNOTSUPP).
    The dangerous case is a filesystem that silently treats every flock as a
    NO-OP: the call "succeeds", so two hosts both believe they hold the lock
    and start postmasters on the same pgdata → WAL/pg_subtrans corruption.

    This probe opens the SAME file twice (two independent open-file
    descriptions) and confirms that holding LOCK_EX on the first BLOCKS a
    LOCK_EX|LOCK_NB on the second. On a real locking FS the second call raises
    EWOULDBLOCK; a no-op FS grants both. Result is cached for the process.

    Returns:
        True  — advisory locks are genuinely enforced on this mount.
        False — locks are a silent no-op or unsupported (cross-host guard is
                NOT reliable here).
        None  — could not determine (probe itself errored unexpectedly); the
                caller should treat this conservatively, like False, for
                warning/refusal purposes.
    """
    import lib.database._pg_ownership as _pkg
    with _pkg._flock_probe_mu:
        if _pkg._flock_enforced is not None:
            return _pkg._flock_enforced
        if IS_WINDOWS:
            _pkg._flock_enforced = False
            return False
        try:
            import fcntl
        except ImportError as e:
            logger.debug('[PgOwnership] fcntl unavailable, disabling flock fallback: %s', e)
            _pkg._flock_enforced = False
            return False

        probe_path = os.path.join(pgdata, '.tofu_flock_probe')
        fd1 = fd2 = None
        try:
            os.makedirs(pgdata, exist_ok=True)
            fd1 = os.open(probe_path, os.O_CREAT | os.O_RDWR, 0o644)
            fd2 = os.open(probe_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                import errno as _errno
                if getattr(e, 'errno', None) in (_errno.EWOULDBLOCK, _errno.EAGAIN):
                    _pkg._flock_enforced = True  # second lock correctly blocked
                else:
                    # Unexpected errno — be conservative and call it unenforced.
                    logger.debug('[DB] flock probe second-lock errno=%s: %s',
                                 getattr(e, 'errno', None), e)
                    _pkg._flock_enforced = False
            else:
                # Second LOCK_EX succeeded while the first was held → no-op FS.
                _pkg._flock_enforced = False
            return _pkg._flock_enforced
        except OSError as e:
            logger.warning('[DB] flock-enforcement probe could not run on %s: %s '
                           '— cannot confirm cross-host locking', pgdata, e)
            _pkg._flock_enforced = None
            return None
        finally:
            for _fd in (fd1, fd2):
                if _fd is not None:
                    try:
                        fcntl.flock(_fd, fcntl.LOCK_UN)
                    except OSError as _e:
                        logger.debug('[DB] flock probe unlock failed: %s', _e)
                    try:
                        os.close(_fd)
                    except OSError as _e:
                        logger.debug('[DB] flock probe close failed: %s', _e)
            try:
                os.remove(probe_path)
            except OSError as _e:
                logger.debug('[DB] flock probe cleanup failed: %s', _e)


def _flock_required():
    """Policy: should PG be REFUSED when advisory locks aren't enforced?

    Driven by env ``TOFU_PG_REQUIRE_FLOCK`` (legacy ``CHATUI_PG_REQUIRE_FLOCK``
    also honored). Truthy (1/true/yes/refuse) → refuse PG on a non-locking
    shared mount rather than risk a double-start. Default false → warn loudly
    but proceed (single-host deployments must not be regressed).
    """
    raw = getenv_compat('TOFU_PG_REQUIRE_FLOCK') or ''
    return raw.strip().lower() in ('1', 'true', 'yes', 'on', 'refuse', 'require')


def _verify_flock_support_or_warn(pgdata):
    """Probe flock enforcement once and surface the result loudly.

    Returns:
        True  — safe to proceed (locks enforced, OR not enforced but policy
                allows proceeding with a loud warning).
        False — caller MUST refuse to bootstrap/start PG (locks not enforced
                AND TOFU_PG_REQUIRE_FLOCK demands enforcement).
    """
    import lib.database._pg_ownership as _pkg
    enforced = _pkg._probe_flock_enforced(pgdata)
    if enforced is True:
        logger.info('[DB] Advisory-lock enforcement verified on pgdata mount %s '
                    '— cross-host startup interlock is reliable.', pgdata)
        return True

    state = 'a SILENT NO-OP' if enforced is False else 'UNVERIFIABLE'
    if _pkg._flock_required():
        logger.critical(
            '[DB] REFUSING PostgreSQL: advisory locks on pgdata mount %s are %s, '
            'so the cross-host startup interlock CANNOT prevent two hosts from '
            'starting postmasters on the same data dir (WAL/pg_subtrans '
            'corruption). TOFU_PG_REQUIRE_FLOCK is set — falling back to SQLite. '
            'Mount pgdata on a filesystem with working POSIX advisory locks '
            '(ext4/xfs/NFSv4/most FUSE) to use PG here.', pgdata, state)
        try:
            from lib.log import audit_log as _audit
            _audit('pg_flock_unenforced_refused', pgdata=pgdata,
                   enforced=enforced, host=_pkg._get_local_ip())
        except Exception as _e:
            logger.debug('[DB] audit_log for pg_flock_unenforced_refused failed: %s', _e)
        return False

    logger.warning(
        '[DB] ⚠️  Advisory locks on pgdata mount %s are %s. The cross-host '
        'startup interlock degrades to heuristics here: if you run TWO tofu '
        'hosts against this SAME pgdata, a double postmaster start can corrupt '
        'WAL/pg_subtrans. This is safe for a SINGLE-host deployment. To make '
        'this fatal instead, set TOFU_PG_REQUIRE_FLOCK=1. To fix, mount pgdata '
        'on a filesystem with working POSIX advisory locks.', pgdata, state)
    try:
        from lib.log import audit_log as _audit
        _audit('pg_flock_unenforced_warning', pgdata=pgdata,
               enforced=enforced, host=_pkg._get_local_ip())
    except Exception as _e:
        logger.debug('[DB] audit_log for pg_flock_unenforced_warning failed: %s', _e)
    return True

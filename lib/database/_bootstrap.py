"""PostgreSQL server management — auto-bootstrap, start, stop, remote discovery.

Extracted from _core.py for modularity. Called from _core at import time.
Cross-platform: works on Linux, macOS, and Windows.
"""

import getpass
import json
import os
import shutil
import subprocess
import threading
import time

from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
from lib.env_compat import getenv_compat
from lib.log import get_logger, log_context

logger = get_logger(__name__)


# Tracks whether THIS process owns (is responsible for) a locally-running
# PG server. Set to True whenever _ensure_pg_running either starts PG via
# pg_ctl or attaches to an already-running local PG that uses our pgdata
# (which was almost certainly started by a prior invocation of server.py
# from this same project). Consumed by shutdown_pool() in _core.py to
# decide whether to call _stop_pg() on exit.
#
# NEVER set when we connect to a REMOTE PG (is_explicit_external, or the
# Step 3 "defer to remote" branch) — that PG belongs to someone else.
_PG_STARTED_BY_US = False


# ─────────────────────────────────────────────────────────────────────
#  Tofu-level heartbeat
#
#  The shared FUSE-mounted pgdata is occasionally inherited from a
#  previous host that didn't shut down cleanly: its postmaster may still
#  be TCP-reachable, but no tofu process there is actively using it.
#  A new server.py on this host would otherwise read .pg_owner_host,
#  see the remote PG answers, and route every DB call across the
#  network — only to time out when the abandoned remote eventually
#  drops or stalls.
#
#  The heartbeat file (`pgdata/.tofu_heartbeat`) is written by the
#  process that actually owns the local PG, refreshed every
#  _HEARTBEAT_REFRESH_S seconds, and cleared on clean shutdown. A new
#  startup considers the previous owner alive iff the heartbeat is
#  fresher than _HEARTBEAT_TTL_S. Otherwise it auto-heals: clears the
#  ownership markers and starts PG locally.
# ─────────────────────────────────────────────────────────────────────

_HEARTBEAT_FILE = '.tofu_heartbeat'
_HEARTBEAT_TTL_S = 120
_HEARTBEAT_REFRESH_S = 30

_heartbeat_thread = None
_heartbeat_stop_event = threading.Event()
_heartbeat_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────
#  Cross-host startup lock (anti-concurrent-pg_ctl race guard)
#
#  When two tofu hosts share the same FUSE-mounted pgdata, both can
#  simultaneously conclude "PG is down, I'll start it" and race to
#  pg_ctl start. Each new postmaster sees the OTHER host's PID in
#  postmaster.pid, immediate-shutdowns with "lock file contains wrong
#  PID", and leaves truncated WAL records — after which every further
#  startup on either side PANICs with "could not locate a valid
#  checkpoint record". The heartbeat alone can't prevent this: it only
#  defends sequential handoff (A dies → B takes over), not concurrent
#  startup within the same 60–120s window.
#
#  The fix: use an advisory POSIX flock() on a lock file INSIDE pgdata.
#  Since pgdata is the shared FUSE mount, the lock is visible to every
#  host that could race with us. The lock is acquired BEFORE any
#  pg_ctl start call and held for the process's lifetime via a retained
#  file descriptor — a process crash releases it, but no graceful exit
#  is needed. If another host already holds it, we abort the start
#  attempt and let the caller fall back to SQLite (or retry next cycle).
#
#  flock() over FUSE is best-effort; if the backend doesn't support it
#  we get IOError/OSError at acquire time and we LOG but do NOT skip
#  the start — we preserve the pre-fix behavior so hosts without FUSE
#  flock support aren't newly regressed. Audit-log emits a signal so
#  multi-host collisions are easy to grep from outside.
# ─────────────────────────────────────────────────────────────────────

_STARTUP_LOCK_FILE = '.tofu_pg_start.lock'
_startup_lock_fd = None         # Lock fd, retained for process life
_startup_lock_mu = threading.Lock()


def _startup_lock_path(pgdata):
    return os.path.join(pgdata, _STARTUP_LOCK_FILE)


def _try_acquire_startup_lock(pgdata):
    """Try to acquire an exclusive cross-host lock on the pgdata startup lock.

    Returns:
        True  — lock held (or flock unsupported / degraded to no-op).
        False — another host holds the lock; caller MUST NOT call pg_ctl start.
    """
    global _startup_lock_fd
    with _startup_lock_mu:
        if _startup_lock_fd is not None:
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
            _startup_lock_fd = fd
            logger.debug('[DB] Startup lock: Windows — no flock, acquired fd=%d '
                         'as a no-op placeholder', fd)
            return True

        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Non-Linux/macOS POSIX without fcntl — degrade to no-op.
            _startup_lock_fd = fd
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
                           pgdata=pgdata, our_host=_get_local_ip(),
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
            _startup_lock_fd = fd
            return True

        _startup_lock_fd = fd
        logger.info('[DB] Acquired cross-host startup lock on %s', path)
        return True


def _release_startup_lock():
    """Release the startup lock if held. Safe to call multiple times."""
    global _startup_lock_fd
    with _startup_lock_mu:
        fd = _startup_lock_fd
        _startup_lock_fd = None
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


# Cached flock-enforcement verdict for the pgdata mount, set once per process
# by _probe_flock_enforced(): True = real advisory locks, False = silent no-op
# / unsupported, None = not yet probed.
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
    global _flock_enforced
    with _flock_probe_mu:
        if _flock_enforced is not None:
            return _flock_enforced
        if IS_WINDOWS:
            _flock_enforced = False
            return False
        try:
            import fcntl
        except ImportError:
            _flock_enforced = False
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
                    _flock_enforced = True  # second lock correctly blocked
                else:
                    # Unexpected errno — be conservative and call it unenforced.
                    logger.debug('[DB] flock probe second-lock errno=%s: %s',
                                 getattr(e, 'errno', None), e)
                    _flock_enforced = False
            else:
                # Second LOCK_EX succeeded while the first was held → no-op FS.
                _flock_enforced = False
            return _flock_enforced
        except OSError as e:
            logger.warning('[DB] flock-enforcement probe could not run on %s: %s '
                           '— cannot confirm cross-host locking', pgdata, e)
            _flock_enforced = None
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
    enforced = _probe_flock_enforced(pgdata)
    if enforced is True:
        logger.info('[DB] Advisory-lock enforcement verified on pgdata mount %s '
                    '— cross-host startup interlock is reliable.', pgdata)
        return True

    state = 'a SILENT NO-OP' if enforced is False else 'UNVERIFIABLE'
    if _flock_required():
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
                   enforced=enforced, host=_get_local_ip())
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
               enforced=enforced, host=_get_local_ip())
    except Exception as _e:
        logger.debug('[DB] audit_log for pg_flock_unenforced_warning failed: %s', _e)
    return True


def _heartbeat_path(pgdata):
    return os.path.join(pgdata, _HEARTBEAT_FILE)


def _read_heartbeat(pgdata):
    """Return parsed heartbeat dict ({host, pid, ts}) or None."""
    path = _heartbeat_path(pgdata)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.debug('[DB] Heartbeat at %s is not a dict', path)
        return None
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _read_heartbeat caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[DB] Could not read heartbeat at %s: %s', path, e)
        return None


def _heartbeat_is_fresh(pgdata, ttl_s=_HEARTBEAT_TTL_S):
    """Return (fresh, info_dict) — fresh=True if heartbeat exists and is
    within ttl_s seconds.

    info_dict carries {host, pid, ts, age_s} when the file is present
    (regardless of freshness) so the caller can log a useful message.
    """
    path = _heartbeat_path(pgdata)
    try:
        st = os.stat(path)
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _heartbeat_is_fresh caught %s: %s', type(_e_audit).__name__, _e_audit)
        return False, None
    except OSError as e:
        logger.debug('[DB] stat heartbeat failed: %s', e)
        return False, None

    age_s = time.time() - st.st_mtime
    info = _read_heartbeat(pgdata) or {}
    info = dict(info)
    info['age_s'] = age_s
    return age_s <= ttl_s, info


def _write_heartbeat(pgdata):
    """Write/refresh the heartbeat file. Best-effort."""
    payload = {
        'host': _get_local_ip(),
        'pid': os.getpid(),
        'ts': time.time(),
    }
    path = _heartbeat_path(pgdata)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug('[DB] Could not write heartbeat to %s: %s', path, e)


def _clear_heartbeat(pgdata):
    path = _heartbeat_path(pgdata)
    try:
        os.remove(path)
        logger.debug('[DB] Cleared heartbeat %s', path)
    except FileNotFoundError as _e_audit:
        logger.debug('[_bootstrap] _clear_heartbeat caught %s: %s', type(_e_audit).__name__, _e_audit)
    except OSError as e:
        logger.debug('[DB] Could not clear heartbeat at %s: %s', path, e)


def _heartbeat_loop(pgdata):
    logger.info('[DB] Heartbeat thread started (pgdata=%s, refresh=%ds, ttl=%ds)',
                pgdata, _HEARTBEAT_REFRESH_S, _HEARTBEAT_TTL_S)
    while not _heartbeat_stop_event.is_set():
        try:
            _write_heartbeat(pgdata)
        except Exception as e:
            logger.warning('[DB] Heartbeat refresh failed: %s', e)
        if _heartbeat_stop_event.wait(_HEARTBEAT_REFRESH_S):
            break
    logger.info('[DB] Heartbeat thread stopped')


def _start_heartbeat_thread(pgdata):
    """Start the heartbeat refresher (idempotent)."""
    global _heartbeat_thread
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_stop_event.clear()
        _write_heartbeat(pgdata)  # immediate first write
        t = threading.Thread(
            target=_heartbeat_loop, args=(pgdata,),
            name='tofu-pg-heartbeat', daemon=True,
        )
        t.start()
        _heartbeat_thread = t


def stop_heartbeat(pgdata=None):
    """Stop the heartbeat refresher and (optionally) clear the file.

    Called from server.py's clean-shutdown hook via _core.stop_local_pg_if_owned.
    """
    global _heartbeat_thread
    with _heartbeat_lock:
        _heartbeat_stop_event.set()
        t = _heartbeat_thread
        _heartbeat_thread = None
    if t is not None and t.is_alive():
        try:
            t.join(timeout=5)
        except Exception as e:
            logger.debug('[DB] Heartbeat thread join failed: %s', e)
    if pgdata is not None:
        _clear_heartbeat(pgdata)


# ─────────────────────────────────────────────────────────────────────
#  Instance-identity stamp — the copy/move detector
#
#  Ownership markers (`.pg_owner_host`, `.tofu_heartbeat`, `postmaster.pid`)
#  live INSIDE pgdata, which is exactly the directory people copy. When the
#  whole project is copied to a NEW path (a colleague's home, `tofu-meituan2`,
#  an open-source clone) the markers come along and the fresh instance trusts
#  them — silently routing every DB call back to the ORIGINAL machine's PG
#  via FUSE cross-machine discovery. Result: shared data, privacy leak, no
#  error shown.
#
#  The discriminator is the pgdata's own ABSOLUTE PATH. Legitimate cross-host
#  sharing happens at the SAME mount path (two machines, one FUSE pgdata);
#  a copy lands at a DIFFERENT path. So we stamp the canonical path (plus a
#  random instance id + creation ts) into `.pg_instance_id` whenever this
#  process takes local ownership. On a later startup, if the stamped path no
#  longer matches the current canonical path, the directory was copied/moved
#  → we ignore ALL inherited ownership markers and take over locally. This
#  strengthens — never weakens — the same-path multi-host failover logic,
#  which continues to rely on `.pg_owner_host` + heartbeat.
# ─────────────────────────────────────────────────────────────────────

_INSTANCE_ID_FILE = '.pg_instance_id'


def _instance_id_path(pgdata):
    return os.path.join(pgdata, _INSTANCE_ID_FILE)


def _canonical_pgdata_path(pgdata):
    """Return a stable canonical key for a pgdata location.

    Uses ``os.path.realpath`` (resolves symlinks + ``..``) so the same
    physical directory always produces the same string regardless of how
    it was addressed. Returns the input unchanged on error.
    """
    try:
        return os.path.realpath(pgdata)
    except OSError as e:
        logger.debug('[DB] realpath(%s) failed: %s', pgdata, e)
        return pgdata


def _read_instance_stamp(pgdata):
    """Return the parsed `.pg_instance_id` dict, or None if absent/invalid."""
    path = _instance_id_path(pgdata)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('path'):
            return data
        logger.debug('[DB] instance stamp at %s malformed: %r', path, data)
        return None
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[DB] Could not read instance stamp at %s: %s', path, e)
        return None


def _write_instance_stamp(pgdata):
    """Stamp this pgdata with its current canonical path + a fresh id.

    Idempotent for the path: if a stamp already exists for the SAME
    canonical path, the existing id/created are preserved (we only rewrite
    when the path differs or no stamp exists). Best-effort — failures are
    logged at debug and never abort startup.
    """
    import uuid
    canon = _canonical_pgdata_path(pgdata)
    existing = _read_instance_stamp(pgdata)
    if existing and _canonical_pgdata_path(existing.get('path', '')) == canon:
        return  # already stamped for this path — keep stable id
    payload = {
        'path': canon,
        'id': (existing or {}).get('id') or uuid.uuid4().hex,
        'created': (existing or {}).get('created') or time.time(),
        'restamped': time.time() if existing else None,
    }
    path = _instance_id_path(pgdata)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        logger.info('[DB] Stamped pgdata instance identity: path=%s id=%s',
                    canon, payload['id'])
    except OSError as e:
        logger.debug('[DB] Could not write instance stamp to %s: %s', path, e)


def _pgdata_was_copied(pgdata):
    """Return (was_copied, stamped_path) for this pgdata.

    ``was_copied`` is True only when a stamp EXISTS and its recorded
    canonical path differs from the current canonical path — i.e. the
    directory was copied or moved here from elsewhere. A missing stamp
    (legacy pgdata predating this mechanism, or a brand-new initdb) returns
    False so existing same-path multi-host behaviour is untouched; the stamp
    is written lazily the next time we take local ownership.
    """
    stamp = _read_instance_stamp(pgdata)
    if not stamp:
        return False, None
    stamped = _canonical_pgdata_path(stamp.get('path', ''))
    current = _canonical_pgdata_path(pgdata)
    if stamped and stamped != current:
        return True, stamped
    return False, stamped


def _clear_ownership_markers(pgdata, *, remove_pidfile=True, reason=''):
    """Remove the machine-specific ownership markers from a pgdata.

    Clears `.pg_owner_host`, the tofu heartbeat, and (optionally)
    `postmaster.pid` / `postmaster.opts`. The DATA files are never touched.
    Used by the copy/move self-heal and by the `reset-ownership` admin
    command. Best-effort; each failure is logged at warning level.
    """
    suffix = f' ({reason})' if reason else ''
    removed = []
    targets = ['.pg_owner_host', _OWNER_ID_FILE, _HEARTBEAT_FILE]
    if remove_pidfile:
        targets += ['postmaster.pid', 'postmaster.opts']
    for name in targets:
        p = os.path.join(pgdata, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(name)
        except OSError as e:
            logger.warning('[DB] Could not remove ownership marker %s%s: %s',
                           name, suffix, e)
    if removed:
        logger.info('[DB] Cleared ownership markers%s: %s', suffix, ', '.join(removed))
    return removed


def _heal_if_copied(pgdata):
    """If this pgdata was copied/moved here, drop inherited ownership markers.

    Returns True when a copy was detected and markers were cleared (the
    caller should treat the directory as freshly-owned-locally), False
    otherwise. The instance stamp is re-written for the new path by the
    subsequent ``_mark_pg_owned_locally`` call.
    """
    was_copied, stamped = _pgdata_was_copied(pgdata)
    if not was_copied:
        return False
    logger.warning('[DB] pgdata was COPIED/MOVED here (stamped path=%s, '
                   'current=%s) — ignoring inherited ownership markers and '
                   'taking over locally. This prevents silently connecting to '
                   "the original machine's PostgreSQL.",
                   stamped, _canonical_pgdata_path(pgdata))
    try:
        from lib.log import audit_log as _audit
        _audit('pg_copied_pgdata_self_heal',
               stamped_path=stamped,
               current_path=_canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for copy self-heal failed: %s', e)
    _clear_ownership_markers(pgdata, remove_pidfile=False, reason='copied pgdata')
    _clear_heartbeat(pgdata)
    return True


def _standalone_mode():
    """True when this deployment is a standalone single-machine copy.

    Set ``TOFU_PG_STANDALONE=1`` (``export.py`` seeds it into every exported
    ``.env``). In this mode we NEVER defer to a remote PG owner recorded in an
    inherited pgdata: such an ``.pg_owner_host`` / heartbeat comes from the
    machine the copy was made on — or a previous container sharing the same
    FUSE-mounted absolute path — NOT a live failover peer. We clear the
    inherited markers and own PG locally instead of routing every DB call
    across a dead cross-host link (the "connection ... timeout expired" crash).

    This deliberately disables same-path multi-host failover, which standalone
    deployments don't use. Same-path failover deployments must leave
    ``TOFU_PG_STANDALONE`` unset to keep the heartbeat handoff in
    ``_pg_already_running_on_another_machine`` Step 3.
    """
    return getenv_compat('TOFU_PG_STANDALONE', default='').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _heal_if_standalone_remote_owner(pgdata):
    """In standalone mode, drop an inherited REMOTE-owner marker so we never
    defer to another machine's PG.

    Complements ``_heal_if_copied``: that one heals when the pgdata was copied
    to a DIFFERENT absolute path. On shared FUSE storage every container sees
    the pgdata at the SAME absolute path, so the stamp matches and copy-detect
    can't fire — yet the ``.pg_owner_host`` still points at a stale peer. The
    explicit ``TOFU_PG_STANDALONE`` flag resolves that ambiguity in favour of
    "own it locally".

    Returns True when an inherited remote marker was cleared (caller should
    treat the directory as freshly owned locally), False otherwise.
    """
    if not _standalone_mode():
        return False
    # Stable-identity guard: if .pg_owner_id says this pgdata is ours, an IP
    # flap (owner_host != local_ip) is NOT an inherited remote marker — it's
    # our own pgdata under a new container IP. Never clear in that case.
    if _owner_is_self(pgdata) is True:
        return False
    owner_host = _read_pg_host_from_pidfile(pgdata)
    if not owner_host:
        return False
    local_ip = _get_local_ip()
    if owner_host in (local_ip, 'localhost', '127.0.0.1'):
        return False  # owner is this host — nothing inherited to heal
    # Owner is remote. IP-independent safety: if our pidfile PID is a LIVE
    # local postgres, THIS host already owns pgdata (the .pg_owner_host IP can
    # be stale after a container-IP flap) — never clobber our own postmaster.
    if _pidfile_pid_is_live_local_postgres(pgdata):
        return False
    logger.warning('[DB] TOFU_PG_STANDALONE set and pgdata carries a REMOTE '
                   'owner marker (owner_host=%s, local_ip=%s) — inherited from '
                   'another machine/container, not a failover peer. Clearing it '
                   'and owning PG locally.', owner_host, local_ip)
    try:
        from lib.log import audit_log as _audit
        _audit('pg_standalone_heal_remote_owner',
               owner_host=owner_host, local_ip=local_ip,
               pgdata=_canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for standalone heal failed: %s', e)
    _clear_ownership_markers(pgdata, remove_pidfile=False,
                             reason='standalone remote-owner marker')
    _clear_heartbeat(pgdata)
    return True


def _mark_pg_owned_locally(pgdata=None):
    """Record that this process is responsible for the local PG.

    When ``pgdata`` is provided, also starts the heartbeat refresher so
    other hosts (sharing the same FUSE-mounted pgdata) can tell that a
    tofu process is actively using this PG.
    """
    global _PG_STARTED_BY_US
    _PG_STARTED_BY_US = True
    if pgdata:
        _write_instance_stamp(pgdata)
        _start_heartbeat_thread(pgdata)


def is_pg_owned_locally():
    """Return True if this process started / took over a local PG server."""
    return _PG_STARTED_BY_US


def _find_pg_binary(name):
    """Locate a PostgreSQL binary by name, cross-platform.

    Uses ``shutil.which()`` which respects PATH on all platforms.
    On Windows, also checks common PostgreSQL install locations.

    Args:
        name: Binary name without extension (e.g. 'pg_ctl', 'initdb').

    Returns:
        Full path to the binary, or *name* itself if not found
        (so subprocess will raise FileNotFoundError with a clear message).
    """
    found = shutil.which(name)
    if found:
        return found
    # On macOS, try common Homebrew / MacPorts / Conda locations
    if IS_MACOS:
        mac_paths = [
            # Homebrew (Apple Silicon)
            '/opt/homebrew/bin',
            '/opt/homebrew/opt/postgresql/bin',
            # Homebrew (Intel)
            '/usr/local/bin',
            '/usr/local/opt/postgresql/bin',
            # MacPorts
            '/opt/local/bin',
            # Postgres.app
            '/Applications/Postgres.app/Contents/Versions/latest/bin',
        ]
        # Also check all Homebrew-versioned postgresql formulae
        for prefix in ['/opt/homebrew/opt', '/usr/local/opt']:
            for pg_ver in range(18, 12, -1):
                mac_paths.append(os.path.join(prefix, f'postgresql@{pg_ver}', 'bin'))
        # Check Conda envs — the user's active conda env and base
        conda_prefix = os.environ.get('CONDA_PREFIX', '')
        if conda_prefix:
            mac_paths.insert(0, os.path.join(conda_prefix, 'bin'))
        conda_base = os.environ.get('CONDA_PREFIX_1', '')  # base env when sub-env is active
        if conda_base:
            mac_paths.append(os.path.join(conda_base, 'bin'))
        for d in mac_paths:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # On Windows, try common PostgreSQL install paths
    if IS_WINDOWS:
        for pg_ver in range(18, 12, -1):
            candidate = os.path.join(
                os.environ.get('ProgramFiles', r'C:\Program Files'),
                'PostgreSQL', str(pg_ver), 'bin', f'{name}.exe'
            )
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # Return bare name — subprocess will raise FileNotFoundError
    return name


def _get_username(fallback='postgres'):
    """Get OS username cross-platform (Linux USER, Windows USERNAME)."""
    try:
        return getpass.getuser()
    except Exception as e:
        logger.debug('[DB] getuser() failed, using fallback %s: %s', fallback, e)
        return fallback


def _read_pg_host_from_pidfile(pgdata):
    """Read the PG owner host from .pg_owner_host on shared FUSE storage."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        if os.path.exists(owner_file):
            with open(owner_file) as f:
                host = f.read().strip()
            if host:
                return host
    except Exception as e:
        logger.debug('[DB] Could not read .pg_owner_host: %s', e)
    return None


def _pidfile_pid_is_live_local_postgres(pgdata):
    """Return True if postmaster.pid names a PID that is a live local postgres.

    This is the IP-independent ground truth for "is OUR machine already
    running PG on this pgdata". The `.pg_owner_host` marker is derived from
    `_get_local_ip()`, which flaps when the container's IP is reassigned
    (cloud-IDE network changes) — making a host mistake its OWN postmaster
    for a remote one. A PID liveness + name check does not depend on the IP,
    so we use it as a hard guard before deleting the pidfile or starting a
    second postmaster (concurrent access to one pgdata corrupts pg_subtrans).

    Returns False if the pidfile is absent/unparseable, the PID is dead, or
    the PID belongs to a non-postgres process (genuinely stale pidfile).
    """
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.readline().strip())
    except (FileNotFoundError, ValueError) as e:
        logger.debug('[DB] pidfile liveness: cannot read PID from %s: %s', pidfile, e)
        return False
    except OSError as e:
        logger.debug('[DB] pidfile liveness: stat/read error on %s: %s', pidfile, e)
        return False
    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            return False
        try:
            named = is_process_named(pid, 'postgres')
        except Exception as e:
            # Can't introspect the name (no /proc perms etc.) — be SAFE and
            # assume it IS our live postgres rather than risk a double-start.
            logger.warning('[DB] pidfile liveness: PID %d alive but name check '
                           'failed (%s) — assuming live postgres to avoid double-start', pid, e)
            return True
        if named:
            logger.info('[DB] pidfile liveness: PID %d is a LIVE local postgres '
                        '— this host already owns pgdata=%s', pid, pgdata)
            return True
        logger.info('[DB] pidfile liveness: PID %d alive but not postgres — stale pidfile', pid)
        return False
    except Exception as e:
        logger.warning('[DB] pidfile liveness check failed (%s) — assuming live '
                       'postgres to avoid double-start', e)
        return True


def _get_local_ip():
    """Get this machine's IP address (non-loopback)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as _e:
        logger.debug('[DB] UDP socket IP detection failed: %s', _e)
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception as _e2:
        logger.debug('[DB] gethostbyname fallback also failed: %s — returning 127.0.0.1', _e2)
        return '127.0.0.1'


_HOST_IDENTITY_CACHE = None
_OWNER_ID_FILE = '.pg_owner_id'


def _get_host_identity():
    """Return a STABLE per-host identity that does NOT flap when the container
    IP is reassigned (unlike ``_get_local_ip()``).

    ``_get_local_ip()`` uses a UDP-trick to 8.8.8.8 and returns whatever the
    container's current IP is. Cloud-IDE / FUSE deployments reassign that IP
    while the host stays the same, which made a server mistake its OWN pgdata
    for a remote one ("connection ... timeout expired" / split-brain). The
    machine-id (or hostname) stays constant across an IP flap but DIFFERS
    between genuinely different containers/hosts — exactly the semantics we
    want for "is this pgdata owned by THIS machine?".

    Order: ``TOFU_HOST_ID`` env override → ``/etc/machine-id`` →
    ``/var/lib/dbus/machine-id`` → ``socket.gethostname()``. Cached per process.
    """
    global _HOST_IDENTITY_CACHE
    if _HOST_IDENTITY_CACHE:
        return _HOST_IDENTITY_CACHE
    ident = (getenv_compat('TOFU_HOST_ID', default='') or '').strip()
    if not ident:
        for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                with open(p) as f:
                    ident = f.read().strip()
                if ident:
                    break
            except OSError as e:
                logger.debug('[DB] host-identity: could not read %s: %s', p, e)
    if not ident:
        try:
            import socket
            ident = socket.gethostname().strip()
        except Exception as e:
            logger.debug('[DB] host-identity: gethostname failed: %s', e)
            ident = ''
    _HOST_IDENTITY_CACHE = ident or 'unknown-host'
    return _HOST_IDENTITY_CACHE


def _owner_is_self(pgdata):
    """IP-independent ownership check using the stable ``.pg_owner_id`` marker.

    Returns:
        True  — the stored host-identity equals ours (we own this pgdata,
                regardless of any IP flap recorded in ``.pg_owner_host``).
        False — the stored identity is a DIFFERENT host.
        None  — no ``.pg_owner_id`` marker (legacy pgdata or never written);
                caller must fall back to the IP / live-PID heuristics.
    """
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        if os.path.exists(id_file):
            with open(id_file) as f:
                stored = f.read().strip()
            if stored:
                return stored == _get_host_identity()
    except OSError as e:
        logger.debug('[DB] Could not read %s: %s', _OWNER_ID_FILE, e)
    return None


def _write_owner_host(pgdata):
    """Write our IP to .pg_owner_host so other machines know where to connect,
    plus a stable host-identity to .pg_owner_id for IP-flap-proof self-check."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        ip = _get_local_ip()
        with open(owner_file, 'w') as f:
            f.write(ip)
        logger.info('[DB] Wrote PG owner host: %s (id=%s)', ip, _get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write .pg_owner_host: %s', e)
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        with open(id_file, 'w') as f:
            f.write(_get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write %s: %s', _OWNER_ID_FILE, e)


def _pg_already_running_on_another_machine(pgdata, pg_port):
    """Check if another machine owns the PG data directory.

    Returns:
        (True, host_ip) if another machine has PG running on this pgdata,
        (False, None) otherwise.
    """
    # Copy/move self-heal: if this pgdata was copied here from another path,
    # every inherited marker (owner_host, heartbeat, pidfile) belongs to the
    # ORIGINAL instance. Never defer to it — that is the "silently connect to
    # the source machine's PG" trap. Clear the markers and report no remote.
    if _heal_if_copied(pgdata):
        return False, None

    # Standalone single-machine copy: an inherited remote-owner marker (same
    # FUSE abs-path, different container/host) must not make us defer. Clear it
    # and own PG locally. No-op unless TOFU_PG_STANDALONE is set.
    if _heal_if_standalone_remote_owner(pgdata):
        return False, None

    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if not os.path.exists(pidfile):
        logger.debug('[DB] No postmaster.pid — PG not running')
        return False, None

    try:
        with open(pidfile) as f:
            lines = f.readlines()
        if len(lines) < 2:
            logger.debug('[DB] postmaster.pid too short (%d lines) — treating as absent', len(lines))
            return False, None
        pid = int(lines[0].strip())
    except Exception as e:
        logger.warning('[DB] Cannot parse postmaster.pid: %s', e)
        return False, None

    # IP-independent identity check FIRST: the stable .pg_owner_id marker
    # (machine-id / hostname) does not flap when the container IP is
    # reassigned, unlike .pg_owner_host. If it says this pgdata is ours, we
    # own it — no matter what IP the (possibly stale) .pg_owner_host records.
    owner_self = _owner_is_self(pgdata)
    if owner_self is True:
        logger.info('[DB] .pg_owner_id matches this host (id=%s) — pgdata is OURS '
                    '(ignoring any IP flap in .pg_owner_host)', _get_host_identity())
        return False, None

    owner_host = _read_pg_host_from_pidfile(pgdata)
    local_ip = _get_local_ip()
    is_remote_owner = (
        owner_host is not None
        and owner_host not in (local_ip, 'localhost', '127.0.0.1')
    )
    # A DIFFERENT-host identity marker is authoritative proof of remoteness
    # even if the flapping IPs happen to coincide.
    if owner_self is False:
        is_remote_owner = True

    logger.info('[DB] postmaster.pid: PID=%d, owner_host=%s, local_ip=%s, owner_self=%s, is_remote=%s',
                pid, owner_host, local_ip, owner_self, is_remote_owner)

    # IP-independent ground truth: if the pidfile PID is a live local
    # postgres, THIS host already owns pgdata — regardless of what the
    # `.pg_owner_host` IP marker says. _get_local_ip() flaps when the
    # container IP is reassigned, which previously made a host mistake its
    # OWN postmaster for a remote one, delete the pidfile, and start a
    # SECOND postmaster on the same pgdata → pg_subtrans corruption. Trust
    # the PID over the IP.
    if is_remote_owner and _pidfile_pid_is_live_local_postgres(pgdata):
        logger.warning('[DB] postmaster.pid PID=%d is a LIVE local postgres but '
                       'owner_host=%s != local_ip=%s — IP flap detected. Treating '
                       'as OURS (not remote) to avoid a double-start.',
                       pid, owner_host, local_ip)
        return False, None

    if is_remote_owner:
        # Use a real psycopg2 connect probe — pg_isready can give false
        # positives on "half-alive" containers (TCP accept works but real
        # queries hang) which is exactly the container-switch scenario on
        # shared FUSE storage.
        reachable = _pg_real_connect_ok(owner_host, pg_port, None, None, timeout_s=5)
        logger.info('[DB] PG owned by remote host %s (real_connect=%s) — deferring to it', owner_host, reachable)
        return True, owner_host

    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            raise ProcessLookupError(f'PID {pid} not alive')
        try:
            if is_process_named(pid, 'postgres'):
                logger.debug('[DB] PID %d is local postgres — already running', pid)
                return False, None
            else:
                logger.info('[DB] PID %d exists locally but is not postgres — stale pidfile', pid)
                return False, None
        except Exception as e:
            logger.warning('[DB] Cannot check PID %d command: %s — assuming stale', pid, e)
            return False, None
    except ProcessLookupError:
        logger.info('[DB] PID %d not found locally, owner=%s (us) — stale pidfile', pid, owner_host or 'unknown')
        return False, None
    except PermissionError:
        logger.info('[DB] Cannot signal PID %d (PermissionError) — assuming local PG running', pid)
        return False, None


def _find_free_port(start=15432, end=15500):
    """Find an available TCP port in [start, end) for PostgreSQL."""
    import socket
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('127.0.0.1', port))
            s.close()
            if result != 0:
                return port
        except Exception as e:
            logger.debug('[DB] Port %d probe error (assuming free): %s', port, e)
            return port
    logger.warning('[DB] No free port found in %d–%d, falling back to %d', start, end, start)
    return start


def _fix_unix_socket_conf(pgdata):
    """Patch postgresql.conf to disable Unix sockets if needed.

    Disables Unix sockets on:
      - FUSE filesystems (Linux: /mnt/ paths) — FUSE doesn't support AF_UNIX
      - Windows — Unix domain sockets are only partially supported
    On macOS with local disk, Unix sockets are fine — skip patching.
    """
    # Decide if we need to disable unix sockets
    if IS_WINDOWS:
        reason = 'Windows (Unix sockets not reliably supported)'
    elif IS_LINUX and pgdata.startswith('/mnt/'):
        reason = 'FUSE filesystem does not support Unix sockets'
    else:
        # macOS and Linux on local disk — Unix sockets are fine
        return

    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return
    try:
        with open(conf_path) as f:
            content = f.read()
        if "unix_socket_directories = ''" in content:
            return
        import re
        new_content, count = re.subn(
            r"unix_socket_directories\s*=\s*'[^']*'",
            "unix_socket_directories = ''",
            content
        )
        if count > 0:
            with open(conf_path, 'w') as f:
                f.write(new_content)
            logger.info('[DB] Patched postgresql.conf: disabled unix_socket_directories (%s)', reason)
    except Exception as e:
        logger.warning('[DB] Could not patch unix_socket_directories in postgresql.conf: %s', e)


# ─────────────────────────────────────────────────────────────────────
#  Managed PostgreSQL tuning block
#
#  Historically the durability + sizing settings (max_connections,
#  wal_level, fsync, …) were appended to postgresql.conf MANUALLY, once,
#  under a "# ── ChatUI Custom Config ──" header. Nothing in the codebase
#  maintained them, so:
#    • bumping the app-side TOFU_DB_MAX_CONNS did NOT raise PG's own
#      max_connections ceiling (a 1000-user deployment would still hit
#      PG "too many clients" at 200);
#    • durability settings could silently drift between deployments.
#
#  This function makes the config code-managed: every owned-PG startup
#  rewrites a single delimited block (idempotently). PG reads the LAST
#  occurrence of a setting in the file, so appending our block also
#  overrides any older manual entries above it.
# ─────────────────────────────────────────────────────────────────────

_MANAGED_BLOCK_BEGIN = '# ── Tofu managed config (auto-generated; do not edit) BEGIN ──'
_MANAGED_BLOCK_END = '# ── Tofu managed config END ──'

# PG server-side max_connections. Provisioned ABOVE the app-side semaphore
# ceiling (_MAX_TOTAL_CONNS, default 1000) so the application — not PG's
# hard FATAL limit — is always the binding constraint. The extra headroom
# absorbs superuser/maintenance/replication connections. Override via env.
_MANAGED_PG_MAX_CONNECTIONS = int(
    getenv_compat('TOFU_PG_MAX_CONNECTIONS', default='1100'))


def _build_managed_pg_config():
    """Return the body (settings only) of the managed postgresql.conf block.

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
    ]


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

    settings = _build_managed_pg_config()
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

    backup_dir = os.path.join(BASE_DIR, 'data', 'pg_backups')
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
    backup_dir = os.path.join(base_dir, 'data', 'pg_backups')
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


def _quarantine_corrupt_pgdata(pgdata):
    """Move a corrupt pgdata aside to pgdata.corrupt.<ts> so a fresh initdb can run.

    Returns the quarantine path on success, or None on failure (in which case
    the caller must NOT proceed to initdb over live data).
    """
    quarantine = f'{pgdata}.corrupt.{time.strftime("%Y%m%d_%H%M%S")}'
    try:
        os.rename(pgdata, quarantine)
        logger.warning('[DB-SelfHeal] Quarantined corrupt cluster: %s → %s',
                       pgdata, quarantine)
        return quarantine
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not quarantine corrupt pgdata %s: %s',
                     pgdata, e)
        return None


def _try_self_heal_corrupt_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                              pg_password, pg_dbname):
    """Automatically recover a PG cluster that exists but refuses to start.

    Triggered from _ensure_pg_running when every normal start path failed but
    a real cluster is present on disk (the 2026-06-04 'WAL redo PANIC →
    lost conversations' class of failure). Fully automatic — the user never
    has to run pg_resetwal / psql by hand.

    Two-stage strategy (least destructive first):
      1. ``pg_resetwal -f`` on the existing pgdata, then retry start. This is
         the standard escape from a WAL-redo PANIC and preserves all heap data
         (only un-checkpointed WAL is discarded). A pre-reset filesystem copy
         of pg_wal is NOT kept, but the whole cluster is quarantined in stage 2
         only if this fails, so stage-1 operates in place.
      2. If reset fails (or the reset cluster still won't start), quarantine
         the corrupt pgdata, ``initdb`` a fresh cluster, and restore the latest
         ``data/pg_backups/`` logical dump into it. Data written since the last
         nightly backup is lost, but the service comes back automatically with
         the bulk of history instead of a 2-row SQLite.

    Disabled by TOFU_PG_SELF_HEAL=0 (then we fall back to the loud-fail path).

    Returns:
        dict {PG_HOST, PG_PORT, PG_DSN} on success, or None.
    """
    if getenv_compat('TOFU_PG_SELF_HEAL', default='1').lower() in ('0', 'false', 'no'):
        logger.warning('[DB-SelfHeal] Disabled via TOFU_PG_SELF_HEAL — skipping '
                       'automatic recovery')
        return None
    if not _pg_binaries_present():
        return None

    from lib.log import audit_log

    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # Take an exclusive backup of the corrupt cluster dir first (cheap, just a
    # filesystem copy) so a botched self-heal can never destroy the only copy.
    logger.warning('[DB-SelfHeal] PG cluster at %s exists but will not start — '
                   'attempting automatic recovery', pgdata)
    audit_log('pg_self_heal_start', pgdata=pgdata)

    # ── Stage 1: pg_resetwal -f, then retry start ──
    pg_resetwal = _find_pg_binary('pg_resetwal')
    if shutil.which(pg_resetwal) or os.path.isfile(pg_resetwal):
        try:
            result = subprocess.run(
                [pg_resetwal, '-f', '-D', pgdata],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.warning('[DB-SelfHeal] Stage 1: pg_resetwal -f succeeded — '
                               'retrying start')
                conf_port = _read_our_pg_port(pgdata) or pg_port
                log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
                try:
                    start = subprocess.run(
                        [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
                         'start', '-w', '-t', '30'],
                        capture_output=True, text=True, timeout=45)
                    if start.returncode == 0 and _verify_pg_after_start(
                            conf_port, pgdata, pg_user, total_wait_s=12):
                        logger.warning('[DB-SelfHeal] Stage 1 recovery SUCCESS — '
                                       'PG started after pg_resetwal')
                        _ensure_database_exists('127.0.0.1', conf_port, pg_dbname,
                                                pg_user, pgdata)
                        _write_owner_host(pgdata)
                        _mark_pg_owned_locally(pgdata)
                        audit_log('pg_self_heal_success', stage='resetwal',
                                  port=conf_port)
                        return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                                'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
                    logger.error('[DB-SelfHeal] Stage 1: PG still will not start '
                                 'after pg_resetwal (rc=%d): %s',
                                 start.returncode, (start.stderr or '').strip()[:300])
                    _stop_local_pg_quietly(pgdata)
                except Exception as e:
                    logger.error('[DB-SelfHeal] Stage 1 start attempt raised: %s', e)
            else:
                logger.error('[DB-SelfHeal] Stage 1: pg_resetwal failed (rc=%d): %s',
                             result.returncode, (result.stderr or '').strip()[:300])
        except Exception as e:
            logger.error('[DB-SelfHeal] Stage 1 pg_resetwal raised: %s', e)

    # ── Stage 2: quarantine + fresh initdb + restore latest backup ──
    latest = _latest_pg_backup(base_dir)
    if not latest:
        logger.critical('[DB-SelfHeal] Stage 2 impossible: no logical backup in '
                        'data/pg_backups/ to restore from. Leaving corrupt cluster '
                        'in place for manual recovery (pg_resetwal already tried). '
                        'A nightly backup would have made this automatic.')
        audit_log('pg_self_heal_failed', reason='no_backup')
        return None

    logger.warning('[DB-SelfHeal] Stage 2: rebuilding from latest backup %s', latest)
    quarantine = _quarantine_corrupt_pgdata(pgdata)
    if not quarantine:
        return None

    # Stage the dump where _bootstrap_pg's restore step looks for it, so we
    # reuse the well-tested initdb → createdb → restore path verbatim.
    staged_dump = os.path.join(base_dir, 'data', 'pg_backup.sql')
    try:
        shutil.copy2(latest, staged_dump)
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not stage backup for restore: %s', e)
        return None

    result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                           pg_password, pg_dbname)
    if result:
        logger.warning('[DB-SelfHeal] Stage 2 recovery SUCCESS — fresh cluster '
                       'restored from %s (corrupt cluster preserved at %s)',
                       latest, quarantine)
        audit_log('pg_self_heal_success', stage='restore_backup',
                  backup=latest, quarantine=quarantine)
        return result

    logger.critical('[DB-SelfHeal] Stage 2 FAILED — fresh initdb+restore did not '
                    'come up. Corrupt cluster preserved at %s, backup at %s.',
                    quarantine, latest)
    audit_log('pg_self_heal_failed', reason='restore_failed',
              quarantine=quarantine, backup=latest)
    return None


def _read_our_pg_port(pgdata):
    """Read the port from OUR postgresql.conf, if it exists."""
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return None
    try:
        port = None
        with open(conf_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('port') and '=' in stripped:
                    if stripped.startswith('#'):
                        continue
                    val = stripped.split('=', 1)[1].strip().split('#')[0].strip()
                    port = int(val)
        return port
    except Exception as e:
        logger.debug('[DB] Could not parse port from postgresql.conf: %s', e)
        return None


def _verify_pg_data_directory(host, port, pgdata, pg_user):
    """Check that the PG on host:port uses OUR pgdata directory."""
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', 'SHOW data_directory;'],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            remote_pgdata = result.stdout.strip()
            our_pgdata = os.path.realpath(pgdata)
            remote_real = os.path.realpath(remote_pgdata) if remote_pgdata else ''
            if remote_real and remote_real != our_pgdata:
                logger.warning(
                    '[DB] data_directory mismatch: PG on %s:%d uses %s, '
                    'but ours is %s', host, port, remote_pgdata, pgdata)
                return False
            logger.debug('[DB] data_directory verified: PG on %s:%d → %s', host, port, remote_pgdata)
            return True
        else:
            logger.debug('[DB] Could not verify data_directory on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return False  # fail-safe: cannot verify → refuse to match
    except FileNotFoundError:
        logger.debug('[DB] psql binary not found — cannot verify data_directory')
        return False  # fail-safe: no psql → refuse to match
    except Exception as e:
        logger.debug('[DB] data_directory check failed on %s:%d: %s', host, port, e)
        return False  # fail-safe: error → refuse to match


def _pg_has_database(host, port, dbname, pg_user):
    """Check if a PostgreSQL instance has a specific database."""
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            has_it = result.stdout.strip() == '1'
            logger.debug('[DB] Database "%s" on %s:%d: %s',
                        dbname, host, port, 'exists' if has_it else 'NOT FOUND')
            return has_it
        else:
            logger.debug('[DB] Could not check database existence on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return True
    except Exception as e:
        logger.debug('[DB] Database existence check failed on %s:%d: %s', host, port, e)
        return True


def _pg_real_connect_ok(host, port, pg_user, pg_dbname, timeout_s=5):
    """Probe a PG host with a *real* connection, not just pg_isready.

    pg_isready returns OK as soon as postmaster accepts a TCP connection,
    even if the backend process that actually services queries is hung
    (common with "half-alive" containers on shared FUSE storage where
    the postmaster's FUSE-bound disk I/O is unreachable). A real
    psycopg2.connect() is what the app uses, so it's what we probe.

    Returns True if a fresh connection + trivial SELECT succeeds.
    """
    try:
        import psycopg2
    except ImportError:
        logger.debug('[DB] psycopg2 not importable — cannot do real-connect probe')
        return False
    db_user = pg_user or _get_username()
    dsn = f"host={host} port={port} dbname={pg_dbname or 'template1'} user={db_user}"
    try:
        conn = psycopg2.connect(
            dsn,
            connect_timeout=timeout_s,
            application_name='tofu-probe',
            gssencmode='disable',
        )
    except Exception as e:
        logger.debug('[DB] Real-connect probe to %s:%d failed: %s', host, port, e)
        return False
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        return True
    except Exception as e:
        logger.debug('[DB] Real-connect probe query to %s:%d failed: %s', host, port, e)
        return False
    finally:
        try:
            conn.close()
        except Exception as _e:
            logger.debug('[DB] Real-connect probe close failed: %s', _e)


def _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
    """Verify the PG we just started is truly ours and stays alive.

    pg_ctl start can succeed (rc=0) and yet the postmaster shuts itself
    down moments later. Three failure modes we must detect here:

    1. WAL recovery PANIC (e.g. "invalid resource manager ID in
       checkpoint record"). pg_ctl returns 0 because the postmaster
       process itself launched fine; the startup sub-process aborts
       seconds later and the postmaster shuts down.
    2. Concurrent-start race: another host's postmaster wrote a
       different PID to postmaster.pid AFTER our pg_ctl rc=0 but BEFORE
       we noticed. Our postmaster will discover this within ~60s and
       perform an "immediate shutdown because data directory lock file
       is invalid".
    3. data_directory mismatch: rare, but if a port collision dance
       lands us on someone else's PG, we should not declare success.

    Approach: poll over ~total_wait_s. At each tick verify (a) postmaster.pid
    still references a live local PG process, AND (b) a real psycopg2
    connect+SELECT 1 succeeds, AND (c) data_directory matches our pgdata.
    Require two consecutive successful checks before declaring victory.

    Returns True if PG is healthy, False otherwise. On failure the caller
    is expected to NOT take ownership and to fall back / retry.
    """
    deadline = time.monotonic() + total_wait_s
    consecutive_ok = 0
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    last_err = None
    while time.monotonic() < deadline:
        # Check 1 — pidfile + PID alive locally
        try:
            with open(pidfile) as _f:
                pid_str = _f.readline().strip()
            pid = int(pid_str)
        except FileNotFoundError as _e_audit:
            logger.debug('[_bootstrap] _verify_pg_after_start caught %s: %s', type(_e_audit).__name__, _e_audit)
            last_err = 'postmaster.pid disappeared'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        except (OSError, ValueError) as e:
            # OSError: permission/filesystem; ValueError: int(pid_str) on garbage.
            logger.debug('[DB:bootstrap] postmaster.pid unreadable: %s', e)
            last_err = f'postmaster.pid unreadable: {e}'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        try:
            from lib.compat import is_process_alive
            if not is_process_alive(pid):
                last_err = f'postmaster PID {pid} not alive (likely PANIC during recovery)'
                consecutive_ok = 0
                time.sleep(0.5)
                continue
        except ImportError as e:
            logger.debug('[DB] is_process_alive unavailable for verify: %s', e)
        # Check 2 — real psycopg2 connect + trivial query
        if not _pg_real_connect_ok('127.0.0.1', pg_port, pg_user, None, timeout_s=3):
            last_err = 'real psycopg2 SELECT 1 failed'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        # Check 3 — data_directory matches ours
        try:
            if not _verify_pg_data_directory('127.0.0.1', pg_port, pgdata, pg_user):
                last_err = 'data_directory mismatch (someone else\'s PG)'
                consecutive_ok = 0
                time.sleep(1.0)
                continue
        except Exception as e:
            logger.debug('[DB] _verify_pg_data_directory raised during verify: %s', e)
            last_err = f'data_directory probe raised: {e}'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        consecutive_ok += 1
        if consecutive_ok >= 2:
            return True
        time.sleep(0.5)
    logger.error('[DB] Post-start verification FAILED after %.1fs: %s',
                 total_wait_s, last_err)
    return False


def _stop_local_pg_quietly(pgdata):
    """Best-effort pg_ctl stop -m fast, used to undo a failed start."""
    try:
        subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast', '-w', '-t', '10'],
            capture_output=True, text=True, timeout=15
        )
        logger.info('[DB] Stopped local PG after failed post-start verification')
    except Exception as e:
        logger.debug('[DB] Quiet stop after failed verify raised: %s', e)


def _scan_for_our_pg(host, port_range, pgdata, pg_user):
    """Scan a range of ports for a PG instance that owns our pgdata."""
    for port in port_range:
        try:
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', host, '-p', str(port), '-d', 'template1'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                continue
            if _verify_pg_data_directory(host, port, pgdata, pg_user):
                logger.info('[DB] Found our PG on %s:%d (port scan recovery)', host, port)
                return port
        except Exception as e:
            logger.debug('[DB] Port scan probe %d failed: %s', port, e)
            continue
    return None


def _ensure_database_exists(host, port, pg_dbname, pg_user, pgdata):
    """Run ``createdb`` if the target database doesn't exist yet."""
    if not _verify_pg_data_directory(host, port, pgdata, pg_user):
        logger.error('[DB] REFUSING to createdb on %s:%d — it is NOT our PG instance '
                     '(data_directory mismatch). This prevents data leakage.',
                     host, port)
        return

    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Try the given host first; if 'localhost' DNS fails (macOS quirk),
    # retry with 127.0.0.1 as fallback.
    hosts_to_try = [host]
    if host == 'localhost':
        hosts_to_try.append('127.0.0.1')
    elif host == '127.0.0.1':
        hosts_to_try.append('localhost')
    for _h in hosts_to_try:
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _h, '-p', str(port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.debug('[DB] Database "%s" already exists on %s:%d',
                                pg_dbname, _h, port)
                    return
                elif 'could not translate host name' in result.stderr and _h != hosts_to_try[-1]:
                    logger.debug('[DB] createdb DNS failed for %s, retrying with %s', _h, hosts_to_try[-1])
                    continue
                else:
                    logger.warning('[DB] createdb on %s:%d failed: %s',
                                  _h, port, result.stderr.strip())
            else:
                logger.info('[DB] Created missing database "%s" on %s:%d',
                           pg_dbname, _h, port)
            return
        except FileNotFoundError:
            logger.debug('[DB] createdb binary not found (looked for: %s) — skipping', createdb_bin)
            return
        except Exception as e:
            logger.warning('[DB] createdb check failed: %s', e)
            return


def _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Bootstrap a brand-new PostgreSQL data directory and start the server.

    Returns:
        dict with updated PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    logger.info('[DB] Bootstrapping new PostgreSQL data directory at %s ...', pgdata)

    os.makedirs(os.path.dirname(pgdata), exist_ok=True)
    os.makedirs(os.path.join(base_dir, 'logs'), exist_ok=True)

    # initdb
    initdb_bin = _find_pg_binary('initdb')
    try:
        result = subprocess.run(
            [initdb_bin, '-D', pgdata, '--encoding=UTF8', '--locale=C',
             '--auth=trust', '--username=' + (pg_user or _get_username())],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            logger.error('[DB] initdb failed: %s', result.stderr)
            return None
        logger.info('[DB] initdb completed successfully')
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] initdb not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', initdb_bin, hint)
        return None
    except Exception as e:
        logger.error('[DB] initdb failed: %s', e, exc_info=True)
        return None

    # Pick a free port and configure
    free_port = _find_free_port(start=pg_port)
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    try:
        with open(conf_path, 'a') as f:
            f.write('\n# ── Tofu auto-bootstrap overrides ──\n')
            f.write(f'port = {free_port}\n')
            f.write("listen_addresses = '*'\n")
            f.write("unix_socket_directories = ''\n")
        logger.info('[DB] Configured PG port=%d in postgresql.conf', free_port)
    except Exception as e:
        logger.error('[DB] Cannot write postgresql.conf: %s', e)
        return None

    # Apply the managed tuning block (max_connections / durability / WAL).
    # Written BEFORE the first start, so no restart is needed here.
    _ensure_managed_pg_config(pgdata)

    # Start PG — but first acquire the cross-host startup lock so we
    # don't race another tofu host that shares this pgdata.
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Skipping initdb-time pg_ctl start: another host '
                       'holds the cross-host startup lock. Falling back.')
        return None
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    pg_ctl_bin = _find_pg_binary('pg_ctl')
    try:
        start_cmd = [pg_ctl_bin, '-D', pgdata, '-l', log_path, 'start']
        if IS_WINDOWS:
            # On Windows, pg_ctl start needs -w (wait) to be reliable
            start_cmd.insert(-1, '-w')
        result = subprocess.run(
            start_cmd,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl start failed: %s', result.stderr)
            _release_startup_lock()
            return None
        logger.info('[DB] PostgreSQL started on port %d', free_port)
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] pg_ctl not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', pg_ctl_bin, hint)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] pg_ctl start failed: %s', e, exc_info=True)
        _release_startup_lock()
        return None

    # Post-start verification: if the postmaster PANIC-shuts within a
    # few seconds (WAL corruption, concurrent-start race), fail fast.
    if not _verify_pg_after_start(free_port, pgdata, pg_user, total_wait_s=12):
        logger.error('[DB] Freshly initdb\'d PG failed post-start verification — '
                     'stopping it and aborting bootstrap. See logs/postgresql.log.')
        _stop_local_pg_quietly(pgdata)
        _release_startup_lock()
        return None

    time.sleep(1)

    # Create the database
    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Use 127.0.0.1 instead of 'localhost' — on macOS, DNS resolution of
    # 'localhost' can fail when network is misconfigured (e.g. iPhone tethering,
    # VPN) with: "could not translate host name 'localhost' to address".
    for _createdb_host in ('127.0.0.1', 'localhost'):
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _createdb_host, '-p', str(free_port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.info('[DB] Database "%s" already exists', pg_dbname)
                    break  # success
                elif ('could not translate host name' in result.stderr
                      and _createdb_host == '127.0.0.1'):
                    # Shouldn't happen with 127.0.0.1, but just in case
                    continue
                else:
                    logger.error('[DB] createdb failed: %s', result.stderr)
                    return None
            else:
                logger.info('[DB] Created database "%s"', pg_dbname)
                break  # success
        except Exception as e:
            logger.error('[DB] createdb failed: %s', e, exc_info=True)
            return None

    # ── Restore from pg_backup.sql if export.py left one behind ──
    # export.py's personal-mode export NEVER raw-copies pgdata/ (hot-copy
    # across FUSE causes TOAST chunk corruption). Instead it does a
    # pg_dumpall → data/pg_backup.sql. On the destination's first boot,
    # we've just finished initdb + createdb above — so this is exactly
    # the moment to feed the dump into psql. On success we delete the
    # dump file so subsequent boots skip straight through.
    _restore_from_sql_dump_if_present(base_dir, free_port, db_user, pg_dbname)

    # Build DSN
    dsn = f"host=127.0.0.1 port={free_port} dbname={pg_dbname}"
    if pg_user:
        dsn += f" user={pg_user}"
    if pg_password:
        dsn += f" password={pg_password}"
    _write_owner_host(pgdata)
    _mark_pg_owned_locally(pgdata)
    logger.info('[DB] Bootstrap complete — DSN: host=127.0.0.1 port=%d dbname=%s',
                free_port, pg_dbname)
    return {'PG_HOST': '127.0.0.1', 'PG_PORT': free_port, 'PG_DSN': dsn}


def _restore_from_sql_dump_if_present(base_dir, pg_port, pg_user, pg_dbname):
    """If ``data/pg_backup.sql`` exists (left by export.py), restore it.

    The dump was produced by ``pg_dumpall --clean --if-exists`` so it's
    safe to apply to a freshly-initdb'd cluster that only has the default
    ``template1`` / ``postgres`` / ``$USER`` databases.

    After a successful restore the dump file is DELETED so we never
    restore the same snapshot twice (which would clobber any new data
    written by the user on the destination after the first boot).

    Silent no-op if the dump is missing, empty, or ``psql`` is unavailable.
    """
    dump_path = os.path.join(base_dir, 'data', 'pg_backup.sql')
    if not os.path.isfile(dump_path):
        return
    try:
        size = os.path.getsize(dump_path)
    except OSError as e:
        logger.warning('[DB] Could not stat pg_backup.sql: %s — skipping restore', e)
        return
    if size == 0:
        logger.info('[DB] pg_backup.sql is empty — removing and skipping restore')
        try:
            os.remove(dump_path)
        except OSError as _e:
            logger.debug('[DB] Could not remove empty dump: %s', _e)
        return

    psql_bin = _find_pg_binary('psql')
    if not shutil.which(psql_bin) and not os.path.isfile(psql_bin):
        logger.warning('[DB] psql not found — cannot restore %s '
                       '(destination will come up with an empty DB). '
                       'Install PostgreSQL client to enable auto-restore.',
                       dump_path)
        return

    logger.info('[DB] Restoring data from %s (%.1f MB) — this may take a moment…',
                dump_path, size / (1024 * 1024))
    try:
        # Connect to the postgres admin DB; pg_dumpall --clean expects
        # to be able to DROP the target databases before recreating them.
        # -v ON_ERROR_STOP=1 makes a partial restore fail loudly instead
        # of leaving a half-restored DB.
        result = subprocess.run(
            [psql_bin, '-h', '127.0.0.1', '-p', str(pg_port), '-U', pg_user,
             '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q', '-f', dump_path],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            # No timeout — large dumps can take minutes on FUSE.
        )
    except Exception as e:
        logger.error('[DB] psql restore invocation failed: %s', e, exc_info=True)
        return

    if result.returncode != 0:
        # Leave the dump file in place so the user can retry manually.
        logger.error('[DB] Restore from %s FAILED (rc=%d). Dump preserved for '
                     'manual retry. stderr=%.1000s',
                     dump_path, result.returncode, (result.stderr or '').strip())
        return

    logger.info('[DB] Restore from %s completed successfully', dump_path)
    try:
        os.remove(dump_path)
        logger.info('[DB] Removed %s (restore complete, one-shot)', dump_path)
    except OSError as e:
        logger.warning('[DB] Could not remove restored dump %s: %s', dump_path, e)


def _pg_binaries_present():
    """Quick check: is pg_ctl available at all on this host?

    Returns True only if the core PG binaries are discoverable. This lets
    us bail out of the whole bootstrap flow early with a friendly
    "fallback to SQLite" message, instead of emitting a string of ERROR
    logs as we probe ports, scan directories, and finally try pg_ctl.
    """
    # _find_pg_binary returns the bare name as a fallback — but that only
    # works as a launch argument if PATH has the real binary. So we also
    # verify with shutil.which() that SOMETHING is there.
    pg_ctl = _find_pg_binary('pg_ctl')
    if os.path.isabs(pg_ctl) and os.path.isfile(pg_ctl):
        return True
    # Bare name — check PATH
    return shutil.which(pg_ctl) is not None


def _try_explicit_pg_target(pgdata, base_dir, pg_host, pg_port, build_dsn):
    """Step 1 of ``_ensure_pg_running``: handle an explicit env-set PG target.

    When ``TOFU_PG_HOST`` names a remote host, OR ``TOFU_PG_PORT`` is set (even
    for localhost), the user manages PG externally — connect directly rather
    than bootstrap.  A LOCAL explicit port that names OUR OWN pgdata is special:
    an unreachable target then means "our local PG is currently down" (e.g. the
    Restart button stopped it and the new server raced ahead), so we fall
    through to the local start path instead of failing to SQLite.

    Returns
    -------
    (handled: bool, result: dict | None)
        handled=True  → the caller must ``return result`` (result is the PG
                        info dict on success, or None on a hard failure).
        handled=False → no explicit target, OR an explicit-LOCAL-ours target is
                        down → the caller falls through to Step 2 (local start).
    """
    explicit_host = getenv_compat('TOFU_PG_HOST')
    explicit_port = getenv_compat('TOFU_PG_PORT', default=None)
    is_explicit_external = (
        (explicit_host and explicit_host not in ('localhost', '127.0.0.1', '::1'))
        or explicit_port is not None  # any explicit port = user-managed PG
    )
    if not is_explicit_external:
        return False, None

    target_host = explicit_host or pg_host
    target_port = int(explicit_port) if explicit_port else pg_port
    # A local explicit port (e.g. TOFU_PG_PORT pointing at 127.0.0.1)
    # almost always names OUR OWN pgdata started by a previous server.py —
    # NOT a truly external/unmanaged PG. If that cluster is OURS and we
    # have the binaries to manage it, an unreachable target just means
    # "our local PG is currently down" (e.g. the user clicked Restart,
    # which stops PG, and the new server raced ahead of it). In that case
    # we must START it ourselves rather than give up and fall back to a
    # near-empty SQLite. Only a genuinely external target (remote host, or
    # a local port whose pgdata isn't ours) is strictly connect-or-fail.
    target_is_local = target_host in ('localhost', '127.0.0.1', '::1')
    target_is_ours = (
        target_is_local
        and _read_our_pg_port(pgdata) == target_port
        and _pg_binaries_present()
    )
    logger.info('[DB] Using explicit PG target from env: %s:%d (manageable_local=%s)',
                target_host, target_port, target_is_ours)
    # Try psycopg2 directly (no pg_isready binary needed — works in CI)
    try:
        import psycopg2
        test_dsn = build_dsn(target_host, target_port)
        conn = psycopg2.connect(test_dsn, connect_timeout=5)
        conn.close()
        logger.info('[DB] Explicit PG target %s:%d is reachable', target_host, target_port)
        # Manage our own cluster's tuning so the connection / WAL settings
        # stay in sync (otherwise PG keeps its initdb defaults — e.g.
        # max_connections=200 — below the app-side TOFU_DB_MAX_CONNS
        # ceiling, producing 'too many clients' FATALs).
        if target_is_local and _read_our_pg_port(pgdata) == target_port:
            _mark_pg_owned_locally(pgdata)
            if _ensure_managed_pg_config(pgdata):
                _restart_local_pg(pgdata, base_dir)
        return True, {'PG_HOST': target_host, 'PG_PORT': target_port,
                      'PG_DSN': test_dsn}
    except ImportError:
        logger.error('[DB] psycopg2 not installed — cannot connect to explicit PG')
        return True, None
    except Exception as e:
        if target_is_ours:
            logger.warning('[DB] Explicit local PG target %s:%d is down (%s) — '
                           'it names OUR pgdata, so attempting to START it '
                           'locally instead of falling back to SQLite.',
                           target_host, target_port, e)
            # Fall through to the local start/bootstrap path (Step 2+).
            return False, None
        logger.error('[DB] Explicit PG target %s:%d not reachable: %s',
                     target_host, target_port, e)
        return True, None


def _pgdata_major_compatible(pgdata) -> bool:
    """Return False when the pgdata major version can't be served by the local
    postgres binary (caller must then bail to SQLite); True to proceed.

    Extracted verbatim from ``_ensure_pg_running`` Step 3b.  If the pgdata
    directory was created by a different PG major than the installed binary
    (common after an export/copy across machines), any start attempt FATALs on
    a config-param mismatch (e.g. PG 18's ``autovacuum_worker_slots`` under a
    PG 17 binary) and the scheduler retry-storms on "connection refused".  We
    detect that here so the caller falls back cleanly.

    Returns
    -------
    bool
        False  → incompatible major OR no postgres binary to check against;
                 the caller must ``return None``.
        True   → no PG_VERSION file, unreadable version, a matching major, or
                 a non-fatal probe error — proceed with the normal flow.
    """
    pg_version_file = os.path.join(pgdata, 'PG_VERSION')
    if not os.path.isfile(pg_version_file):
        return True
    try:
        with open(pg_version_file) as _vf:
            pgdata_major = _vf.read().strip().split('.')[0]
    except Exception as _e:
        logger.debug('[DB] Could not read PG_VERSION from %s: %s', pgdata, _e)
        pgdata_major = None
    if not pgdata_major:
        return True
    # Query the locally-installed postgres binary for its major.
    try:
        _postgres_bin = _find_pg_binary('postgres')
        _ver_out = subprocess.run(
            [_postgres_bin, '--version'],
            capture_output=True, text=True, timeout=5
        )
        if _ver_out.returncode == 0:
            # Output is like "postgres (PostgreSQL) 17.2"
            _bin_major = _ver_out.stdout.strip().split()[-1].split('.')[0]
            if _bin_major != pgdata_major:
                logger.error(
                    '[DB] pgdata major=%s but local postgres binary major=%s '
                    '— REFUSING to start (would FATAL with config-param errors). '
                    'Falling back to SQLite. To recover: move %s aside (e.g. '
                    '`mv pgdata pgdata.bak`) so a fresh pgdata is initdb\'d, '
                    'OR install matching PG version, OR set TOFU_DB_BACKEND=sqlite.',
                    pgdata_major, _bin_major, pgdata)
                return False
            logger.debug('[DB] pgdata major (%s) matches local binary', pgdata_major)
    except FileNotFoundError:
        # No postgres binary on host — caller (_core) already bailed
        # earlier via _pg_binaries_present(), so this shouldn't fire,
        # but guard anyway.
        logger.info('[DB] No postgres binary to version-check pgdata against')
        return False
    except Exception as _e:
        logger.debug('[DB] Could not run postgres --version: %s', _e)
        # Non-fatal — let normal flow try to start PG; it'll fail
        # with a clearer log if incompatible.
    return True


def _ensure_pg_running(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Ensure PostgreSQL is accessible. Start locally or discover remote instance.

    Returns:
        dict with PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # ── Step 0: Early bail if PG binaries are simply not installed ──
    # Unless the user has explicitly set TOFU_PG_HOST to a remote, there's
    # no point probing anything — we can't start, query, or verify PG.
    # This turns a noisy "ERROR: pg_ctl not found" trace into a single
    # friendly INFO line, and the caller seamlessly falls back to SQLite.
    _explicit_host = getenv_compat('TOFU_PG_HOST')
    _explicit_remote = (_explicit_host
                        and _explicit_host not in ('localhost', '127.0.0.1', '::1'))
    if not _explicit_remote and not _pg_binaries_present():
        logger.info(
            '[DB] PostgreSQL client binaries (pg_ctl, initdb, psql) not found '
            'on this host — SKIPPING PG bootstrap and falling back to SQLite. '
            'This is normal when PG is not installed. '
            'To enable PG (better concurrency for 100+ users): '
            'conda install -c conda-forge postgresql>=18'
        )
        return None

    # ── Step 1: Explicit host/port override (see _try_explicit_pg_target) ──
    # An env-set remote host or any explicit port = user-managed PG: connect
    # directly. handled=True → return its result; handled=False → fall through
    # to the local start path (no explicit target, or our-own-local PG is down).
    _handled, _explicit_result = _try_explicit_pg_target(
        pgdata, base_dir, pg_host, pg_port, _build_dsn)
    if _handled:
        return _explicit_result

    # ── Step 2: Read OUR port from OUR postgresql.conf ──
    our_port = _read_our_pg_port(pgdata)
    if our_port is not None:
        pg_port = our_port
        logger.info('[DB] Read port=%d from our postgresql.conf', our_port)

        try:
            # Use 127.0.0.1 — 'localhost' DNS can fail on macOS with certain
            # network configs (iPhone tethering, VPN, etc.)
            _local = '127.0.0.1'
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', _local, '-p', str(pg_port), '-d', 'template1'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                is_ours = _verify_pg_data_directory(_local, pg_port, pgdata, pg_user)
                if is_ours:
                    logger.info('[DB] PostgreSQL already running on %s:%d (verified ours)', _local, pg_port)
                    _ensure_database_exists(_local, pg_port, pg_dbname, pg_user, pgdata)
                    # Already-running local PG on our pgdata — almost
                    # certainly started by a previous server.py on this
                    # host. Take ownership so shutdown_pool stops it.
                    _mark_pg_owned_locally(pgdata)
                    # Re-apply managed tuning; restart only if it changed a
                    # restart-only setting (max_connections / wal_level).
                    if _ensure_managed_pg_config(pgdata):
                        _restart_local_pg(pgdata, base_dir)
                    return {'PG_HOST': _local, 'PG_PORT': pg_port,
                            'PG_DSN': _build_dsn(_local, pg_port)}
                else:
                    # NOT our PG — NEVER reuse another project's PG just because
                    # it has a database with the same name. This prevents cross-project
                    # data leakage and PID-file duel crashes.
                    logger.warning(
                        '[DB] PG on %s:%d is NOT ours (data_directory mismatch) '
                        '— REFUSING to reuse. Scanning nearby ports for our PG.',
                        _local, pg_port)
                    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
                    if found_our_port:
                        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
                        _mark_pg_owned_locally(pgdata)
                        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                                'PG_DSN': _build_dsn(_local, found_our_port)}
        except Exception as _e:
            logger.debug('[DB] pg_isready localhost:%d check failed: %s', pg_port, _e)

    # ── Step 2b: Scan nearby ports for our PG by data_directory ──
    # Only match by data_directory verification — NEVER by database name alone.
    # This prevents cross-project data leakage when exported copies share the
    # same database name but must use independent PG instances.
    _local = '127.0.0.1'
    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
    if found_our_port:
        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
        _mark_pg_owned_locally(pgdata)
        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                'PG_DSN': _build_dsn(_local, found_our_port)}

    # ── Step 3: Check if another machine owns the pgdata ──
    #
    # Defer to the remote ONLY if a fresh tofu heartbeat proves another
    # tofu process is actively using that PG right now. A bare TCP-alive
    # postmaster is not enough: it could be the stale tail of a previous,
    # unclean exit on another host — in which case we must take over
    # rather than route every DB call across a dying link (see
    # .tofu/skills/pg-cross-host-heartbeat-takeover.md).
    is_remote, remote_host = _pg_already_running_on_another_machine(pgdata, pg_port)
    if is_remote and remote_host:
        fresh, hb_info = _heartbeat_is_fresh(pgdata)
        if fresh:
            remote_ok = _pg_real_connect_ok(remote_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_ok:
                logger.info('[DB] PostgreSQL is running on remote machine %s '
                            '(heartbeat fresh, age=%.1fs, pid=%s) — connecting as client',
                            remote_host, hb_info.get('age_s', -1) if hb_info else -1,
                            hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(remote_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': remote_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(remote_host, pg_port)}
            logger.warning('[DB] Heartbeat was fresh but real-connect to %s:%d failed — '
                          'treating as dead and taking over locally', remote_host, pg_port)
        else:
            if hb_info is None:
                logger.info('[DB] Remote PG owner %s present but no tofu heartbeat file '
                            '— previous owner exited uncleanly; taking over locally',
                            remote_host)
            else:
                logger.info('[DB] Remote PG owner %s has a STALE heartbeat '
                            '(age=%.1fs > ttl=%ds, last_pid=%s) — previous owner is gone; '
                            'taking over locally',
                            remote_host, hb_info.get('age_s', -1),
                            _HEARTBEAT_TTL_S, hb_info.get('pid'))

    # ── Step 3b: pgdata ↔ binary major-version sanity check ──
    # A pgdata created by a different PG major than the installed binary
    # FATALs on start (config-param mismatch) → scheduler retry-storm. Detect
    # it (see _pgdata_major_compatible) and fall back to SQLite cleanly.
    if not _pgdata_major_compatible(pgdata):
        return None

    # ── Step 4/5: Start PG locally or bootstrap ──
    # Before any local start/takeover, verify the pgdata mount truly enforces
    # advisory locks. A silent-no-op filesystem makes the cross-host startup
    # interlock useless (two hosts both "acquire" and double-start → WAL
    # corruption). Warn loudly, or refuse entirely if TOFU_PG_REQUIRE_FLOCK.
    if not _verify_flock_support_or_warn(pgdata):
        return None

    if not os.path.isdir(pgdata):
        logger.info('[DB] No pgdata directory — bootstrapping new PostgreSQL instance')
        result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
        if not result:
            logger.error('[DB] Bootstrap failed — refusing to connect to '
                         'default 127.0.0.1:%d (may be another project)', pg_port)
        return result

    # Clean up stale pidfile
    #
    # Container-switch scenario: a user uses web-based VS Code and moves
    # between containers, so the machine IP changes but only ONE container
    # is live at any time. The `.pg_owner_host` marker from the previous
    # container will point at an IP that no longer runs PG. Treat such a
    # marker as stale — probe reachability first before deferring to it.
    #
    # Rule:
    #   - Remote host reachable on PG port → concurrent multi-host scenario,
    #     defer to remote (preserves the original cross-machine safety net).
    #   - Remote host NOT reachable → previous owner is dead (container gone
    #     or machine switched), auto-heal by removing stale markers and
    #     starting PG locally. This makes container switches a no-op.
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if os.path.exists(pidfile):
        owner_host = _read_pg_host_from_pidfile(pgdata)
        local_ip = _get_local_ip()
        # IP-independent guard FIRST: if the pidfile PID is a live local
        # postgres, this host already owns the cluster. _get_local_ip() can
        # flap (container IP reassignment), which previously caused a host to
        # see its OWN postmaster as "remote", delete the pidfile, and start a
        # SECOND postmaster on the same pgdata → pg_subtrans corruption.
        # Reuse the running instance instead of taking over.
        if _pidfile_pid_is_live_local_postgres(pgdata):
            conf_port = _read_our_pg_port(pgdata) or pg_port
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.warning('[DB] Step 4: postmaster.pid is a LIVE local postgres '
                               '(owner_host marker=%s, local_ip=%s) — reusing it '
                               'instead of taking over (IP-flap safe).',
                               owner_host, local_ip)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            logger.warning('[DB] Step 4: pidfile PID is live postgres but data_directory '
                           'verify on 127.0.0.1:%d failed — proceeding with caution.', conf_port)
        if owner_host and owner_host not in (local_ip, 'localhost', '127.0.0.1'):
            # Heartbeat is the authoritative signal: only defer if another
            # tofu is actively running there. Bare TCP-alive postmaster
            # is not enough (an unclean exit can leave it answering for
            # hours).
            fresh, hb_info = _heartbeat_is_fresh(pgdata)
            remote_alive = fresh and _pg_real_connect_ok(
                owner_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_alive:
                logger.warning('[DB] Step 4 safety net: postmaster.pid belongs to '
                               'remote host %s (we are %s) and tofu heartbeat is '
                               'fresh (age=%.1fs, pid=%s) — refusing to delete. '
                               'Connecting to remote host.',
                               owner_host, local_ip,
                               hb_info.get('age_s', -1) if hb_info else -1,
                               hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(owner_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': owner_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(owner_host, pg_port)}
            # Stale or missing heartbeat — previous owner is gone (unclean
            # exit, container switched, machine rebooted). Auto-heal: remove
            # ownership markers and proceed to start PG locally. Data
            # files are untouched.
            if hb_info is None:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s exited '
                               'uncleanly (no heartbeat file) — taking over locally.',
                               owner_host)
            elif not fresh:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat is '
                               'stale (age=%.1fs > ttl=%ds, last_pid=%s) — '
                               'taking over locally.',
                               owner_host, hb_info.get('age_s', -1),
                               _HEARTBEAT_TTL_S, hb_info.get('pid'))
            else:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat '
                               'fresh but PG unreachable — taking over locally.',
                               owner_host)
            owner_file = os.path.join(pgdata, '.pg_owner_host')
            try:
                if os.path.exists(owner_file):
                    os.remove(owner_file)
                    logger.info('[DB] Removed stale .pg_owner_host (was %s)', owner_host)
            except Exception as _e:
                logger.warning('[DB] Could not remove stale .pg_owner_host: %s', _e)
            _clear_heartbeat(pgdata)
        else:
            logger.warning('[DB] Removing stale postmaster.pid before starting PG '
                          '(owner: %s, us: %s)', owner_host, local_ip)
        # Cross-host HARD interlock — the real anti-corruption barrier.
        #
        # We are about to delete a postmaster.pid and start our own
        # postmaster. Removing the pidfile defeats PostgreSQL's OWN
        # single-postmaster guard, so THIS is the catastrophic step: if
        # every IP/PID/heartbeat heuristic above was wrong and the "dead"
        # owner is actually a LIVE peer on another host, deleting its
        # pidfile and starting a second postmaster on the shared FUSE
        # pgdata corrupts WAL / pg_subtrans. A live peer holds this flock
        # for its entire lifetime, so we acquire it BEFORE the deletion:
        # if another host holds it, two postmasters physically cannot
        # coexist — we refuse to take over and fall back. (The later
        # pg_ctl-start acquisition is idempotent — a no-op once held here.)
        if not _try_acquire_startup_lock(pgdata):
            logger.warning('[DB] Refusing to remove postmaster.pid / take over: '
                           'another host holds the cross-host startup lock on '
                           'pgdata=%s — a live peer owns this PG. Falling back '
                           '(SQLite / retry next cycle).', pgdata)
            return None
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            # Already gone (race with another cleanup path) — fine.
            logger.debug('[DB] postmaster.pid already removed')
        except PermissionError as e:
            if IS_WINDOWS:
                logger.error('[DB] Cannot remove stale pidfile (file locked by another process '
                             '— PG may still be running): %s', e)
            else:
                logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None
        except Exception as e:
            logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None

    _fix_unix_socket_conf(pgdata)

    # Check if configured port is taken (possibly by our own orphaned PG)
    conf_port = _read_our_pg_port(pgdata) or pg_port
    try:
        check = subprocess.run(
            [_find_pg_binary('pg_isready'), '-h', '127.0.0.1', '-p', str(conf_port), '-d', 'template1'],
            capture_output=True, text=True, timeout=3
        )
        if check.returncode == 0:
            # PG is already responding on our port — check if it's ours
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.info('[DB] PG already running on 127.0.0.1:%d (our data_directory) '
                           '— reusing after pidfile cleanup', conf_port)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                if _ensure_managed_pg_config(pgdata):
                    _restart_local_pg(pgdata, base_dir)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            # Not ours — reassign to a different port
            free_port = _find_free_port(start=conf_port + 1)
            if free_port is None:
                logger.error('[DB] No free port found — cannot start PG')
                _release_startup_lock()
                return None
            logger.info('[DB] Port %d is occupied by another PG — reassigning to %d',
                       conf_port, free_port)
            _conf_path = os.path.join(pgdata, 'postgresql.conf')
            try:
                with open(_conf_path) as _f:
                    _lines = _f.readlines()
                with open(_conf_path, 'w') as _f:
                    for _line in _lines:
                        _s = _line.strip()
                        if _s.startswith('port') and '=' in _s and not _s.startswith('#'):
                            _f.write(f'port = {free_port}\n')
                        else:
                            _f.write(_line)
                pg_port = free_port
                logger.info('[DB] Updated postgresql.conf: port = %d', free_port)
            except Exception as _e:
                logger.error('[DB] Failed to update postgresql.conf port: %s', _e)
                _release_startup_lock()
                return None
    except Exception as _e:
        logger.debug('[DB] Port availability check failed: %s', _e)

    logger.info('[DB] Starting PostgreSQL server from %s ...', pgdata)
    # Cross-host startup lock — prevents two tofu hosts on the same
    # FUSE-mounted pgdata from racing into pg_ctl start at the same time
    # (which corrupts WAL with mutual-PID-eviction).
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Another tofu host is currently starting/owning PG '
                       'on this pgdata — skipping our pg_ctl start. Caller will '
                       'fall back to SQLite (or retry next cycle).')
        return None
    try:
        log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path, 'start'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info('[DB] PostgreSQL started successfully on this machine')
            # Verify it stays up — pg_ctl rc=0 does NOT mean recovery
            # succeeded. If WAL is corrupted or another host's pidfile
            # races us, the postmaster will shut itself down within
            # seconds. Catch that here instead of letting the scheduler
            # storm-retry.
            if not _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
                logger.error('[DB] PG started (rc=0) but failed post-start '
                             'verification — likely WAL corruption or concurrent '
                             'start by another host. Stopping local PG and '
                             'attempting automatic self-heal. See logs/postgresql.log.')
                _stop_local_pg_quietly(pgdata)
                # Automatic corruption recovery (pg_resetwal → restore-from-backup)
                # BEFORE giving up. We still hold the cross-host startup lock, so
                # no peer can race us during recovery. Released after either way.
                healed = _try_self_heal_corrupt_pg(
                    pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
                _release_startup_lock()
                return healed
            _ensure_database_exists('127.0.0.1', pg_port, pg_dbname, pg_user, pgdata)
            _write_owner_host(pgdata)
            _mark_pg_owned_locally(pgdata)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn('127.0.0.1', pg_port)}
        else:
            logger.error('[DB] Failed to start PostgreSQL: %s', result.stderr)
            _release_startup_lock()
            return None
    except FileNotFoundError as e:
        # pg_ctl / initdb binary not present — PostgreSQL is simply not
        # installed on this host. This is a normal "PG not available →
        # fallback to SQLite" path, NOT a bug. Log at INFO level so it's
        # clear the system is intentionally degrading.
        logger.info('[DB] PostgreSQL binaries not found on this host (%s). '
                    'This is normal — tofu will automatically use SQLite. '
                    'To enable PG (better concurrency): '
                    '  conda install -c conda-forge postgresql>=18',
                    e)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] Failed to start PostgreSQL: %s', e, exc_info=True)
        _release_startup_lock()
        return None


def _stop_pg(pgdata):
    """Stop PostgreSQL server on shutdown."""
    # Stop the heartbeat first so a peer host that starts up during
    # the pg_ctl stop window sees "no heartbeat" and takes over cleanly.
    stop_heartbeat(pgdata)
    if os.path.isdir(pgdata):
        try:
            subprocess.run(
                [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast'],
                capture_output=True, text=True, timeout=30
            )
            logger.info('[DB] PostgreSQL stopped')
        except Exception as e:
            logger.warning('[DB] Error stopping PostgreSQL: %s', e)
    # Always release the cross-host startup lock on shutdown, regardless
    # of whether pg_ctl stop succeeded — a peer host is better off taking
    # over a potentially-stuck PG than being locked out forever.
    _release_startup_lock()

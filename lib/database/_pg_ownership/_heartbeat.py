"""Tofu-level heartbeat.

Owns the ``_heartbeat_thread`` module global (rebound by
``_start_heartbeat_thread`` / ``stop_heartbeat``), the ``_heartbeat_stop_event``,
the ``_heartbeat_lock``, and the ``_HEARTBEAT_*`` tunables. All accessors of
``_heartbeat_thread`` live HERE.

The shared FUSE-mounted pgdata is occasionally inherited from a previous host
that didn't shut down cleanly: its postmaster may still be TCP-reachable, but
no tofu process there is actively using it. A new server.py on this host would
otherwise read ``.pg_owner_host``, see the remote PG answers, and route every
DB call across the network — only to time out when the abandoned remote
eventually drops or stalls.

The heartbeat file (``pgdata/.tofu_heartbeat``) is written by the process that
actually owns the local PG, refreshed every ``_HEARTBEAT_REFRESH_S`` seconds,
and cleared on clean shutdown. A new startup considers the previous owner
alive iff the heartbeat is fresher than ``_HEARTBEAT_TTL_S``.

Patch-safety: ``_write_heartbeat`` resolves ``_get_local_ip`` through the
``lib.database._pg_ownership`` facade.
"""

import json
import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


_HEARTBEAT_FILE = '.tofu_heartbeat'
_HEARTBEAT_TTL_S = 120
_HEARTBEAT_REFRESH_S = 30

_heartbeat_thread = None
_heartbeat_stop_event = threading.Event()
_heartbeat_lock = threading.Lock()


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
    import lib.database._pg_ownership as _pkg
    payload = {
        'host': _pkg._get_local_ip(),
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

"""Daily backup scheduler.

One tiny daemon thread owns the recurring job: when dual-path backup is
enabled, mirror one verified snapshot to the configured stable directory
once every 24 h.

HTTP activity is fed by a single ``before_request`` hook registered in
``lib/app_assembly.py``; the hook and the loop are both fully guarded so a
panel failure can never break request serving.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_TICK_S = 60.0
_BACKUP_INTERVAL_S = 24 * 60 * 60

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()
_activity_lock = threading.Lock()
_last_http_activity = time.time()  # boot counts as activity: no instant idle run


def note_http_activity() -> None:
    """Record one HTTP request; called from the app before_request hook."""
    global _last_http_activity
    with _activity_lock:
        _last_http_activity = time.time()


def _seconds_since_http_activity() -> float:
    with _activity_lock:
        return time.time() - _last_http_activity


def _storage_ready() -> bool:
    try:
        from lib.storage.service import storage_status

        return bool(storage_status().get('ready'))
    except Exception as _e:  # noqa: BLE001
        logger.debug('probe storage readiness failed: %s', _e)
        return False


def _maybe_run_backup(preferences: dict[str, Any]) -> None:
    backup = preferences.get('backup') or {}
    if not backup.get('enabled') or not backup.get('directory'):
        return
    last = preferences.get('lastBackup') or {}
    if last.get('ok') and time.time() - float(last.get('at') or 0) < _BACKUP_INTERVAL_S:
        return
    if not _storage_ready():
        return
    from lib.database_panel import backup as backup_mod

    try:
        backup_mod.run_backup()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[DatabasePanel] scheduled backup failed: %s: %s',
                       type(exc).__name__, str(exc)[:200])


def _loop() -> None:
    while not _stop.wait(_TICK_S):
        try:
            from lib.database_panel import config_store

            preferences = config_store.load_preferences()
            _maybe_run_backup(preferences)
        except Exception as exc:  # noqa: BLE001 - the loop never dies
            logger.warning('[DatabasePanel] maintenance tick failed: %s: %s',
                           type(exc).__name__, str(exc)[:200])


def start_maintenance() -> bool:
    """Start the scheduler thread once per process; returns True if started."""
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True
    threading.Thread(
        target=_loop, name='database-panel-maintenance', daemon=True).start()
    logger.info('[DatabasePanel] maintenance thread started')
    return True

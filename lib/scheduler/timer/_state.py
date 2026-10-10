"""Shared live Timer Watcher registry and its lock.

All timer modules import these same objects by reference. Durable watcher
records and poll evidence remain in the owner-scoped storage authority.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._runtime import TimerWatcherHandle

_active_timers: dict[str, TimerWatcherHandle] = {}
_timers_lock = threading.Lock()


def remove_live_watcher(timer_id: str) -> None:
    """Wake a retired watcher without cancelling its executing callback."""
    with _timers_lock:
        handle = _active_timers.pop(timer_id, None)
    if handle is not None:
        handle.stop()

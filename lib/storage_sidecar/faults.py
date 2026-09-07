"""Explicit fault points used by isolated storage certification processes."""

from __future__ import annotations

import os
import threading

from lib.storage.errors import StorageError


_lock = threading.Lock()
_consumed: set[str] = set()


def status() -> dict[str, object]:
    enabled = os.environ.get('TOFU_STORAGE_ENABLE_FAULT_INJECTION') == '1'
    requested = sorted({
        item.strip() for item in
        os.environ.get('TOFU_STORAGE_FAULT_ONCE', '').split(',') if item.strip()
    }) if enabled else []
    with _lock:
        consumed = sorted(_consumed) if enabled else []
    return {
        'enabled': enabled, 'requested': requested, 'consumed': consumed,
    }


def inject_once(point: str) -> None:
    """Raise once at *point* when a test-authorized child requests it.

    Fault injection is deliberately unavailable unless the process receives
    the explicit enable flag.  Production launchers never set this flag;
    certification supervisors set it only for an isolated project root.
    """
    if os.environ.get('TOFU_STORAGE_ENABLE_FAULT_INJECTION') != '1':
        return
    requested = {
        item.strip() for item in
        os.environ.get('TOFU_STORAGE_FAULT_ONCE', '').split(',') if item.strip()
    }
    if point not in requested:
        return
    with _lock:
        if point in _consumed:
            return
        _consumed.add(point)
    # A non-retryable classification exposes the transaction boundary to the
    # certification client.  Retryable backend faults are exercised separately
    # by the adapter retry tests.
    raise StorageError('database_internal', f'Injected storage fault at {point}')


__all__ = ['inject_once', 'status']

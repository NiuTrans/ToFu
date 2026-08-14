"""Process-wide application access to the supervised Storage Sidecar."""

from __future__ import annotations

import threading

from lib.storage.client import StorageClient
from lib.storage.runtime import StorageRuntime


_lock = threading.RLock()
_runtime: StorageRuntime | None = None


def storage_runtime() -> StorageRuntime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = StorageRuntime()
        return _runtime


def start_storage() -> StorageClient:
    """Start and fully handshake the configured backend, or fail closed."""
    return storage_runtime().start()


def get_storage_client(*, write: bool = False) -> StorageClient:
    """Return the ready client; never lazily open a database or fallback."""
    return storage_runtime().client(write=write)


def storage_status() -> dict[str, object]:
    """Return current sidecar state without creating or probing a runtime."""
    with _lock:
        runtime = _runtime
    if runtime is None:
        return {
            'ready': False,
            'state': 'not_started',
            'backend': None,
            'pid': None,
            'port': None,
            'restarting': False,
            'restart_attempts': 0,
            'last_exit_code': None,
            'last_error': '',
        }
    return runtime.status()


def stop_storage(timeout: float = 10.0) -> None:
    global _runtime
    with _lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.stop(timeout=timeout)


def install_runtime_for_test(runtime: StorageRuntime | None) -> None:
    """Replace process state only in isolated tests; stop any prior owner."""
    global _runtime
    with _lock:
        previous, _runtime = _runtime, runtime
    if previous is not None and previous is not runtime:
        previous.stop()


__all__ = [
    'get_storage_client', 'install_runtime_for_test', 'start_storage',
    'stop_storage', 'storage_runtime', 'storage_status',
]

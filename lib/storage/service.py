"""Process-wide application access to the supervised Storage Sidecar."""

from __future__ import annotations

import threading

from lib.log import get_logger
from lib.storage.client import StorageClient
from lib.storage.runtime import StorageRuntime

logger = get_logger(__name__)

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


def storage_authority_status(mode: str | None = None) -> dict[str, object]:
    """Return readiness for the sole runtime storage authority."""
    if mode not in (None, 'sidecar'):
        raise ValueError(f'unsupported storage authority: {mode!r}')
    return storage_status()


def stop_storage(timeout: float = 10.0) -> None:
    global _runtime
    with _lock:
        runtime = _runtime
        if runtime is None:
            return
        # Keep the declared owner discoverable until its bounded stop has
        # actually completed.  Clearing first creates a window in which another
        # caller can construct a second runtime while the old Sidecar still
        # holds the project lease; it also hides a failed stop from the re-exec
        # gate.  The service lock serializes this rare lifecycle transition.
        runtime.stop(timeout=timeout)
        if _runtime is runtime:
            _runtime = None


def install_runtime_for_test(runtime: StorageRuntime | None) -> None:
    """Replace process state only in isolated tests; stop any prior owner."""
    global _runtime
    with _lock:
        previous, _runtime = _runtime, runtime
    if previous is not None and previous is not runtime:
        previous.stop()


__all__ = [
    'get_storage_client', 'install_runtime_for_test', 'start_storage',
    'stop_storage', 'storage_authority_status', 'storage_runtime',
    'storage_status',
]

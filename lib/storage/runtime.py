"""Process-wide readiness and crash fencing for application integration."""

from __future__ import annotations

import threading
import time
from typing import Callable

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage.supervisor import StorageSupervisor
from lib.log import get_logger


logger = get_logger('tofu.storage.runtime')


class StorageRuntime:
    """Own the sidecar and fail closed when its process disappears.

    A supervisor may restart the same configured backend, but this state never
    synthesizes a fallback backend and never reports ready before a new health
    handshake succeeds.
    """

    def __init__(
        self,
        supervisor: StorageSupervisor | None = None,
        *,
        on_write_fence: Callable[[], None] | None = None,
        auto_restart: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._ready = False
        self._on_write_fence = on_write_fence
        self._auto_restart = auto_restart
        self._stopping = threading.Event()
        self._restart_thread: threading.Thread | None = None
        self._restart_attempts = 0
        self._last_error = ''
        self._last_exit_code: int | None = None
        self._supervisor = supervisor or StorageSupervisor()
        self._supervisor.add_crash_callback(self._crashed)

    def _crashed(self, code: int) -> None:
        with self._lock:
            self._ready = False
            self._last_exit_code = code
            self._last_error = f'sidecar exited unexpectedly ({code})'
        if self._on_write_fence is not None:
            try:
                self._on_write_fence()
            except Exception as exc:
                logger.error(
                    'Storage write-fence callback failed: %s: %s',
                    type(exc).__name__, str(exc)[:200])
        if self._auto_restart and not self._stopping.is_set():
            logger.warning(
                'Storage writes fenced after sidecar exit code=%d; scheduling restart',
                code)
            with self._lock:
                if self._restart_thread is None or not self._restart_thread.is_alive():
                    self._restart_thread = threading.Thread(
                        target=self._restart_loop,
                        name='storage-restart',
                        daemon=True,
                    )
                    self._restart_thread.start()

    def _restart_loop(self) -> None:
        delay = 0.1
        while not self._stopping.wait(delay):
            try:
                with self._lock:
                    self._restart_attempts += 1
                self._start(clear_stop=False)
                logger.info(
                    'Storage sidecar restart succeeded after %d attempt(s)',
                    self._restart_attempts)
                return
            except Exception as exc:
                # Readiness remains false; retry the same immutable supervisor
                # configuration without choosing another backend.
                logger.warning(
                    'Storage sidecar restart attempt %d failed: %s: %s',
                    self._restart_attempts, type(exc).__name__, str(exc)[:200])
                delay = min(5.0, delay * 2)

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready and self._supervisor.ready

    def status(self) -> dict[str, object]:
        """Return the process-wide readiness state without probing storage."""
        supervisor = self._supervisor.status()
        with self._lock:
            ready = self._ready and bool(supervisor['ready'])
            restarting = bool(
                self._restart_thread is not None
                and self._restart_thread.is_alive()
            )
            if ready:
                state = 'ready'
            elif (self._stopping.is_set()
                    and supervisor['state'] == 'stopped'
                    and not restarting):
                state = 'stopped'
            elif self._stopping.is_set():
                state = 'stopping'
            elif restarting:
                state = 'restarting'
            else:
                state = str(supervisor['state'])
            return {
                **supervisor,
                'ready': ready,
                'state': state,
                'restarting': restarting,
                'restart_attempts': self._restart_attempts,
                'last_exit_code': (
                    self._last_exit_code
                    if self._last_exit_code is not None
                    else supervisor['last_exit_code']
                ),
                'last_error': self._last_error,
            }

    def start(self) -> StorageClient:
        return self._start(clear_stop=True)

    def _start(self, *, clear_stop: bool) -> StorageClient:
        if clear_stop:
            self._stopping.clear()
        elif self._stopping.is_set():
            raise StorageError(
                'database_unavailable', 'Storage runtime is stopping')
        try:
            client = self._supervisor.start()
            health = client.health()
            if not health.get('ready'):
                self._supervisor.stop()
                raise StorageError(
                    'database_unavailable',
                    'Storage sidecar failed its ready handshake')
        except Exception as exc:
            with self._lock:
                self._ready = False
                self._last_error = f'{type(exc).__name__}: {str(exc)[:200]}'
            raise
        stopping_after_handshake = False
        with self._lock:
            # A crash-restart thread can finish its health handshake while the
            # production shutdown owner is concurrently joining it.  Never
            # publish that late authority as ready after stop() won.
            if self._stopping.is_set():
                self._ready = False
                self._last_error = 'Storage runtime stopped during startup'
                stopping_after_handshake = True
            else:
                self._ready = True
                self._last_error = ''
        if stopping_after_handshake:
            self._supervisor.stop()
            raise StorageError(
                'database_unavailable',
                'Storage runtime stopped during startup',
                retryable=True,
            )
        return client

    def client(self, *, write: bool = False) -> StorageClient:
        if not self.ready:
            message = ('Storage writes are fenced while the sidecar is unavailable'
                       if write else 'Storage sidecar is unavailable')
            raise StorageError(
                'database_unavailable', message, retryable=True, retry_after_ms=100)
        return self._supervisor.client

    def stop(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        self._stopping.set()
        with self._lock:
            self._ready = False
            restart_thread = self._restart_thread
        self._supervisor.stop(timeout=max(0.1, deadline - time.monotonic()))
        if (restart_thread is not None
                and restart_thread is not threading.current_thread()
                and restart_thread.is_alive()):
            restart_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if (restart_thread is not None
                and restart_thread is not threading.current_thread()
                and restart_thread.is_alive()):
            # Returning success here used to let the re-exec gate certify the
            # storage boundary while this daemon could still complete a late
            # start and reacquire the project lease.  Fail closed: ordinary
            # process exit releases the owner pipe, and the external manager
            # starts a fresh generation only after the OS lease disappears.
            raise StorageError(
                'database_timeout',
                'Storage restart worker did not stop before the shutdown deadline',
                retryable=True,
            )
        with self._lock:
            if self._restart_thread is restart_thread:
                self._restart_thread = None


__all__ = ['StorageRuntime']

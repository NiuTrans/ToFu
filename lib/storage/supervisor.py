"""Parent-owned lifecycle for the local storage sidecar."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
from typing import Callable

from lib.storage.client import StorageClient
from lib.storage.protocol import PROTOCOL_VERSION
from lib.log import get_logger


logger = get_logger('tofu.storage.supervisor')


class StorageSupervisor:
    """Launch, authenticate, monitor, and stop one sidecar process.

    The authentication token is passed only in the child's environment.  The
    random port returns on the child's stdout control channel; neither value is
    written to a runtime file or included in argv.
    """

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] | None = None,
        backend: str | None = None,
        startup_timeout: float = 30.0,
        on_crash: Callable[[int], None] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve() if project_root else None
        self._backend = backend
        self._startup_timeout = max(1.0, float(startup_timeout))
        self._crash_callbacks: list[Callable[[int], None]] = []
        if on_crash is not None:
            self._crash_callbacks.append(on_crash)
        self._process: subprocess.Popen[str] | None = None
        self._client: StorageClient | None = None
        self._token = ''
        self._intentional_stop = False
        self._starting = False
        self._ready_backend: str | None = None
        self._endpoint_port: int | None = None
        self._last_exit_code: int | None = None
        self._monitor: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def client(self) -> StorageClient:
        with self._lock:
            if self._client is None or not self.ready:
                raise RuntimeError('storage sidecar is not ready')
            return self._client

    @property
    def ready(self) -> bool:
        process = self._process
        return bool(process is not None and process.poll() is None and self._client)

    def status(self) -> dict[str, object]:
        """Return an in-memory snapshot; never performs an RPC or disk read."""
        with self._lock:
            process = self._process
            alive = process is not None and process.poll() is None
            ready = bool(alive and self._client is not None)
            if ready:
                state = 'ready'
            elif self._starting:
                state = 'starting'
            elif alive:
                state = 'unready'
            elif self._last_exit_code is not None and not self._intentional_stop:
                state = 'exited'
            else:
                state = 'stopped'
            configured_backend = (
                self._backend
                or (os.environ.get('TOFU_DB_BACKEND') or 'sqlite').strip().lower()
            )
            return {
                'ready': ready,
                'state': state,
                'backend': self._ready_backend or configured_backend,
                'pid': process.pid if alive else None,
                'port': self._endpoint_port if ready else None,
                'last_exit_code': self._last_exit_code,
            }

    def start(self) -> StorageClient:
        with self._lock:
            if self.ready:
                return self._client  # type: ignore[return-value]
            if self._starting:
                raise RuntimeError('storage sidecar startup is already in progress')
            self._intentional_stop = False
            self._starting = True
            self._ready_backend = None
            self._endpoint_port = None
            self._last_exit_code = None
            self._token = secrets.token_urlsafe(48)
            env = os.environ.copy()
            env['TOFU_STORAGE_TOKEN'] = self._token
            if self._backend is not None:
                env['TOFU_DB_BACKEND'] = self._backend
            if self._project_root is not None:
                env['TOFU_STORAGE_PROJECT_ROOT'] = str(self._project_root)
                env['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
            creationflags = 0
            if os.name == 'nt':  # pragma: no cover - exercised on Windows CI
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            try:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'lib.storage_sidecar'],
                    # Import the installed/source package from its own root.  A
                    # test project-root override controls persistent paths only;
                    # it is not necessarily a Python checkout.
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    bufsize=1,
                    close_fds=(os.name != 'nt'),
                    creationflags=creationflags,
                )
            except BaseException:
                self._starting = False
                self._token = ''
                raise
            self._process = process

        lines: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_ready() -> None:
            assert process.stdout is not None
            lines.put(process.stdout.readline())

        reader = threading.Thread(target=read_ready, name='storage-ready', daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=self._startup_timeout)
        except queue.Empty as exc:
            self._abort_failed_start(process)
            raise RuntimeError('storage sidecar startup timed out') from exc
        if process.poll() is not None and not line:
            code = process.returncode
            self._abort_failed_start(process)
            raise RuntimeError(f'storage sidecar exited during startup ({code})')
        try:
            ready = json.loads(line)
            if (ready.get('type') != 'storage.ready'
                    or ready.get('protocol') != PROTOCOL_VERSION
                    or not isinstance(ready.get('port'), int)
                    or ready.get('backend') not in {'sqlite', 'postgres'}):
                raise ValueError('invalid ready envelope')
            expected_backend = (
                self._backend
                or (env.get('TOFU_DB_BACKEND') or 'sqlite').strip().lower()
            )
            if ready['backend'] != expected_backend:
                raise RuntimeError('storage sidecar started an unexpected backend')
            client = StorageClient('127.0.0.1', ready['port'], self._token)
            health = client.health(deadline=2.0)
            if (not health.get('ready')
                    or health.get('backend') != ready['backend']):
                raise RuntimeError('sidecar health preflight did not pass')
        except Exception:
            self._abort_failed_start(process)
            raise
        with self._lock:
            self._client = client
            self._starting = False
            self._ready_backend = ready['backend']
            self._endpoint_port = ready['port']
            self._monitor = threading.Thread(
                target=self._monitor_process,
                args=(process,),
                name='storage-supervisor',
                daemon=True,
            )
            self._monitor.start()
            logger.info(
                'Storage sidecar ready pid=%d backend=%s endpoint=127.0.0.1:%d',
                process.pid, self._ready_backend, self._endpoint_port)
            return client

    def _abort_failed_start(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        with self._lock:
            self._last_exit_code = process.returncode
            self._client = None
            self._process = None
            self._token = ''
            self._starting = False
            self._ready_backend = None
            self._endpoint_port = None

    def _monitor_process(self, process: subprocess.Popen[str]) -> None:
        return_code = process.wait()
        with self._lock:
            if process is not self._process:
                return
            crashed = not self._intentional_stop
            self._last_exit_code = return_code
            self._client = None
            self._process = None
            self._token = ''
            self._starting = False
            self._ready_backend = None
            self._endpoint_port = None
        if crashed:
            logger.critical(
                'Storage sidecar exited unexpectedly exit_code=%d', return_code)
            for callback in tuple(self._crash_callbacks):
                try:
                    callback(return_code)
                except Exception:
                    logger.exception('Storage crash callback failed')
        else:
            logger.info('Storage sidecar stopped exit_code=%d', return_code)

    def add_crash_callback(self, callback: Callable[[int], None]) -> None:
        if not callable(callback):
            raise TypeError('storage crash callback must be callable')
        with self._lock:
            self._crash_callbacks.append(callback)

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            process = self._process
            self._intentional_stop = True
            self._client = None
            self._starting = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        with self._lock:
            if self._process is process:
                self._process = None
            self._token = ''
            self._ready_backend = None
            self._endpoint_port = None

    def wait_until_unready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self.ready and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self.ready

    def __enter__(self) -> 'StorageSupervisor':
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


__all__ = ['StorageSupervisor']

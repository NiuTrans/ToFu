"""Application-owned readiness supervision for the local Storage Sidecar."""

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
from lib.storage.connection_file import (
    read_connection_file,
    resolve_connection_file,
)
from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage.startup_budget import (
    ORDINARY_STORAGE_STARTUP_TIMEOUT_S,
    storage_startup_timeout,
)
from lib.storage.startup_control import (
    MAX_STARTUP_CONTROL_LINE_CHARS,
    STARTUP_ERROR_TYPE,
    STARTUP_PROGRESS_TYPE,
    STARTUP_READY_TYPE,
    StartupProgress,
    parse_startup_progress,
)
from lib.log import get_logger
from runtime_guards import install_process_resource_defaults


logger = get_logger('tofu.storage.supervisor')


class _StartupDeadline:
    """A renewable stall deadline inside one immutable hard boot budget."""

    def __init__(
        self,
        *,
        started_at: float,
        stall_timeout: float,
        hard_timeout: float,
    ) -> None:
        self._stall_timeout = max(0.001, float(stall_timeout))
        self._hard_deadline = started_at + max(
            self._stall_timeout, float(hard_timeout))
        self._stall_deadline = started_at + self._stall_timeout
        self._phase = ''
        self._completed_bytes = 0
        self._total_bytes = 0

    def remaining(self, now: float) -> float:
        return max(
            0.0,
            min(self._stall_deadline, self._hard_deadline) - now,
        )

    def observe(self, progress: StartupProgress, *, now: float) -> bool:
        """Record monotonic work; return whether the stall deadline renewed."""
        phase_changed = progress.phase != self._phase
        if not phase_changed:
            if progress.total_bytes != self._total_bytes:
                raise ValueError(
                    'storage startup progress total changed within one phase')
            if progress.completed_bytes < self._completed_bytes:
                raise ValueError('storage startup progress moved backwards')
        advanced = bool(
            phase_changed
            or progress.completed_bytes > self._completed_bytes
            or progress.heartbeat
        )
        self._phase = progress.phase
        self._completed_bytes = progress.completed_bytes
        self._total_bytes = progress.total_bytes
        if advanced:
            self._stall_deadline = now + self._stall_timeout
        return advanced

    def timeout_error(self, *, now: float) -> RuntimeError:
        detail = (
            f' phase={self._phase} '
            f'progress={self._completed_bytes}/{self._total_bytes}'
            if self._phase else '')
        if now >= self._hard_deadline:
            return RuntimeError(
                'storage sidecar startup exceeded its hard timeout' + detail)
        return RuntimeError(
            'storage sidecar startup stalled without progress' + detail)


class StorageSupervisor:
    """Authenticate and monitor one child or co-container Sidecar.

    Personal mode launches a child and receives its token/port through the
    environment and stdout control channel. Kubernetes mode attaches to the
    independently managed co-container through a private connection file; it
    never owns or terminates that container. Both modes revoke readiness and
    notify crash callbacks when the authenticated health channel disappears.
    """

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] | None = None,
        backend: str | None = None,
        connection_file: str | os.PathLike[str] | None = None,
        startup_timeout: float | None = None,
        startup_stall_timeout: float | None = None,
        on_crash: Callable[[int], None] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve() if project_root else None
        self._backend = backend
        raw_connection_file = connection_file
        if raw_connection_file is None:
            raw_connection_file = os.environ.get(
                'TOFU_STORAGE_CONNECTION_FILE', '').strip() or None
        self._connection_file = (
            resolve_connection_file(raw_connection_file)
            if raw_connection_file is not None else None
        )
        self._startup_timeout = (
            storage_startup_timeout(os.environ)
            if startup_timeout is None
            else max(1.0, float(startup_timeout))
        )
        self._startup_stall_timeout = min(
            self._startup_timeout,
            max(
                0.001,
                float(startup_stall_timeout)
                if startup_stall_timeout is not None
                else ORDINARY_STORAGE_STARTUP_TIMEOUT_S,
            ),
        )
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
        self._external_attached = False
        self._external_monitor_stop = threading.Event()
        self._lock = threading.RLock()

    @property
    def client(self) -> StorageClient:
        with self._lock:
            if self._client is None or not self.ready:
                raise RuntimeError('storage sidecar is not ready')
            return self._client

    @property
    def ready(self) -> bool:
        if self._connection_file is not None:
            return bool(self._external_attached and self._client is not None)
        process = self._process
        return bool(process is not None and process.poll() is None and self._client)

    def status(self) -> dict[str, object]:
        """Return an in-memory snapshot; never performs an RPC or disk read."""
        with self._lock:
            process = self._process
            child_alive = process is not None and process.poll() is None
            alive = (
                self._external_attached
                if self._connection_file is not None
                else child_alive
            )
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
            if self._backend is not None:
                configured_backend = self._backend
            else:
                from runtime_guards import load_deployment_configuration
                configured_backend = load_deployment_configuration(
                    allow_test_backend_override=(
                        os.environ.get(
                            'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1')
                ).storage_backend
            return {
                'ready': ready,
                'state': state,
                'backend': self._ready_backend or configured_backend,
                'pid': process.pid if child_alive else None,
                'port': self._endpoint_port if ready else None,
                'last_exit_code': self._last_exit_code,
            }

    def start(self) -> StorageClient:
        if self._connection_file is not None:
            return self._attach_external_sidecar()
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
            if self._project_root is not None:
                env['TOFU_PROJECT_PATH'] = str(self._project_root)
            install_process_resource_defaults(env)
            env['TOFU_STORAGE_TOKEN'] = self._token
            # The sidecar is a child authority, never an independent daemon.
            # Give it an explicit owner identity so it can release the project
            # lease if this worker is SIGKILLed or otherwise disappears before
            # the normal stop() path runs.
            env['TOFU_STORAGE_PARENT_PID'] = str(os.getpid())
            if self._backend is not None:
                env['TOFU_STORAGE_TEST_BACKEND'] = self._backend
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
                    # The parent keeps this write end open for exactly one
                    # server-process image.  Python creates it close-on-exec, so
                    # parent death *or an in-place execv with the same PID*
                    # delivers EOF to the Sidecar ownership watcher.
                    stdin=subprocess.PIPE,
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
            try:
                owner_channel_is_close_on_exec = bool(
                    process.stdin is not None
                    and not os.get_inheritable(process.stdin.fileno())
                )
            except (OSError, ValueError):
                owner_channel_is_close_on_exec = False
            if not owner_channel_is_close_on_exec:
                self._abort_failed_start(process)
                raise RuntimeError(
                    'storage sidecar owner channel is not close-on-exec')
        try:
            ready = self._read_startup_envelope(process)
            if isinstance(ready, dict) and ready.get('type') == STARTUP_ERROR_TYPE:
                code = str(ready.get('code') or 'database_unavailable')[:80]
                diagnostic = str(
                    ready.get('diagnostic') or 'startup was refused')[:500]
                raise RuntimeError(
                    f'storage sidecar startup refused ({code}): {diagnostic}')
            if not isinstance(ready, dict):
                raise ValueError('invalid ready envelope')
            if (ready.get('type') != STARTUP_READY_TYPE
                    or ready.get('protocol') != PROTOCOL_VERSION
                    or not isinstance(ready.get('port'), int)
                    or ready.get('backend') not in {'sqlite', 'postgres'}):
                raise ValueError('invalid ready envelope')
            if self._backend is not None:
                expected_backend = self._backend
            else:
                from runtime_guards import load_deployment_configuration
                expected_backend = load_deployment_configuration(
                    env,
                    allow_test_backend_override=(
                        env.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1'),
                ).storage_backend
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

    def _read_startup_envelope(
        self,
        process: subprocess.Popen[str],
    ) -> object:
        """Consume bounded progress frames until the final startup envelope."""
        messages: queue.Queue[str] = queue.Queue(maxsize=8)

        def read_messages() -> None:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline(
                    MAX_STARTUP_CONTROL_LINE_CHARS + 2)
                messages.put(line)
                if not line:
                    return
                # Progress frames are the only non-terminal control messages.
                # Stop reading as soon as any final/invalid envelope has been
                # handed to the validating owner below; otherwise this daemon
                # remains blocked on the live child's stdout for the entire
                # server lifetime after readiness.
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    return
                if (not isinstance(message, dict)
                        or message.get('type') != STARTUP_PROGRESS_TYPE):
                    return

        reader = threading.Thread(
            target=read_messages,
            name='storage-startup-control',
            daemon=True,
        )
        reader.start()
        started_at = time.monotonic()
        deadline = _StartupDeadline(
            started_at=started_at,
            stall_timeout=self._startup_stall_timeout,
            hard_timeout=self._startup_timeout,
        )
        last_log_at = 0.0
        last_logged_phase = ''
        while True:
            now = time.monotonic()
            remaining = deadline.remaining(now)
            if remaining <= 0:
                raise deadline.timeout_error(now=now)
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise deadline.timeout_error(now=time.monotonic()) from exc
            if not line.strip():
                # EOF is not JSON. Reporting json.loads('') used to hide the
                # child failure behind JSONDecodeError. Give it a brief chance
                # to publish an exit code for one stable diagnostic.
                try:
                    code = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    code = process.poll()
                suffix = f' (exit {code})' if code is not None else ''
                raise RuntimeError(
                    'storage sidecar closed its readiness channel before startup'
                    + suffix)
            if len(line) > MAX_STARTUP_CONTROL_LINE_CHARS + 1:
                raise RuntimeError(
                    'storage sidecar returned an oversized startup response')
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(
                    'storage sidecar returned an invalid startup response') from exc
            if isinstance(message, dict):
                try:
                    progress = parse_startup_progress(message)
                    if progress is not None:
                        deadline.observe(progress, now=time.monotonic())
                except ValueError as exc:
                    raise RuntimeError(
                        'storage sidecar returned invalid startup progress') from exc
                if progress is not None:
                    now = time.monotonic()
                    if (progress.phase != last_logged_phase
                            or progress.completed_bytes == progress.total_bytes
                            or now - last_log_at >= 10.0):
                        logger.info(
                            'Storage sidecar startup progress phase=%s '
                            'bytes=%d/%d heartbeat=%s',
                            progress.phase,
                            progress.completed_bytes,
                            progress.total_bytes,
                            progress.heartbeat,
                        )
                        last_log_at = now
                        last_logged_phase = progress.phase
                    continue
            return message

    def _configured_backend(self) -> str:
        if self._backend is not None:
            return self._backend
        from runtime_guards import load_deployment_configuration

        return load_deployment_configuration(
            allow_test_backend_override=(
                os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1')
        ).storage_backend

    def _attach_external_sidecar(self) -> StorageClient:
        """Attach to a co-located container through its private handoff file."""
        assert self._connection_file is not None
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
            self._external_attached = False
            self._external_monitor_stop.clear()

        deadline = time.monotonic() + self._startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                connection = read_connection_file(self._connection_file)
                expected_backend = self._configured_backend()
                if connection['backend'] != expected_backend:
                    raise RuntimeError(
                        'storage connection file selected an unexpected backend')
                client = StorageClient(
                    connection['host'], connection['port'], connection['token'])
                health = client.health(deadline=2.0)
                if (not health.get('ready')
                        or health.get('backend') != expected_backend):
                    raise RuntimeError(
                        'external storage sidecar failed its health handshake')
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            with self._lock:
                self._starting = False
                self._client = None
                self._external_attached = False
            raise RuntimeError(
                'external storage sidecar startup timed out') from last_error

        with self._lock:
            self._client = client
            self._token = connection['token']
            self._starting = False
            self._external_attached = True
            self._ready_backend = connection['backend']
            self._endpoint_port = connection['port']
            self._monitor = threading.Thread(
                target=self._monitor_external_sidecar,
                args=(client,),
                name='storage-external-monitor',
                daemon=True,
            )
            self._monitor.start()
        logger.info(
            'Attached external Storage Sidecar backend=%s endpoint=%s:%d',
            connection['backend'], connection['host'], connection['port'])
        return client

    def _monitor_external_sidecar(self, client: StorageClient) -> None:
        """Fence the application after a co-container health handshake fails."""
        while not self._external_monitor_stop.wait(1.0):
            try:
                health = client.health(deadline=1.5)
                if health.get('ready'):
                    continue
            except Exception as exc:
                logger.debug('storage health monitor probe failed: %s', exc)
            with self._lock:
                if client is not self._client or self._intentional_stop:
                    return
                self._last_exit_code = -1
                self._client = None
                self._external_attached = False
                self._ready_backend = None
                self._endpoint_port = None
            logger.critical('External Storage Sidecar became unavailable')
            for callback in tuple(self._crash_callbacks):
                try:
                    callback(-1)
                except Exception:
                    logger.exception('Storage crash callback failed')
            return

    def _abort_failed_start(self, process: subprocess.Popen[str]) -> None:
        self._close_owner_channel(process)
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

    @staticmethod
    def _close_owner_channel(process: subprocess.Popen[str]) -> None:
        """Close the one-image parent lease without leaking a pipe descriptor."""
        channel = process.stdin
        if channel is None:
            return
        try:
            channel.close()
        except (OSError, ValueError):
            # Process exit and concurrent stop can close the same wrapper.
            pass

    def _monitor_process(self, process: subprocess.Popen[str]) -> None:
        return_code = process.wait()
        self._close_owner_channel(process)
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
        if self._connection_file is not None:
            with self._lock:
                self._intentional_stop = True
                self._client = None
                self._starting = False
                self._external_attached = False
                self._ready_backend = None
                self._endpoint_port = None
                monitor = self._monitor
            self._external_monitor_stop.set()
            if (monitor is not None
                    and monitor is not threading.current_thread()
                    and monitor.is_alive()):
                monitor.join(timeout=max(0.0, timeout))
            return
        with self._lock:
            process = self._process
            self._intentional_stop = True
            self._client = None
            self._starting = False
        if process is None:
            return
        if process.poll() is None:
            # EOF is the authority-ownership signal and also covers execv,
            # where the parent PID is intentionally stable.  SIGTERM remains a
            # prompt graceful-stop nudge; bounded kill is the final backstop.
            self._close_owner_channel(process)
            process.terminate()
            try:
                process.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self._close_owner_channel(process)
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

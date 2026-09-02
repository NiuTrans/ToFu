"""Sidecar entry point with stdout startup and stdin ownership channels."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import threading

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage.startup_control import (
    STARTUP_ERROR_TYPE,
    STARTUP_READY_TYPE,
    encode_startup_progress,
)
from lib.storage.connection_file import (
    remove_connection_file,
    resolve_connection_file,
    write_connection_file,
)
from lib.storage_sidecar.adapters import create_backend
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.logical_outbox import LogicalOutboxPipeline
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.server import create_server


def _configured_parent_pid() -> int | None:
    """Return the supervisor PID, or None for an explicitly standalone run."""
    raw = (os.environ.get('TOFU_STORAGE_PARENT_PID') or '').strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('invalid TOFU_STORAGE_PARENT_PID') from exc
    if parent_pid <= 1:
        raise RuntimeError('invalid TOFU_STORAGE_PARENT_PID')
    return parent_pid


def _start_parent_watch(
    expected_pid: int | None,
    request_stop,
    *,
    ownership_stream=None,
    interval: float = 0.25,
) -> threading.Event | None:
    """Stop the child authority when its owning process image disappears.

    Production supervision supplies a pipe whose parent write end is
    close-on-exec.  EOF therefore covers ordinary parent death, subreaper
    adoption, PID reuse, and the important ``execv`` case where the worker PID
    intentionally stays unchanged.  The PPID poll remains a compatibility
    fallback for an older/manual launcher without the ownership channel.
    """
    if expected_pid is None:
        return None
    stopped = threading.Event()
    ownership_descriptor = None
    if ownership_stream is not None:
        try:
            ownership_descriptor = ownership_stream.fileno()
            os.set_blocking(ownership_descriptor, False)
        except (AttributeError, OSError, ValueError) as exc:
            raise RuntimeError(
                'storage parent ownership channel is unavailable') from exc

    def watch() -> None:
        while not stopped.wait(interval):
            if ownership_descriptor is not None:
                chunk = None
                try:
                    chunk = os.read(ownership_descriptor, 1)
                except BlockingIOError:
                    pass
                except OSError:
                    if not stopped.is_set():
                        request_stop()
                    return
                if chunk == b'':
                    if not stopped.is_set():
                        request_stop()
                    return
            try:
                parent_changed = os.getppid() != expected_pid
            except OSError:
                parent_changed = True
            if parent_changed:
                if not stopped.is_set():
                    request_stop()
                return

    threading.Thread(
        target=watch,
        name='storage-parent-watch',
        daemon=True,
    ).start()
    return stopped


def _write_startup_error(code: str, diagnostic: str) -> None:
    """Best-effort structured failure on the parent control channel."""
    try:
        sys.stdout.write(json.dumps({
            'type': STARTUP_ERROR_TYPE,
            'protocol': PROTOCOL_VERSION,
            'code': str(code or 'database_unavailable')[:80],
            'diagnostic': str(diagnostic or 'storage startup failed')[:500],
        }, separators=(',', ':')) + '\n')
        sys.stdout.flush()
    except OSError:
        # A dead parent closes the pipe; cleanup in finally still matters.
        pass


def _write_startup_progress(
    phase: str,
    completed_bytes: int,
    total_bytes: int,
    *,
    heartbeat: bool = False,
) -> None:
    """Publish one credential-free observation to the owning supervisor."""
    sys.stdout.write(encode_startup_progress(
        phase,
        completed_bytes,
        total_bytes,
        heartbeat=heartbeat,
    ) + '\n')
    sys.stdout.flush()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    log = get_logger('tofu.storage.sidecar')
    backend = None
    logical_outbox = None
    lease = None
    server = None
    parent_watch = None
    connection_file = None
    connection_token = ''
    ready_sent = False
    try:
        expected_parent_pid = _configured_parent_pid()
        raw_connection_file = os.environ.get(
            'TOFU_STORAGE_CONNECTION_FILE', '').strip()
        if raw_connection_file:
            if expected_parent_pid is not None:
                raise RuntimeError(
                    'storage connection-file mode cannot use a parent PID')
            connection_file = resolve_connection_file(raw_connection_file)
            connection_token = secrets.token_urlsafe(48)
            os.environ['TOFU_STORAGE_TOKEN'] = connection_token
        config = SidecarConfig.from_environment()
        lease = ProjectLease(
            config.data_dir, expected_parent_pid=expected_parent_pid)
        lease.acquire()
        # A co-container publishes readiness through its private connection
        # file, so its stdout remains the legacy final-envelope-only stream.
        # A child Sidecar has an owning supervisor that can consume bounded
        # progress before readiness and distinguish slow work from a stall.
        startup_progress = (
            _write_startup_progress
            if expected_parent_pid is not None and connection_file is None
            else None
        )
        backend = create_backend(
            config, startup_progress=startup_progress)
        backend.start()
        logical_outbox = LogicalOutboxPipeline.from_config(config, backend)
        logical_outbox.start()
        server = create_server(
            backend,
            config.token,
            rpc_capacity=config.rpc_capacity,
            read_only_preview=config.distributed_preview_read_only,
            logical_outbox=logical_outbox,
            idle_trim_rss_bytes=config.idle_trim_rss_mib * 1024 * 1024,
            idle_trim_cooldown_s=config.idle_trim_cooldown_s,
        )

        stop_requested = threading.Event()
        stop_request_lock = threading.Lock()

        def request_stop(_signum=None, _frame=None):
            if server is None:
                return
            with stop_request_lock:
                if stop_requested.is_set():
                    return
                stop_requested.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        parent_watch = _start_parent_watch(
            expected_parent_pid,
            request_stop,
            ownership_stream=(
                sys.stdin.buffer if expected_parent_pid is not None else None
            ),
        )
        # Close the small race where the worker died before the watcher thread
        # got scheduled.  Do not advertise readiness to a nonexistent owner.
        if expected_parent_pid is not None and os.getppid() != expected_parent_pid:
            raise StorageError(
                'database_unavailable', 'Storage supervisor exited during startup')
        ready = {
            'type': STARTUP_READY_TYPE,
            'protocol': PROTOCOL_VERSION,
            'port': int(server.server_address[1]),
            'backend': config.backend,
        }
        if connection_file is not None:
            write_connection_file(
                connection_file,
                host='127.0.0.1',
                port=ready['port'],
                token=config.token,
                backend=config.backend,
            )
        # This is the final stdout message.  A supervised child may have sent
        # bounded progress envelopes first; none contains the token, database
        # paths, credentials, or authority contents.
        sys.stdout.write(json.dumps(ready, separators=(',', ':')) + '\n')
        sys.stdout.flush()
        ready_sent = True
        server.serve_forever(poll_interval=0.2)
        return 0
    except StorageError as exc:
        log.critical('storage startup refused code=%s diagnostic=%s', exc.code, exc.message)
        if not ready_sent:
            _write_startup_error(exc.code, exc.message)
        return 2
    except BaseException as exc:
        log.critical('storage startup failed type=%s', type(exc).__name__, exc_info=True)
        if not ready_sent:
            _write_startup_error(
                'database_unavailable',
                f'storage startup failed ({type(exc).__name__})',
            )
        return 2
    finally:
        if connection_file is not None and connection_token:
            remove_connection_file(
                Path(connection_file), token=connection_token)
        if parent_watch is not None:
            parent_watch.set()
        if server is not None:
            server.server_close()
        if logical_outbox is not None:
            logical_outbox.close()
        if backend is not None:
            backend.close()
        if lease is not None:
            lease.release()


if __name__ == '__main__':
    raise SystemExit(main())

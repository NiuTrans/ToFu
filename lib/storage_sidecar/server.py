"""Authenticated loopback RPC server for ``storage.v1``."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import socket
import socketserver
import threading
import time
from typing import Any, Callable, Mapping

import psutil

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage.protocol import (
    PROTOCOL_VERSION, canonical_json, recv_frame, send_frame, validate_operation,
)
from lib.storage_sidecar.operations import resolve_operation


logger = get_logger('tofu.storage.sidecar')

# Keep the active-handler limit strict while absorbing one short scheduling
# burst in the already-bounded TCP backlog.  ``process_request`` runs on the
# accept-loop thread, so at most one accepted socket waits here; later sockets
# remain capped by ``request_queue_size``.  Sustained saturation still gets the
# classified rejection below instead of growing threads or an application
# queue without bound.
_RPC_ADMISSION_WAIT_S = 0.1


def _malloc_trim() -> bool:
    """Return free glibc arenas to the OS; fail closed off glibc."""
    try:
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        return bool(libc.malloc_trim(ctypes.c_size_t(0)))
    except (OSError, AttributeError, TypeError) as exc:
        logger.debug('storage idle malloc_trim unavailable: %s', exc)
        return False


class _IdleHeapTrimmer:
    """Cooldown-bound Sidecar heap return, called only at an RPC idle edge."""

    def __init__(
        self,
        *,
        threshold_bytes: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
        rss_bytes: Callable[[], int] | None = None,
        trim: Callable[[], bool] = _malloc_trim,
    ) -> None:
        self.threshold_bytes = max(0, int(threshold_bytes))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._clock = clock
        self._rss_bytes = rss_bytes or (
            lambda: int(psutil.Process().memory_info().rss))
        self._trim = trim
        self._lock = threading.Lock()
        self._last_check_at: float | None = None
        self._attempts = 0
        self._successes = 0
        self._reclaimed_bytes = 0
        self._last_before_bytes = 0
        self._last_after_bytes = 0

    def maybe_trim(self) -> dict[str, int] | None:
        """Trim once above threshold and outside the configured cooldown."""
        if self.threshold_bytes <= 0:
            return None
        now = self._clock()
        with self._lock:
            if (self._last_check_at is not None
                    and now - self._last_check_at < self.cooldown_s):
                return None
            self._last_check_at = now
            try:
                before = max(0, int(self._rss_bytes()))
            except (OSError, ValueError, TypeError, psutil.Error) as exc:
                logger.debug('storage idle RSS probe failed: %s', exc)
                return None
            if before < self.threshold_bytes:
                return None
            self._attempts += 1
            try:
                trimmed = self._trim()
            except Exception as exc:  # best-effort memory relief only
                logger.debug('storage idle heap trim failed: %s', exc)
                trimmed = False
            try:
                after = max(0, int(self._rss_bytes()))
            except (OSError, ValueError, TypeError, psutil.Error) as exc:
                logger.debug('storage post-trim RSS probe failed: %s', exc)
                after = before
            reclaimed = max(0, before - after)
            self._successes += int(bool(trimmed))
            self._reclaimed_bytes += reclaimed
            self._last_before_bytes = before
            self._last_after_bytes = after
            result = {
                'before_bytes': before,
                'after_bytes': after,
                'reclaimed_bytes': reclaimed,
            }
        log = logger.info if reclaimed >= 1024 * 1024 else logger.debug
        log(
            'storage idle heap trim: %.1fMiB -> %.1fMiB '
            '(reclaimed %.1fMiB, threshold %.1fMiB)',
            before / (1024 * 1024),
            after / (1024 * 1024),
            reclaimed / (1024 * 1024),
            self.threshold_bytes / (1024 * 1024),
        )
        return result

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                'idle_trim_attempts': self._attempts,
                'idle_trim_successes': self._successes,
                'idle_trim_reclaimed_bytes': self._reclaimed_bytes,
                'idle_trim_last_before_bytes': self._last_before_bytes,
                'idle_trim_last_after_bytes': self._last_after_bytes,
            }


class _StorageTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 16
    rpc_capacity = 8

    def __init__(
        self, address, handler, *, backend, token: str, rpc_capacity: int,
        read_only_preview: bool, logical_outbox=None,
        idle_trim_rss_bytes: int = 0,
        idle_trim_cooldown_s: float = 300.0,
    ):
        self.rpc_capacity = max(2, min(256, int(rpc_capacity)))
        # The kernel accept backlog should absorb a short scheduling burst, not
        # become a second hidden concurrency reservoir retaining sockets/Fds.
        self.request_queue_size = max(4, min(128, self.rpc_capacity * 2))
        self._rpc_slots = threading.BoundedSemaphore(self.rpc_capacity)
        self._rpc_metrics_lock = threading.Lock()
        self._rpc_active = 0
        self._rpc_waiting = 0
        self._rpc_rejected = 0
        self._idle_heap_trimmer = _IdleHeapTrimmer(
            threshold_bytes=idle_trim_rss_bytes,
            cooldown_s=idle_trim_cooldown_s,
        )
        super().__init__(address, handler, bind_and_activate=True)
        self.backend = backend
        self.token = token
        self.read_only_preview = bool(read_only_preview)
        self.logical_outbox = logical_outbox

    def process_request(self, request, client_address) -> None:
        acquired = self._rpc_slots.acquire(blocking=False)
        if not acquired:
            with self._rpc_metrics_lock:
                self._rpc_waiting += 1
            try:
                acquired = self._rpc_slots.acquire(timeout=_RPC_ADMISSION_WAIT_S)
            finally:
                with self._rpc_metrics_lock:
                    self._rpc_waiting -= 1
        if not acquired:
            with self._rpc_metrics_lock:
                self._rpc_rejected += 1
            self._reject_over_capacity(request)
            return
        with self._rpc_metrics_lock:
            self._rpc_active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_rpc_slot()
            raise

    def _reject_over_capacity(self, request) -> None:
        """Answer an over-capacity connection with a CLASSIFIED retryable frame.

        A bare ``shutdown_request`` left the client reading EOF mid-frame —
        an opaque error that bypassed every retry path, so a capacity blip
        became a user-visible stream failure.  Send 'database_unavailable'
        in-band instead: the client's idempotent-read retry loop then rides
        over the burst, and command callers get a correctly classified
        transient.  The request frame is deliberately NEVER read here (a
        drip-fed 64 MiB frame must not park the accept loop), so the id
        cannot be echoed; the client admits a classified error envelope
        without a matching id.  The bounded send keeps a half-dead client
        from blocking acceptance — loopback-only, so a blackholed peer is
        not a realistic stall.
        """
        try:
            request.settimeout(1.0)
            send_frame(request, {
                'protocol': PROTOCOL_VERSION,
                'request_id': '',
                'ok': False,
                'error': StorageError(
                    'database_unavailable',
                    'Storage sidecar is at capacity', True, 100,
                ).to_payload(),
            })
        except (OSError, StorageError):
            pass
        finally:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_rpc_slot()

    def _release_rpc_slot(self) -> None:
        with self._rpc_metrics_lock:
            self._rpc_active -= 1
            if self._rpc_active == 0:
                # Holding the admission lock prevents a newly accepted request
                # from starting while this infrequent process-wide trim runs.
                # The semaphore is released immediately afterwards.
                self._idle_heap_trimmer.maybe_trim()
        self._rpc_slots.release()

    def rpc_metrics(self) -> dict[str, int]:
        with self._rpc_metrics_lock:
            metrics = {
                'active': self._rpc_active,
                'capacity': self.rpc_capacity,
                'waiting': self._rpc_waiting,
                'rejected': self._rpc_rejected,
            }
        metrics.update(self._idle_heap_trimmer.metrics())
        return metrics

    @staticmethod
    def process_metrics() -> dict[str, int]:
        process = psutil.Process()
        handles = (process.num_fds() if hasattr(process, 'num_fds')
                   else process.num_handles())
        return {
            'rss_bytes': int(process.memory_info().rss),
            'open_fds_or_handles': int(handles),
            'threads': int(process.num_threads()),
        }


class _StorageHandler(socketserver.BaseRequestHandler):
    server: _StorageTCPServer

    def handle(self) -> None:
        request_id = ''
        operation = ''
        # Authentication is inside the first frame.  Bound unauthenticated
        # half-open clients so they cannot retain a worker/FD indefinitely.
        self.request.settimeout(5.0)
        try:
            request = recv_frame(self.request)
            request_id = str(request.get('request_id') or '')
            operation = str(request.get('operation') or '')
            response = self._dispatch(request, request_id)
        except (TimeoutError, socket.timeout):
            logger.debug(
                'storage frame deadline elapsed operation_id=%s', request_id)
            response = {
                'protocol': PROTOCOL_VERSION,
                'request_id': request_id,
                'ok': False,
                'error': StorageError(
                    'database_timeout', 'Storage frame timed out', True, 25,
                    request_id,
                ).to_payload(),
            }
        except StorageError as exc:
            exc.operation_id = exc.operation_id or request_id
            logger.debug(
                'classified storage request failure code=%s operation_id=%s',
                exc.code, request_id)
            response = {
                'protocol': PROTOCOL_VERSION,
                'request_id': request_id,
                'ok': False,
                'error': exc.to_payload(),
            }
        except BaseException:
            logger.exception('unclassified storage request failure operation_id=%s', request_id)
            response = {
                'protocol': PROTOCOL_VERSION,
                'request_id': request_id,
                'ok': False,
                'error': StorageError(
                    'database_internal', 'Internal storage failure',
                    operation_id=request_id,
                ).to_payload(),
            }
        self._send_response(response, request_id, operation)

    def _send_response(
        self, response: Mapping[str, Any], request_id: str, operation: str
    ) -> None:
        """Send a result, falling back to a small classified encoding error.

        ``send_frame`` encodes before writing.  A result over the 64 MiB
        contract therefore raises ``StorageError`` with zero bytes sent; the
        old handler swallowed that exception and closed the socket, so clients
        saw the misleading retryable ``connection closed mid-frame``.  A
        second, bounded error frame is safe in this exact branch and preserves
        the real diagnosis.  OSError may mean a partial network write, so that
        branch still closes without attempting a second frame.
        """
        try:
            send_frame(self.request, response)
            return
        except StorageError as exc:
            exc.operation_id = exc.operation_id or request_id
            logger.warning(
                'storage response encoding failed code=%s operation=%s '
                'operation_id=%s: %s',
                exc.code, operation or 'unknown', request_id, exc.message)
            fallback = {
                'protocol': PROTOCOL_VERSION,
                'request_id': request_id,
                'ok': False,
                'error': exc.to_payload(),
            }
            try:
                send_frame(self.request, fallback)
                return
            except (OSError, StorageError):
                logger.debug(
                    'storage fallback response channel closed operation=%s '
                    'operation_id=%s', operation or 'unknown', request_id)
                return
        except OSError:
            logger.debug(
                'storage response channel closed operation=%s operation_id=%s',
                operation or 'unknown', request_id)

    def _dispatch(self, request: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        if request.get('protocol') != PROTOCOL_VERSION:
            raise StorageError(
                'database_protocol_error', 'Storage protocol version mismatch')
        if not request_id or len(request_id) > 64:
            raise StorageError(
                'database_protocol_error', 'Invalid storage request id')
        supplied = request.get('auth')
        if not isinstance(supplied, str) or not hmac.compare_digest(
                supplied, self.server.token):
            raise StorageError(
                'database_protocol_error', 'Storage authentication failed')
        deadline_ms = request.get('deadline_unix_ms')
        if not isinstance(deadline_ms, int):
            raise StorageError(
                'database_protocol_error', 'Storage deadline is required')
        remaining = (deadline_ms / 1000) - time.time()
        if remaining <= 0:
            raise StorageError(
                'database_timeout', 'Storage request deadline expired', True, 25)
        # A malformed client may not reserve a writer or connection forever.
        deadline_at = time.monotonic() + min(remaining, 3600.0)
        kind = str(request.get('kind') or '')
        operation = validate_operation(request.get('operation'))
        if self.server.read_only_preview and kind == 'command':
            raise StorageError(
                'database_unavailable',
                'Distributed preview storage is read-only',
                True,
                1000,
            )

        if kind == 'health' and operation == 'system.health':
            result = self.server.backend.health()
            if self.server.logical_outbox is not None:
                result['logical_outbox'] = self.server.logical_outbox.status()
                if not self.server.logical_outbox.health_ready():
                    result['ready'] = False
        elif kind == 'metrics' and operation == 'system.metrics':
            result = self.server.backend.metrics()
            if self.server.logical_outbox is not None:
                result['logical_outbox'] = self.server.logical_outbox.status()
            result['rpc'] = self.server.rpc_metrics()
            result['process'] = self.server.process_metrics()
            from lib.storage_sidecar.operations_pkg._turns import (
                attempt_event_write_metrics,
            )
            result['attempt_events'] = attempt_event_write_metrics()
            from lib.storage_sidecar.faults import status as fault_status
            faults = fault_status()
            if faults['enabled']:
                result['fault_injection'] = faults
        elif kind == 'maintenance':
            if operation == 'system.integrity_check':
                result = self.server.backend.integrity_check(deadline_at)
            elif operation == 'system.preflight':
                result = self.server.backend.health().get('preflight', {})
            elif operation == 'system.backup':
                result = self.server.backend.backup(deadline_at)
            elif operation == 'system.baseline':
                result = self.server.backend.baseline(deadline_at)
            else:
                # Backend-native maintenance above has no semantic Session
                # callback. Bounded transactional housekeeping is registered
                # in the ordinary operation catalog, so kind validation has
                # one source of truth instead of a second hard-coded allowlist.
                if self.server.read_only_preview:
                    raise StorageError(
                        'database_unavailable',
                        'Distributed preview storage is read-only',
                        True,
                        1000,
                    )
                payload = request.get('payload')
                if not isinstance(payload, Mapping):
                    raise StorageError(
                        'database_protocol_error',
                        'Storage payload must be an object')
                receipt_required, callback = resolve_operation(
                    operation, kind, payload)
                if receipt_required:
                    raise StorageError(
                        'database_protocol_error',
                        'Maintenance operations cannot require command receipts')
                digest = hashlib.sha256(canonical_json({
                    'operation': operation, 'payload': payload,
                })).hexdigest()
                result = self.server.backend.command(
                    operation,
                    digest,
                    None,
                    'maintenance',
                    callback,
                    deadline_at,
                    receipt_required=False,
                )
        elif kind in {'query', 'command'}:
            payload = request.get('payload')
            if not isinstance(payload, Mapping):
                raise StorageError(
                    'database_protocol_error', 'Storage payload must be an object')
            receipt_required, callback = resolve_operation(operation, kind, payload)
            if kind == 'query':
                result = self.server.backend.query(
                    operation, callback, deadline_at)
            else:
                digest = hashlib.sha256(canonical_json({
                    'operation': operation, 'payload': payload,
                })).hexdigest()
                priority = request.get('priority') or 'user'
                if priority not in {'user', 'event', 'maintenance'}:
                    raise StorageError(
                        'database_protocol_error', 'Invalid storage priority')
                captured_record_bytes: list[int | None] = [None]
                logical_outbox = self.server.logical_outbox
                if logical_outbox is not None and logical_outbox.capture_enabled:
                    semantic_callback = callback

                    def callback_with_logical_outbox(session):
                        response, record_bytes = logical_outbox.execute_and_capture(
                            session,
                            semantic_callback,
                            operation=operation,
                            request_id=request_id,
                            request_digest=digest,
                            command_id=request.get('command_id'),
                            payload=payload,
                        )
                        captured_record_bytes[0] = record_bytes
                        return response

                    callback = callback_with_logical_outbox
                result = self.server.backend.command(
                    operation,
                    digest,
                    request.get('command_id'),
                    priority,
                    callback,
                    deadline_at,
                    receipt_required=receipt_required,
                )
                if logical_outbox is not None:
                    logical_outbox.notify(captured_record_bytes[0])
        else:
            raise StorageError(
                'database_protocol_error', 'Invalid storage request kind')
        return {
            'protocol': PROTOCOL_VERSION,
            'request_id': request_id,
            'ok': True,
            'result': result,
        }


def create_server(
    backend, token: str, *, rpc_capacity: int = 8,
    read_only_preview: bool = False, logical_outbox=None,
    idle_trim_rss_bytes: int = 0,
    idle_trim_cooldown_s: float = 300.0,
) -> _StorageTCPServer:
    return _StorageTCPServer(
        ('127.0.0.1', 0), _StorageHandler, backend=backend, token=token,
        rpc_capacity=rpc_capacity, read_only_preview=read_only_preview,
        logical_outbox=logical_outbox,
        idle_trim_rss_bytes=idle_trim_rss_bytes,
        idle_trim_cooldown_s=idle_trim_cooldown_s,
    )


__all__ = ['create_server']

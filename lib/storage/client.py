"""Synchronous semantic RPC client used by repositories and services."""

from __future__ import annotations

import random
import socket

from tofu_db._loopback import create_connection
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage.frame_admission import FrameByteAdmission
from lib.storage.commit_events import publish_committed_events
from lib.storage.protocol import (
    PROTOCOL_VERSION, recv_frame, send_frame, validate_operation,
)
from lib.storage.retry_log import retry_log_throttle

logger = get_logger(__name__)


# Transient classified failures that are safe to replay on a fresh socket.
# Reads are replay-safe. A command remains fail-fast unless the peer explicitly
# proves it was rejected before dispatch; ambiguous replay could double-apply.
_RETRYABLE_CODES = {'database_timeout', 'database_busy', 'database_unavailable'}

# The socket outlives the server-side execution deadline by this grace so a
# classified server error (read-pool acquisition timeout, interrupted query)
# reaches the client instead of losing the race to an opaque local timeout.
_SOCKET_GRACE_S = 1.0
_PROCESS_RESPONSE_ADMISSION_LOCK = threading.Lock()
_PROCESS_RESPONSE_ADMISSION: FrameByteAdmission | None = None


def _process_response_frame_admission() -> FrameByteAdmission:
    """Return the one serialized-response budget for this client process.

    The singleton state stays here (tests reset it through this module);
    ``lib/storage/frame_admission.process_response_frame_admission`` is the
    shared accessor the storage.v2 client uses for the same object, so both
    protocols charge one process-wide budget.
    """
    global _PROCESS_RESPONSE_ADMISSION
    with _PROCESS_RESPONSE_ADMISSION_LOCK:
        if _PROCESS_RESPONSE_ADMISSION is None:
            from runtime_guards import resolve_resource_budget

            capacity_mib = resolve_resource_budget(
                'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB',
                minimum=128,
                maximum=8192,
            )
            _PROCESS_RESPONSE_ADMISSION = FrameByteAdmission(
                capacity_bytes=capacity_mib * 1024 * 1024)
        return _PROCESS_RESPONSE_ADMISSION


def _record_rpc(kind: str, operation: str, started_at: float,
                outcome: str) -> None:
    """Best-effort latency record; metrics must never break the RPC path."""
    try:
        from lib.observability import record_storage_rpc
        record_storage_rpc(
            kind, operation, time.monotonic() - started_at, outcome)
    except Exception as e:
        # Metrics must never break the RPC path; trace and continue.
        logger.debug('[StorageClient] rpc metrics record dropped: %s', e)


class StorageClient:
    """Small, thread-safe-by-construction client (one socket per RPC)."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        timeout: float = 5.0,
        read_attempts: int = 3,
        response_frame_admission: FrameByteAdmission | None = None,
    ):
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('storage.v1 is loopback-only')
        if not token or len(token) < 32:
            raise ValueError('storage token is missing or too short')
        self._host = host
        self._port = int(port)
        self._token = token
        self._timeout = max(0.05, float(timeout))
        self._read_attempts = max(1, int(read_attempts))
        self._response_frame_admission = (
            response_frame_admission
            if response_frame_admission is not None
            else _process_response_frame_admission()
        )
        self._transport_metrics_lock = threading.Lock()
        self._pre_dispatch_command_retries = 0
        self._pre_dispatch_command_retry_exhaustions = 0
        from lib.storage.checkpoint_control import CheckpointClientPort
        self._checkpoint_port = CheckpointClientPort(timeout=self._timeout,
            request_once=self._checkpoint_request, check_scope=lambda owner: None, is_closed=lambda: False)

    def _checkpoint_request(self, operation, payload, owner, deadline_unix_ms):
        from tofu_db.generated_checkpoint_control import CAPABILITIES_OPERATION
        remaining = min(self._timeout, deadline_unix_ms/1000 - time.time())
        if remaining <= 0:
            raise StorageError('database_timeout', 'checkpoint deadline elapsed before effects')
        return self._attempt('query' if operation == CAPABILITIES_OPERATION else 'command',
            operation, {'owner_user_id': owner, **payload}, timeout=remaining, command_id=None,
            priority='user', deadline_unix_ms=deadline_unix_ms)

    def native_checkpoint_available(self, *, user_id: int) -> bool:
        return self._checkpoint_port.available(user_id=user_id)

    def native_checkpoint_control(self, operation: str, payload: dict[str, Any], *,
                                  user_id: int, deadline_unix_ms: int, response_bytes: int) -> dict[str, Any]:
        return self._checkpoint_port.control(operation, payload, user_id=user_id,
            deadline_unix_ms=deadline_unix_ms, response_bytes=response_bytes)

    def close(self):
        self._checkpoint_port.close()

    @property
    def request_timeout_seconds(self) -> float:
        """Configured per-request I/O policy, without opening a connection."""
        return self._timeout

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

    def transport_metrics(self) -> dict[str, int]:
        """Return local retry counters without another storage RPC."""
        frame_metrics = self._response_frame_admission.metrics()
        with self._transport_metrics_lock:
            return {
                'pre_dispatch_command_retries': (
                    self._pre_dispatch_command_retries),
                'pre_dispatch_command_retry_exhaustions': (
                    self._pre_dispatch_command_retry_exhaustions),
                'response_frame_bytes_inflight': frame_metrics[
                    'frame_bytes_inflight'],
                'response_frame_bytes_capacity': frame_metrics[
                    'frame_bytes_capacity'],
                'response_frame_bytes_peak': frame_metrics[
                    'frame_bytes_peak'],
                'response_frame_buffer_memory_bytes': frame_metrics.get('frame_buffer_memory_bytes', 0),
                'response_frame_buffer_file_bytes': frame_metrics.get('frame_buffer_file_bytes', 0),
                'response_frame_buffer_spill_bytes_total': frame_metrics.get('frame_buffer_spill_bytes_total', 0),
                'response_frame_admission_waiting': frame_metrics[
                    'frame_admission_waiting'],
                'response_frame_admission_waits': frame_metrics[
                    'frame_admission_waits'],
                'response_frame_admission_rejections': frame_metrics[
                    'frame_admission_rejections'],
                'response_frame_bytes_admitted_total': frame_metrics[
                    'frame_bytes_admitted_total'],
                'response_frame_bytes_observed_total': frame_metrics[
                    'response_frame_bytes_total'],
                'response_frame_bytes_observed_max': frame_metrics[
                    'response_frame_bytes_max'],
            }

    def _call(
        self,
        kind: str,
        operation: str,
        payload: Mapping[str, Any] | None,
        *,
        deadline: float | None,
        command_id: str | None = None,
        priority: str = 'user',
    ) -> Any:
        operation = validate_operation(operation)
        timeout = self._timeout if deadline is None else max(0.001, float(deadline))
        # Reads are idempotent and the transport opens one socket per RPC, so
        # a transient stall must be retried in place: the error taxonomy
        # already marks these failures retryable (retry_after_ms included),
        # and nothing else honored that contract — a multi-second network-FS
        # stall (page-reclaim thrash under cgroup pressure wedges sidecar
        # reads inside uninterruptible syscalls) surfaced as a user-visible
        # stream/HTTP failure instead of a retried read.
        read_replay_safe = kind in {'query', 'health', 'metrics'}
        # Commands enter the wider loop only so a later classified response
        # can prove that dispatch never started. Every ambiguous command fault
        # still raises from its first attempt.
        attempts = (
            self._read_attempts
            if read_replay_safe or kind == 'command'
            else 1
        )
        stop_at = time.monotonic() + (timeout + _SOCKET_GRACE_S) * attempts
        started_at = time.monotonic()
        for attempt in range(attempts):
            try:
                result = self._attempt(
                    kind, operation, payload, timeout=timeout,
                    command_id=command_id, priority=priority,
                )
                _record_rpc(kind, operation, started_at, 'success')
                return result
            except StorageError as exc:
                retry_classified = (
                    exc.retryable and exc.code in _RETRYABLE_CODES)
                command_replay_safe = (
                    kind == 'command' and exc.request_not_dispatched)
                replay_safe = read_replay_safe or command_replay_safe
                exhausted = attempt + 1 >= attempts
                if not retry_classified or not replay_safe or exhausted:
                    if command_replay_safe and retry_classified and exhausted:
                        with self._transport_metrics_lock:
                            self._pre_dispatch_command_retry_exhaustions += 1
                    _record_rpc(kind, operation, started_at, 'error')
                    raise
                remaining = stop_at - time.monotonic()
                if remaining <= 0.05:
                    if command_replay_safe:
                        with self._transport_metrics_lock:
                            self._pre_dispatch_command_retry_exhaustions += 1
                    _record_rpc(kind, operation, started_at, 'error')
                    raise
                if command_replay_safe:
                    with self._transport_metrics_lock:
                        self._pre_dispatch_command_retries += 1
                delay = min((exc.retry_after_ms or 50) / 1000.0, remaining)
                if retry_log_throttle().allow(f'v1:{kind}:{operation}'):
                    logger.warning(
                        '[StorageClient] retrying %s %s (attempt %d/%d) in '
                        '%.2fs after %s: %s',
                        kind, operation, attempt + 2, attempts, delay,
                        exc.code, str(exc)[:160])
                time.sleep(delay + random.uniform(0.0, min(0.025, delay)))
        _record_rpc(kind, operation, started_at, 'error')
        raise StorageError(  # unreachable: loop either returns or raises
            'database_internal', 'Storage retry loop exited unexpectedly')

    def _attempt(
        self,
        kind: str,
        operation: str,
        payload: Mapping[str, Any] | None,
        *,
        timeout: float,
        command_id: str | None,
        priority: str,
        deadline_unix_ms: int | None = None,
    ) -> Any:
        request_id = uuid.uuid4().hex
        response_reservation = None

        def admit_response_frame(size: int):
            nonlocal response_reservation
            response_reservation = self._response_frame_admission.reserve_frame(size)
            self._response_frame_admission.observe_frame('response', size)
            return response_reservation

        request: dict[str, Any] = {
            'protocol': PROTOCOL_VERSION,
            'request_id': request_id,
            'auth': self._token,
            'kind': kind,
            'operation': operation,
            'payload': dict(payload or {}),
            'deadline_unix_ms': (int((time.time() + timeout) * 1000)
                                 if deadline_unix_ms is None else deadline_unix_ms),
        }
        if kind == 'command':
            request['command_id'] = command_id
            request['priority'] = priority
        try:
            with create_connection(self.endpoint, timeout=timeout) as sock:
                sock.settimeout(timeout + _SOCKET_GRACE_S)
                send_frame(sock, request)
                try:
                    response = recv_frame(
                        sock, before_payload=admit_response_frame)
                finally:
                    if response_reservation is not None:
                        response_reservation.close()
        except StorageError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise StorageError(
                'database_timeout', 'Storage request timed out', True, 50,
                request_id,
            ) from exc
        except OSError as exc:
            raise StorageError(
                'database_unavailable', 'Storage sidecar is unavailable', True,
                100, request_id,
            ) from exc
        if response.get('protocol') != PROTOCOL_VERSION:
            raise StorageError(
                'database_protocol_error', 'Storage protocol version mismatch',
                operation_id=request_id,
            )
        if response.get('request_id') != request_id:
            # A well-formed server-classified ERROR envelope stays actionable
            # without a matching correlation id: the capacity-rejection path
            # answers before it may read the request frame, so it cannot echo
            # the id.  Raise the carried (retryable) error.  The desync
            # detector keeps its full strength for result-carrying (ok=True)
            # responses, where misattribution would corrupt caller state.
            error = response.get('error')
            if response.get('ok') is not False or not isinstance(error, Mapping):
                raise StorageError(
                    'database_protocol_error', 'Storage response correlation failed',
                    operation_id=request_id,
                )
            classified = StorageError.from_payload(error)
            classified.operation_id = classified.operation_id or request_id
            raise classified
        if not response.get('ok'):
            error = response.get('error')
            if not isinstance(error, Mapping):
                raise StorageError(
                    'database_protocol_error', 'Malformed storage error response',
                    operation_id=request_id,
                )
            raise StorageError.from_payload(error)
        return response.get('result')

    def query(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> Any:
        return self._call('query', operation, payload, deadline=deadline)

    def command(
        self,
        operation: str,
        payload: Mapping[str, Any] | None,
        command_id: str | None,
        priority: str = 'user',
        deadline: float | None = None,
    ) -> Any:
        result = self._call(
            'command', operation, payload, deadline=deadline,
            command_id=command_id, priority=priority,
        )
        if not isinstance(result, Mapping) or '_storageCommitContract' not in result:
            return result
        if result.get('_storageCommitContract') != 'storage.committed-events/v1':
            raise StorageError(
                'database_protocol_error',
                'Storage committed-event envelope version mismatch',
            )
        events = result.get('events')
        if not isinstance(events, list) or not all(
            isinstance(event, Mapping) for event in events
        ):
            raise StorageError(
                'database_protocol_error',
                'Malformed storage committed-event envelope',
            )
        # The sidecar sends this envelope only after its transaction has
        # committed.  Wake subscribers before returning the original semantic
        # value, while keeping observer failures non-fatal in the dispatcher.
        publish_committed_events(events)
        return result.get('value')

    def health(self, deadline: float | None = 2.0) -> Any:
        return self._call('health', 'system.health', {}, deadline=deadline)

    def metrics(self, deadline: float | None = 2.0) -> Any:
        return self._call('metrics', 'system.metrics', {}, deadline=deadline)

    def maintenance(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        deadline: float | None = 30.0,
    ) -> Any:
        return self._call('maintenance', operation, payload, deadline=deadline)


__all__ = ['StorageClient']

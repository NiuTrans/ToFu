"""Synchronous semantic RPC client used by repositories and services."""

from __future__ import annotations

import random
import socket
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage.commit_events import publish_committed_events
from lib.storage.protocol import (
    PROTOCOL_VERSION, recv_frame, send_frame, validate_operation,
)


# Transient classified failures that are safe to replay on a fresh socket.
# Only reads are auto-retried: a ``command`` replay without a server-side
# receipt could double-apply a mutation, so commands keep fail-fast semantics.
_RETRYABLE_CODES = {'database_timeout', 'database_busy', 'database_unavailable'}

# The socket outlives the server-side execution deadline by this grace so a
# classified server error (read-pool acquisition timeout, interrupted query)
# reaches the client instead of losing the race to an opaque local timeout.
_SOCKET_GRACE_S = 1.0


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

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

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
        attempts = self._read_attempts if kind in {'query', 'health', 'metrics'} else 1
        stop_at = time.monotonic() + (timeout + _SOCKET_GRACE_S) * attempts
        for attempt in range(attempts):
            try:
                return self._attempt(
                    kind, operation, payload, timeout=timeout,
                    command_id=command_id, priority=priority,
                )
            except StorageError as exc:
                if (not exc.retryable or exc.code not in _RETRYABLE_CODES
                        or attempt + 1 >= attempts):
                    raise
                remaining = stop_at - time.monotonic()
                if remaining <= 0.05:
                    raise
                delay = min((exc.retry_after_ms or 50) / 1000.0, remaining)
                time.sleep(delay + random.uniform(0.0, min(0.025, delay)))
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
    ) -> Any:
        request_id = uuid.uuid4().hex
        request: dict[str, Any] = {
            'protocol': PROTOCOL_VERSION,
            'request_id': request_id,
            'auth': self._token,
            'kind': kind,
            'operation': operation,
            'payload': dict(payload or {}),
            'deadline_unix_ms': int((time.time() + timeout) * 1000),
        }
        if kind == 'command':
            request['command_id'] = command_id
            request['priority'] = priority
        try:
            with socket.create_connection(self.endpoint, timeout=timeout) as sock:
                sock.settimeout(timeout + _SOCKET_GRACE_S)
                send_frame(sock, request)
                response = recv_frame(sock)
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

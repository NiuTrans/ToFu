"""Synchronous semantic RPC client used by repositories and services."""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage.protocol import (
    PROTOCOL_VERSION, recv_frame, send_frame, validate_operation,
)


class StorageClient:
    """Small, thread-safe-by-construction client (one socket per RPC)."""

    def __init__(self, host: str, port: int, token: str, *, timeout: float = 5.0):
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('storage.v1 is loopback-only')
        if not token or len(token) < 32:
            raise ValueError('storage token is missing or too short')
        self._host = host
        self._port = int(port)
        self._token = token
        self._timeout = max(0.05, float(timeout))

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
                sock.settimeout(timeout)
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
            raise StorageError(
                'database_protocol_error', 'Storage response correlation failed',
                operation_id=request_id,
            )
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
        return self._call(
            'command', operation, payload, deadline=deadline,
            command_id=command_id, priority=priority,
        )

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

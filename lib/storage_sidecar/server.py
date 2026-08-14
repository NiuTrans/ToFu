"""Authenticated loopback RPC server for ``storage.v1``."""

from __future__ import annotations

import hashlib
import hmac
import socket
import socketserver
import threading
import time
from typing import Any, Mapping

import psutil

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage.protocol import (
    PROTOCOL_VERSION, canonical_json, recv_frame, send_frame, validate_operation,
)
from lib.storage_sidecar.operations import resolve_operation


logger = get_logger('tofu.storage.sidecar')


class _StorageTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 256
    rpc_capacity = 256

    def __init__(self, address, handler, *, backend, token: str):
        self._rpc_slots = threading.BoundedSemaphore(self.rpc_capacity)
        self._rpc_metrics_lock = threading.Lock()
        self._rpc_active = 0
        self._rpc_rejected = 0
        super().__init__(address, handler, bind_and_activate=True)
        self.backend = backend
        self.token = token

    def process_request(self, request, client_address) -> None:
        if not self._rpc_slots.acquire(blocking=False):
            with self._rpc_metrics_lock:
                self._rpc_rejected += 1
            self.shutdown_request(request)
            return
        with self._rpc_metrics_lock:
            self._rpc_active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_rpc_slot()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_rpc_slot()

    def _release_rpc_slot(self) -> None:
        with self._rpc_metrics_lock:
            self._rpc_active -= 1
        self._rpc_slots.release()

    def rpc_metrics(self) -> dict[str, int]:
        with self._rpc_metrics_lock:
            return {
                'active': self._rpc_active,
                'capacity': self.rpc_capacity,
                'rejected': self._rpc_rejected,
            }

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
        # Authentication is inside the first frame.  Bound unauthenticated
        # half-open clients so they cannot retain a worker/FD indefinitely.
        self.request.settimeout(5.0)
        try:
            request = recv_frame(self.request)
            request_id = str(request.get('request_id') or '')
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
        try:
            send_frame(self.request, response)
        except (OSError, StorageError):
            logger.debug('storage response channel closed operation_id=%s', request_id)

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

        if kind == 'health' and operation == 'system.health':
            result = self.server.backend.health()
        elif kind == 'metrics' and operation == 'system.metrics':
            result = self.server.backend.metrics()
            result['rpc'] = self.server.rpc_metrics()
            result['process'] = self.server.process_metrics()
        elif kind == 'maintenance':
            if operation == 'system.integrity_check':
                result = self.server.backend.integrity_check(deadline_at)
            elif operation == 'system.preflight':
                result = self.server.backend.health().get('preflight', {})
            elif operation == 'system.backup':
                result = self.server.backend.backup(deadline_at)
            else:
                raise StorageError(
                    'database_protocol_error', 'Unknown maintenance operation')
        elif kind in {'query', 'command'}:
            payload = request.get('payload')
            if not isinstance(payload, Mapping):
                raise StorageError(
                    'database_protocol_error', 'Storage payload must be an object')
            receipt_required, callback = resolve_operation(operation, kind, payload)
            if kind == 'query':
                result = self.server.backend.query(callback, deadline_at)
            else:
                digest = hashlib.sha256(canonical_json({
                    'operation': operation, 'payload': payload,
                })).hexdigest()
                priority = request.get('priority') or 'user'
                if priority not in {'user', 'event', 'maintenance'}:
                    raise StorageError(
                        'database_protocol_error', 'Invalid storage priority')
                result = self.server.backend.command(
                    operation,
                    digest,
                    request.get('command_id'),
                    priority,
                    callback,
                    deadline_at,
                    receipt_required=receipt_required,
                )
        else:
            raise StorageError(
                'database_protocol_error', 'Invalid storage request kind')
        return {
            'protocol': PROTOCOL_VERSION,
            'request_id': request_id,
            'ok': True,
            'result': result,
        }


def create_server(backend, token: str) -> _StorageTCPServer:
    return _StorageTCPServer(
        ('127.0.0.1', 0), _StorageHandler, backend=backend, token=token)


__all__ = ['create_server']

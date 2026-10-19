"""Shared instance-bound checkpoint negotiation over the selected transport.

The transport supplies one attempt and its scope/close checks. This owner keeps
no database, native library or retry loop; stale bindings never auto-refresh.
"""
import sys
import threading
import time
import uuid

from lib.storage.errors import StorageError
from lib.storage.protocol import validate_finite_json_numbers


class CheckpointClientPort:
    def __init__(self, *, timeout, request_once, check_scope, is_closed):
        self.timeout = timeout
        self.request_once = request_once
        self.check_scope = check_scope
        self.is_closed = is_closed
        self._lock = threading.Lock()
        self._bindings = {}
        self._unavailable_binding = None
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def _open(self, owner):
        if type(owner) is not int or not 0 < owner < 1 << 64:
            raise StorageError('database_forbidden', 'native checkpoint owner differs')
        self.check_scope(owner)
        if self._closed.is_set() or self.is_closed():
            raise StorageError('database_unavailable', 'native checkpoint client closed')

    def binding(self, owner, deadline_unix_ms):
        from tofu_db.generated_checkpoint_control import CAPABILITIES_OPERATION, TRANSPORT_FORMAT
        self._open(owner)
        remaining = min(self.timeout, deadline_unix_ms/1000 - time.time())
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise StorageError('database_timeout', 'checkpoint binding deadline elapsed')
        try:
            self._open(owner)
            if self._unavailable_binding is not None:
                return self._unavailable_binding
            if owner not in self._bindings:
                if deadline_unix_ms/1000 <= time.time():
                    raise StorageError('database_timeout', 'checkpoint binding deadline elapsed')
                result = self.request_once(CAPABILITIES_OPERATION, {}, owner, deadline_unix_ms)
                if (not isinstance(result, dict) or set(result) != {
                        'format', 'available', 'runtime_id', 'maximum_response_bytes'}
                        or result['format'] != TRANSPORT_FORMAT or type(result['available']) is not bool
                        or type(result['maximum_response_bytes']) is not int or result['maximum_response_bytes'] <= 0
                        or not isinstance(result['runtime_id'], str)):
                    raise StorageError('database_protocol_error', 'invalid checkpoint capability response')
                try:
                    if str(uuid.UUID(result['runtime_id'])) != result['runtime_id']:
                        raise ValueError('noncanonical runtime identity')
                except ValueError as error:
                    raise StorageError('database_protocol_error', 'invalid checkpoint runtime identity') from error
                self._open(owner)
                binding = (result['runtime_id'], result['maximum_response_bytes'], result['available'])
                if not result['available']:
                    self._unavailable_binding = binding
                    return binding
                self._bindings[owner] = binding
            return self._bindings[owner]
        finally:
            self._lock.release()

    def available(self, *, user_id):
        return self.binding(user_id, int((time.time()+self.timeout)*1000))[2]

    def control(self, operation, payload, *, user_id, deadline_unix_ms, response_bytes):
        from tofu_db.generated_checkpoint_control import CONTROL_OPERATION, TRANSPORT_FORMAT, FORMAT, OPERATIONS
        if (not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(payload, dict)
                or type(response_bytes) is not int or not 0 < response_bytes <= sys.maxsize
                or type(deadline_unix_ms) is not int or not 0 < deadline_unix_ms < 1 << 64):
            raise StorageError('database_protocol_error', 'invalid checkpoint control admission')
        validate_finite_json_numbers(payload)
        runtime_id, _maximum_response, available = self.binding(user_id, deadline_unix_ms)
        if not available:
            raise StorageError('database_unsupported', 'selected authority lacks durable native checkpoint control')
        if deadline_unix_ms/1000 <= time.time():
            raise StorageError('database_timeout', 'checkpoint deadline elapsed before effects')
        self._open(user_id)
        result = self.request_once(CONTROL_OPERATION, dict(format=TRANSPORT_FORMAT,
            runtime_id=runtime_id, operation=operation, payload=payload, response_bytes=response_bytes),
            user_id, deadline_unix_ms)
        self._open(user_id)
        if (not isinstance(result, dict) or result.get('format') != FORMAT or type(result.get('accepted')) is not bool):
            raise StorageError('database_protocol_error', 'invalid checkpoint control response')
        return result

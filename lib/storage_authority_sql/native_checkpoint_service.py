"""Selected SQL Sidecar ownership of one explicit native checkpoint scope.

The service lazily loads its pinned code and binds controls to this process
instance. Close withdraws admission, joins initialization and delegates the
entered-call barrier to the controller before the backend may close.
"""
import threading
import time
import uuid

from lib.storage.errors import StorageError
from lib.storage.protocol import MAX_FRAME_BYTES, validate_finite_json_numbers
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC


class NativeCheckpointService:
    def __init__(self, backend, configuration=None, *, logical_outbox=None, read_only=False):
        self.backend = backend
        self.configuration = configuration
        self.logical_outbox = logical_outbox
        self.read_only = read_only
        self.runtime_id = str(uuid.uuid4())
        self._condition = threading.Condition()
        self._closed = False
        self._loading = False
        self._controller = None
        self._failure = None

    def _owner(self, payload):
        owner = payload.get(SPEC['application']['transport_owner_field']) if isinstance(payload, dict) else None
        if (type(owner) is not int or not 0 < owner < 1 << 64
                or (self.configuration is not None and owner != self.configuration.owner_user_id)):
            raise StorageError('database_forbidden', 'native checkpoint owner differs')
        return owner

    def _open(self):
        if self._closed:
            raise StorageError('database_unavailable', 'native checkpoint service closed')

    def _get(self, deadline_at):
        with self._condition:
            self._open()
            while self._loading:
                remaining = deadline_at-time.monotonic()
                if remaining <= 0:
                    raise StorageError('database_timeout', 'native checkpoint initialization deadline elapsed')
                self._condition.wait(remaining)
                self._open()
            if deadline_at <= time.monotonic():
                raise StorageError('database_timeout', 'native checkpoint initialization deadline elapsed')
            if self._controller is not None:
                return self._controller
            if self._failure is not None:
                raise StorageError('database_unavailable', 'native checkpoint image admission failed') from self._failure
            if self.configuration is None or self.read_only:
                return None
            self._loading = True
        controller, failure = None, None
        try:
            from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpoint
            config = self.configuration
            controller = NativeSqlCheckpoint(backend=self.backend, tenant_id=config.tenant_id,
                owner_user_id=config.owner_user_id, document_bytes=config.document_bytes,
                identity_bytes=config.identity_bytes, library=config.library_path, sha256=config.sha256,
                binary_bytes=config.binary_bytes, logical_outbox=self.logical_outbox)
        except BaseException as error:
            failure = error
        finally:
            with self._condition:
                self._controller, self._failure = controller, failure
                self._loading = False
                self._condition.notify_all()
                self._open()
        if failure is not None:
            if not isinstance(failure, Exception):
                raise failure
            raise StorageError('database_unavailable', 'native checkpoint image admission failed') from failure
        if deadline_at <= time.monotonic():
            raise StorageError('database_timeout', 'native checkpoint initialization deadline elapsed')
        return controller

    def dispatch(self, kind, operation, payload, deadline_at):
        from tofu_db.generated_checkpoint_control import CAPABILITIES_OPERATION, CONTROL_OPERATION, TRANSPORT_FORMAT, OPERATIONS
        self._owner(payload)
        owner_field = SPEC['application']['transport_owner_field']
        if operation == CAPABILITIES_OPERATION:
            if kind != 'query' or set(payload) != {owner_field}:
                raise StorageError('database_protocol_error', 'invalid checkpoint capability admission')
            controller = self._get(deadline_at)
            maximum = MAX_FRAME_BYTES - SPEC['envelope_bytes']
            return dict(format=TRANSPORT_FORMAT, available=controller is not None, runtime_id=self.runtime_id,
                        maximum_response_bytes=min(maximum, controller._maximum) if controller else maximum)
        if (operation != CONTROL_OPERATION or kind != 'command'
                or set(payload) != {owner_field, 'format', 'runtime_id', 'operation', 'payload', 'response_bytes'}
                or payload['format'] != TRANSPORT_FORMAT or payload['runtime_id'] != self.runtime_id
                or not isinstance(payload['operation'], str) or payload['operation'] not in OPERATIONS or not isinstance(payload['payload'], dict)
                or type(payload['response_bytes']) is not int
                or not 0 < payload['response_bytes'] <= MAX_FRAME_BYTES-SPEC['envelope_bytes']):
            raise StorageError('database_protocol_error', 'invalid or stale checkpoint control admission')
        validate_finite_json_numbers(payload['payload'])
        controller = self._get(deadline_at)
        if controller is None:
            raise StorageError('database_unsupported', 'selected authority lacks durable native checkpoint control')
        from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpointError
        try:
            return controller.control(payload['operation'], payload['payload'], deadline_at=deadline_at,
                                      response_bytes=payload['response_bytes'])
        except NativeSqlCheckpointError as error:
            raise StorageError('database_protocol_error', 'native checkpoint invocation refused') from error

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._loading:
                self._condition.wait()
            controller = self._controller
        if controller is not None:
            controller.close()

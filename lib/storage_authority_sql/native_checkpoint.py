"""Experimental Rust checkpoint controller over the selected SQL backend.

The composition owner supplies authenticated scope, capacity and pinned code.
This adapter owns only synchronous FFI, SQL bytes and joined close. The lazy
application service owns availability and instance binding; no store fallback.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import orjson

from lib.native_binary import NativeConfigurationError, _seal
from lib.storage_authority_sql.generated_checkpoint_storage_cffi import ffi
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
from lib.storage_authority_sql.native_checkpoint_rows import NativeCheckpointRows


class NativeSqlCheckpointError(RuntimeError):
    """Content-free ABI/lifetime failure; ambiguous delivery is never retried."""


class NativeSqlCheckpoint:
    def __init__(self, *, backend, tenant_id, owner_user_id, document_bytes, identity_bytes,
                 library: Path, sha256: str, binary_bytes: int, logical_outbox=None):
        if (type(tenant_id) is not int or not 0 <= tenant_id < 1 << 64
                or type(owner_user_id) is not int or not 0 < owner_user_id < 1 << 64
                or type(document_bytes) is not int or not 0 < document_bytes < (1 << 60)
                or type(identity_bytes) is not int or not 0 < identity_bytes <= document_bytes
                or not isinstance(library, Path) or not library.is_absolute()
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(ch not in '0123456789abcdef' for ch in sha256)
                or type(binary_bytes) is not int or binary_bytes < 4):
            raise NativeConfigurationError('native SQL checkpoint requires explicit scope, bounds and pinned code')
        if (backend.name not in ('sqlite', 'postgres') or backend.config.distributed_preview_read_only
                or (logical_outbox is None and backend.config.logical_shadow_mode != 'off')
                or (logical_outbox is not None and (logical_outbox.backend is not backend
                    or (backend.config.logical_shadow_mode != 'off'
                        and logical_outbox.policy.mode != backend.config.logical_shadow_mode)
                    or (logical_outbox.policy.capture_enabled and not logical_outbox.capture_enabled)))):
            raise NativeConfigurationError('native SQL checkpoint authority configuration is unsupported')
        self._rows = NativeCheckpointRows(backend, tenant_id=tenant_id, owner_user_id=owner_user_id,
            document_bytes=document_bytes, identity_bytes=identity_bytes, logical_outbox=logical_outbox)
        self._rows.validate_handoff = self._validate_handoff
        self._rows.goal_transaction = self._goal_transaction
        self._condition = threading.Condition()
        self._active = 0
        self._state = 'open'
        self._invocation = threading.local()
        self._maximum = (document_bytes * SPEC['request_document_slots']
                         + identity_bytes * SPEC['request_identity_escape_slots'] + SPEC['envelope_bytes'])
        self._image = _seal(library, sha256, binary_bytes, ffi=ffi)
        self._library = None
        self._handle = 0
        self._callback = None
        try:
            self._callback = ffi.callback('tofu_checkpoint_storage_callback', self._dispatch, error=SPEC['statuses']['internal'])
            self._library = ffi.dlopen(f'/proc/self/fd/{self._image.fileno()}')
            for symbol in SPEC['signatures']:
                getattr(self._library, symbol)
            if self._library.tofu_checkpoint_storage_abi_version() != SPEC['abi_version']:
                raise NativeSqlCheckpointError('native SQL checkpoint ABI differs')
            handle = ffi.new('uint64_t *')
            self._check(self._library.tofu_checkpoint_storage_open(tenant_id, owner_user_id,
                document_bytes, identity_bytes, self._callback, ffi.NULL, handle))
            self._handle = int(handle[0])
        except BaseException:
            if self._library is not None:
                ffi.dlclose(self._library)
            self._image.close()
            self._callback = None
            raise

    @staticmethod
    def _check(status):
        if status != SPEC['statuses']['ok']:
            name = next((name for name, number in SPEC['statuses'].items() if number == status), 'unknown')
            raise NativeSqlCheckpointError('native SQL checkpoint refused: ' + name)

    def _dispatch(self, _context, request, length, response, capacity, written):
        try:
            invocation = getattr(self._invocation, 'value', None)
            if invocation is None or length > self._maximum:
                raise NativeSqlCheckpointError('native SQL callback lacks a bounded invocation')
            if time.monotonic() >= invocation['deadline']:
                raise NativeSqlCheckpointError('native SQL callback deadline elapsed')
            value = self._rows.dispatch(orjson.loads(bytes(ffi.buffer(request, length))), invocation['deadline'])
            raw = orjson.dumps(value)
            if len(raw) > capacity:
                raise NativeSqlCheckpointError('native SQL callback response exceeds capacity')
            ffi.buffer(response, len(raw))[:] = raw
            written[0] = len(raw)
            return SPEC['statuses']['ok']
        except BaseException as error:
            # This slot belongs only to the entering call/thread. Preserve the
            # actual storage error without throwing through a foreign stack.
            invocation = getattr(self._invocation, 'value', None)
            if invocation is not None:
                invocation['error'] = error
            return SPEC['statuses']['internal']

    def _validate_handoff(self, request, observations):
        """Runs inside the SQL transaction; the outer control call retains code."""
        raw, actual = orjson.dumps(request), orjson.dumps(observations)
        if len(raw) > self._maximum or len(actual) > self._maximum:
            return 'oversize'
        rows = self._rows
        status = self._library.tofu_checkpoint_storage_validate_handoff(
            rows.tenant, rows.owner, rows.document_bytes, rows.identity_bytes,
            raw, len(raw), actual, len(actual))
        if status == SPEC['statuses']['ok']:
            return None
        for error in ('invalid', 'conflict', 'ineligible', 'oversize'):
            if status == SPEC['statuses'][error]:
                return error
        self._check(status)

    def _goal_transaction(self, request, dispatch, deadline_at):
        """The joined outer call retains code while this exact Session does IO."""
        from lib.storage_authority_sql.native_checkpoint_goal import GoalPolicyRefusal
        raw = orjson.dumps(request)
        if len(raw) > self._maximum:
            raise GoalPolicyRefusal('oversize')
        errors = []
        def callback(_context, pointer, length, response, capacity, written):
            try:
                if length > self._maximum or time.monotonic() >= deadline_at:
                    raise NativeSqlCheckpointError('native Goal transaction callback expired or unbounded')
                value = dispatch(orjson.loads(bytes(ffi.buffer(pointer, length))))
                encoded = orjson.dumps(value)
                if len(encoded) > capacity:
                    raise NativeSqlCheckpointError('native Goal transaction response exceeds capacity')
                ffi.buffer(response, len(encoded))[:] = encoded
                written[0] = len(encoded)
                return SPEC['statuses']['ok']
            except BaseException as error:
                errors.append(error)
                return SPEC['statuses']['internal']
        local_callback = ffi.callback('tofu_checkpoint_storage_callback', callback, error=SPEC['statuses']['internal'])
        rows = self._rows
        response, written = ffi.new('uint8_t[]', rows.document_bytes), ffi.new('size_t *')
        # SQLite runs here on its joined writer thread. Refuse recursive control
        # and close there too, without entering another handle or Backend job.
        own_invocation = getattr(self._invocation, 'value', None) is None
        if own_invocation:
            self._invocation.value = {'deadline': deadline_at, 'error': None}
        try:
            status = self._library.tofu_checkpoint_storage_goal_transaction(
                rows.tenant, rows.owner, rows.document_bytes, rows.identity_bytes, raw, len(raw),
                local_callback, ffi.NULL, response, rows.document_bytes, written)
            if errors:
                raise errors[0]
            for name in ('invalid', 'conflict', 'ineligible', 'oversize'):
                if status == SPEC['statuses'][name]:
                    raise GoalPolicyRefusal(name)
            self._check(status)
            if not 0 < written[0] <= rows.document_bytes:
                raise NativeSqlCheckpointError('native Goal transaction response length invalid')
            return orjson.loads(bytes(ffi.buffer(response, written[0])))
        finally:
            if own_invocation:
                del self._invocation.value

    @contextmanager
    def _enter(self, deadline):
        if getattr(self._invocation, 'value', None) is not None:
            raise NativeSqlCheckpointError('native SQL checkpoint reentrant invocation refused')
        with self._condition:
            if self._state != 'open':
                raise NativeSqlCheckpointError('native SQL checkpoint is closed')
            self._active += 1
        invocation = {'deadline': deadline, 'error': None}
        self._invocation.value = invocation
        try:
            yield invocation
        finally:
            del self._invocation.value
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def control(self, operation, payload, *, deadline_at, response_bytes):
        if (not isinstance(operation, str) or not operation.isascii() or not 0 < len(operation) <= 32
                or type(response_bytes) is not int or not 0 < response_bytes <= self._maximum
                or not isinstance(deadline_at, (float, int)) or not 0 < deadline_at - time.monotonic() <= 3600):
            raise NativeSqlCheckpointError('native SQL checkpoint invocation bounds invalid')
        # json.dumps rejects non-finite numbers instead of turning them into null.
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(raw) > self._maximum:
            raise NativeSqlCheckpointError('native SQL checkpoint request exceeds capacity')
        with self._enter(deadline_at) as invocation:
            output, written = ffi.new('uint8_t[]', response_bytes), ffi.new('size_t *')
            deadline = int(time.time() * 1000 + max(0, deadline_at - time.monotonic()) * 1000)
            name = operation.encode('ascii')
            status = self._library.tofu_checkpoint_storage_control(self._handle, name, len(name), raw, len(raw),
                deadline, output, response_bytes, written)
            if invocation['error'] is not None:
                raise invocation['error']
            self._check(status)
            if not 0 < written[0] <= response_bytes:
                raise NativeSqlCheckpointError('native SQL checkpoint response length invalid')
            return orjson.loads(bytes(ffi.buffer(output, written[0])))

    def close(self):
        if getattr(self._invocation, 'value', None) is not None:
            raise NativeSqlCheckpointError('native SQL checkpoint close from callback refused')
        with self._condition:
            while self._state == 'closing':
                self._condition.wait()
            if self._state == 'closed':
                return
            if self._state == 'failed':
                raise NativeSqlCheckpointError('native SQL checkpoint teardown failed; resources retained')
            self._state = 'closing'
            self._condition.notify_all()
            try:
                while self._active:
                    self._condition.wait()
            except BaseException:
                self._state = 'open'
                self._condition.notify_all()
                raise
        # Keep code and callback alive if native close fails; unloading after an
        # unproven join would be unsafe. The owner receives the failure.
        try:
            self._check(self._library.tofu_checkpoint_storage_close(self._handle))
            ffi.dlclose(self._library)
            self._image.close()
        except BaseException:
            with self._condition:
                self._state = 'failed'
                self._condition.notify_all()
            raise
        self._library = self._callback = self._rows = None
        with self._condition:
            self._state = 'closed'
            self._condition.notify_all()

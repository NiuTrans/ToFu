"""Transactional logical-commit capture and bounded asynchronous publishing.

Responsibility: turn each successful semantic command into a canonical outbox
row inside the command's existing database transaction, then publish that row
to a durable sink without holding the database writer during filesystem I/O.

Entry points are ``LogicalOutboxPipeline.start``, ``capture``, ``notify``, and
``close``.  The pipeline depends only on the backend/session contracts and the
``LogicalCommitSink`` protocol, so SQLite and PostgreSQL share identical
failure semantics and future object-store adapters do not leak into handlers.
The database remains authoritative until the separate replay/cutover gate says
otherwise; a full or unavailable sink therefore accumulates a bounded outbox
and ultimately backpressures commands instead of dropping recovery records.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol, TYPE_CHECKING
import uuid

from lib.log import get_logger
from lib.secret_envelope import (
    BoundPayloadCipher,
    SecretEnvelopeError,
    bound_payload_cipher,
)
from lib.storage.errors import StorageError
from lib.storage_sidecar.operation_domains import REGISTRY_VERSION
from lib.storage_sidecar.adapters.base import (
    Backend,
    Session,
    receipt_cacheable,
)
from lib.storage_sidecar.logical_shadow import (
    RECORD_FORMAT,
    LogicalCommitReceipt,
    LogicalCommitShadow,
    LogicalShadowBusyError,
    LogicalShadowCapacityError,
    LogicalShadowCorruptionError,
)


if TYPE_CHECKING:
    from lib.storage_sidecar.config import SidecarConfig


logger = get_logger('tofu.storage.sidecar.logical_outbox')

_META_STREAM_ID = 'logical_outbox_stream_id'
_META_LAST_SEQUENCE = 'logical_outbox_last_sequence'
_META_PUBLISHED_SEQUENCE = 'logical_outbox_published_sequence'
_META_PENDING_BYTES = 'logical_outbox_pending_bytes'
_META_ENCRYPTION_KEY_ID = 'logical_outbox_encryption_key_id'
_SYSTEM_TENANT_ID = 'system'
_PAYLOAD_CODEC = 'tofu.bound-fernet-json.v1'
_PAYLOAD_PURPOSE = 'logical-commit-payload'
_SINK_STARTUP_TIMEOUT_S = 5.0
_MAX_MUTATIONS_PER_COMMAND = 4096
_MAX_MUTATION_SQL_BYTES = 64 * 1024
_MAX_MUTATION_PARAMETERS = 4096
_MUTATION_PREFIXES = ('delete ', 'insert ', 'replace ', 'update ', 'with ')


class LogicalCommitSink(Protocol):
    """Minimal append-only sink boundary used by the publisher.

    A cloud adapter can implement this protocol with conditional object puts
    and a manifest CAS. The built-in implementation targets private POSIX
    directories, including local block devices and correctly mounted network
    or FUSE filesystems.
    """

    def append(self, **record: Any) -> LogicalCommitReceipt: ...
    def status(self) -> Any: ...
    def close(self) -> None: ...


SinkFactory = Callable[..., LogicalCommitSink]


def _sink_status_document(sink: LogicalCommitSink) -> dict[str, Any]:
    raw = sink.status()
    converter = getattr(raw, 'as_dict', None)
    if callable(converter):
        raw = converter()
    if not isinstance(raw, Mapping):
        raise LogicalShadowCorruptionError(
            'logical sink returned an invalid status document')
    return dict(raw)


@dataclass(frozen=True, slots=True)
class LogicalOutboxPolicy:
    mode: str
    capture_enabled: bool
    publisher_enabled: bool
    sink_root: Path | None
    reason: str
    max_pending_bytes: int
    max_record_bytes: int
    max_segment_bytes: int
    max_shadow_bytes: int
    publish_batch_size: int
    access_mode: str = 'owner'


@dataclass(frozen=True, slots=True)
class LogicalOutboxRecord:
    sequence: int
    event_id: str
    operation: str
    schema_version: int
    registry_version: int
    request_id: str
    request_digest: str
    command_id: str
    tenant_id: str
    owner_user_id: int
    encryption_key_id: str
    payload_ciphertext: str
    record_bytes: int
    committed_at_ms: int


class LogicalMutationRecordingSession:
    """Session facade that records successful backend-neutral mutations."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self.backend = session.backend
        self.mutations: list[dict[str, Any]] = []

    def lock_key(self, namespace: str, key: str) -> None:
        self._session.lock_key(namespace, key)

    def index_exists(self, index_name: str) -> bool:
        return self._session.index_exists(index_name)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if not isinstance(sql, str) or not sql.strip() or (
                len(sql.encode('utf-8')) > _MAX_MUTATION_SQL_BYTES):
            raise StorageError(
                'database_protocol_error',
                'Logical mutation statement exceeds its contract',
            )
        if not sql.lstrip().lower().startswith(_MUTATION_PREFIXES):
            raise StorageError(
                'database_protocol_error',
                'Logical command attempted a non-replayable mutation statement',
            )
        if len(params) > _MAX_MUTATION_PARAMETERS:
            raise StorageError(
                'storage_payload_too_large',
                'Logical mutation parameter count exceeds its contract',
            )
        if len(self.mutations) >= _MAX_MUTATIONS_PER_COMMAND:
            raise StorageError(
                'storage_payload_too_large',
                'Logical mutation count exceeds its contract',
            )
        rowcount = self._session.execute(sql, params)
        self.mutations.append({
            'params': _wire_value(list(params)),
            'rowcount': int(rowcount),
            'sql': sql,
        })
        return rowcount

    def execute_many_exact(
        self, sql: str, params: Sequence[tuple[Any, ...]],
    ) -> int:
        """Record a backend-batched set as equivalent replay statements."""
        rows = tuple(params)
        if not rows:
            return 0
        if not isinstance(sql, str) or not sql.strip() or (
                len(sql.encode('utf-8')) > _MAX_MUTATION_SQL_BYTES):
            raise StorageError(
                'database_protocol_error',
                'Logical mutation statement exceeds its contract',
            )
        if not sql.lstrip().lower().startswith(_MUTATION_PREFIXES):
            raise StorageError(
                'database_protocol_error',
                'Logical command attempted a non-replayable mutation statement',
            )
        if len(self.mutations) + len(rows) > _MAX_MUTATIONS_PER_COMMAND:
            raise StorageError(
                'storage_payload_too_large',
                'Logical mutation count exceeds its contract',
            )
        if any(len(row) > _MAX_MUTATION_PARAMETERS for row in rows):
            raise StorageError(
                'storage_payload_too_large',
                'Logical mutation parameter count exceeds its contract',
            )
        rowcount = self._session.execute_many_exact(sql, rows)
        self.mutations.extend({
            'params': _wire_value(list(row)),
            'rowcount': 1,
            'sql': sql,
        } for row in rows)
        return rowcount

    def fetch_one(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        return self._session.fetch_one(sql, params)

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> list[Mapping[str, Any]]:
        return self._session.fetch_all(sql, params)

    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        return self._session.fetch_one_for_update_skip_locked(sql, params)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise StorageError(
            'database_protocol_error',
            'Logical commit value is not canonical JSON',
        ) from exc


def _wire_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Return one deterministic JSON document without losing binary values."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode('ascii')}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    seen = set() if _seen is None else _seen
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise StorageError(
                'database_protocol_error',
                'Logical commit value contains a reference cycle',
            )
        seen.add(identity)
        try:
            document: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise StorageError(
                        'database_protocol_error',
                        'Logical commit object keys must be strings',
                    )
                document[key] = _wire_value(item, _seen=seen)
            return document
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise StorageError(
                'database_protocol_error',
                'Logical commit value contains a reference cycle',
            )
        seen.add(identity)
        try:
            return [_wire_value(item, _seen=seen) for item in value]
        finally:
            seen.remove(identity)
    raise StorageError(
        'database_protocol_error',
        'Logical commit contains an unsupported value type',
    )


def decode_logical_payload(
    record: Mapping[str, Any],
    *,
    cipher: BoundPayloadCipher | None = None,
) -> dict[str, Any]:
    """Authenticate and decrypt one logical record's request/response body."""
    payload = record.get('payload')
    if not isinstance(payload, Mapping):
        raise ValueError('logical record payload is missing')
    contract = payload.get('contract')
    ciphertext = payload.get('ciphertext')
    if not isinstance(contract, Mapping) or not isinstance(ciphertext, str):
        raise ValueError('logical record encrypted payload is invalid')
    if contract.get('payload_codec') != _PAYLOAD_CODEC:
        raise ValueError('logical record payload codec is unsupported')
    resolved_cipher = cipher or bound_payload_cipher()
    if contract.get('encryption_key_id') != resolved_cipher.key_id:
        raise ValueError('logical record encryption key identity differs')
    try:
        cleartext = resolved_cipher.open(
            ciphertext,
            purpose=_PAYLOAD_PURPOSE,
            tenant_id=str(record.get('tenant_id') or ''),
            owner_user_id=int(record.get('owner_user_id')),
            record_id=str(record.get('event_id') or ''),
        )
        document = json.loads(cleartext)
    except (SecretEnvelopeError, TypeError, ValueError) as exc:
        raise ValueError('logical record payload binding is invalid') from exc
    if not isinstance(document, dict) or not isinstance(
            document.get('request'), dict):
        raise ValueError('logical record clear payload is invalid')
    expected_binding = {
        'operation': record.get('operation'),
        'request_digest': record.get('request_digest'),
        'sequence': record.get('sequence'),
        'stream_id': record.get('stream_id'),
    }
    if document.get('contract') != dict(contract) or (
            document.get('binding') != expected_binding):
        raise ValueError('logical record clear payload metadata differs')
    return document


def _identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    principal = payload.get('principal')
    principal_map = principal if isinstance(principal, Mapping) else {}
    tenant_candidate = payload.get('tenant_id', principal_map.get('tenant_id'))
    tenant_id = (
        tenant_candidate
        if isinstance(tenant_candidate, str)
        and 1 <= len(tenant_candidate) <= 128
        else _SYSTEM_TENANT_ID
    )
    owner_candidate = payload.get(
        'owner_user_id',
        payload.get('user_id', principal_map.get('owner_user_id', 0)),
    )
    owner_user_id = (
        owner_candidate
        if isinstance(owner_candidate, int)
        and not isinstance(owner_candidate, bool)
        and owner_candidate >= 0
        else 0
    )
    return tenant_id, owner_user_id


def _event_id(
    *, operation: str, command_id: str, request_id: str, request_digest: str,
) -> str:
    if command_id:
        identity = f'command\0{operation}\0{command_id}'
    else:
        identity = f'request\0{operation}\0{request_id}\0{request_digest}'
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _meta_int(session: Session, key: str, default: int = 0) -> int:
    row = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?', (key,))
    if row is None:
        return default
    try:
        value = int(row['meta_value'])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(
            'database_integrity', 'Logical outbox metadata is invalid') from exc
    if value < 0:
        raise StorageError(
            'database_integrity', 'Logical outbox metadata is invalid')
    return value


def _set_meta(session: Session, key: str, value: str | int) -> None:
    session.execute(
        'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) '
        'ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value',
        (key, str(value)),
    )


def _bootstrap(
    session: Session, *, encryption_key_id: str,
) -> dict[str, Any]:
    """Reconcile bounded counters and mint one backend-neutral lineage ID."""
    session.lock_key('logical_outbox', 'sequence')
    if len(encryption_key_id) != 16 or any(
            character not in '0123456789abcdef'
            for character in encryption_key_id):
        raise StorageError(
            'database_integrity', 'Logical encryption key identity is invalid')
    stored_key = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        (_META_ENCRYPTION_KEY_ID,),
    )
    if stored_key is None:
        pending_key = session.fetch_one(
            'SELECT MIN(encryption_key_id) AS first_key, '
            'MAX(encryption_key_id) AS last_key '
            'FROM storage_logical_outbox')
        first_key = str((pending_key or {}).get('first_key') or '')
        last_key = str((pending_key or {}).get('last_key') or '')
        if first_key and (
                first_key != encryption_key_id or last_key != first_key):
            raise StorageError(
                'database_integrity',
                'Logical outbox contains a different encryption key identity')
        _set_meta(session, _META_ENCRYPTION_KEY_ID, encryption_key_id)
    elif str(stored_key['meta_value']) != encryption_key_id:
        raise StorageError(
            'database_integrity',
            'Logical encryption key changed without a replay keyring migration')
    stream = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        (_META_STREAM_ID,),
    )
    if stream is None:
        authority = session.fetch_one(
            'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
            ('authority_uuid',),
        )
        stream_id = str(
            authority['meta_value'] if authority is not None else uuid.uuid4().hex)
        _set_meta(session, _META_STREAM_ID, stream_id)
    else:
        stream_id = str(stream['meta_value'])
    if not 1 <= len(stream_id) <= 128:
        raise StorageError(
            'database_integrity', 'Logical outbox stream identity is invalid')

    aggregate = session.fetch_one(
        'SELECT COALESCE(MIN(sequence), 0) AS first_sequence, '
        'COALESCE(MAX(sequence), 0) AS last_pending_sequence, '
        'COALESCE(SUM(record_bytes), 0) AS pending_bytes, '
        'COUNT(*) AS pending_records FROM storage_logical_outbox')
    assert aggregate is not None
    first_pending = int(aggregate['first_sequence'])
    last_pending = int(aggregate['last_pending_sequence'])
    pending_bytes = int(aggregate['pending_bytes'])
    pending_records = int(aggregate['pending_records'])
    last_sequence = _meta_int(session, _META_LAST_SEQUENCE, last_pending)
    if last_sequence < last_pending:
        last_sequence = last_pending
    published_default = first_pending - 1 if first_pending else last_sequence
    published_sequence = _meta_int(
        session, _META_PUBLISHED_SEQUENCE, published_default)
    if published_sequence > last_sequence:
        raise StorageError(
            'database_integrity', 'Logical outbox cursor exceeds its sequence')
    if pending_records and (
            last_pending - first_pending + 1 != pending_records):
        raise StorageError(
            'database_integrity', 'Logical outbox contains a sequence gap')
    if first_pending and (
            first_pending != published_sequence + 1
            or last_pending != last_sequence):
        raise StorageError(
            'database_integrity', 'Logical outbox pending sequence has a gap')
    if not first_pending and published_sequence != last_sequence:
        raise StorageError(
            'database_integrity', 'Logical outbox acknowledged rows are missing')
    _set_meta(session, _META_LAST_SEQUENCE, last_sequence)
    _set_meta(session, _META_PUBLISHED_SEQUENCE, published_sequence)
    _set_meta(session, _META_PENDING_BYTES, pending_bytes)
    return {
        'stream_id': stream_id,
        'first_pending_sequence': first_pending,
        'last_sequence': last_sequence,
        'published_sequence': published_sequence,
        'pending_bytes': pending_bytes,
        'pending_records': pending_records,
    }


def _capture(
    session: Session,
    policy: LogicalOutboxPolicy,
    *,
    cipher: BoundPayloadCipher,
    stream_id: str,
    operation: str,
    request_id: str,
    request_digest: str,
    command_id: str | None,
    payload: Mapping[str, Any],
    response: Any,
    mutations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> int | None:
    if not receipt_cacheable(response):
        return None
    request_document = _wire_value(payload)
    if not isinstance(request_document, dict):
        raise StorageError(
            'database_protocol_error', 'Logical command request is not an object')
    response_document = _wire_value(response)
    mutation_document = _wire_value(list(mutations))
    if not isinstance(mutation_document, list):
        raise StorageError(
            'database_protocol_error', 'Logical mutations are not an array')
    resolved_command_id = (
        command_id
        if isinstance(command_id, str) and 1 <= len(command_id) <= 200
        else ''
    )
    tenant_id, owner_user_id = _identity(payload)

    session.lock_key('logical_outbox', 'sequence')
    last_sequence = _meta_int(session, _META_LAST_SEQUENCE)
    pending_bytes = _meta_int(session, _META_PENDING_BYTES)
    sequence = last_sequence + 1
    event_id = _event_id(
        operation=operation,
        command_id=resolved_command_id,
        request_id=request_id,
        request_digest=request_digest,
    )
    committed_at_ms = int(time.time() * 1000)
    schema_version = _meta_int(session, 'schema_version')
    logical_contract = {
        'encryption_key_id': cipher.key_id,
        'operation_registry_version': REGISTRY_VERSION,
        'payload_codec': _PAYLOAD_CODEC,
        'schema_version': schema_version,
    }
    clear_payload = _canonical_bytes({
        'binding': {
            'operation': operation,
            'request_digest': request_digest,
            'sequence': sequence,
            'stream_id': stream_id,
        },
        'contract': logical_contract,
        'mutations': mutation_document,
        'request': request_document,
        'response': response_document,
    }).decode('utf-8')
    payload_ciphertext = cipher.seal(
        clear_payload,
        purpose=_PAYLOAD_PURPOSE,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        record_id=event_id,
    )
    logical_record = {
        'command_id': resolved_command_id or None,
        'committed_at_ms': committed_at_ms,
        'event_id': event_id,
        'format': RECORD_FORMAT,
        'operation': operation,
        'owner_user_id': owner_user_id,
        'payload': {
            'contract': logical_contract,
            'ciphertext': payload_ciphertext,
        },
        'request_digest': request_digest,
        'sequence': sequence,
        'stream_id': stream_id,
        'tenant_id': tenant_id,
    }
    record_bytes = len(_canonical_bytes(logical_record))
    if record_bytes > policy.max_record_bytes:
        raise StorageError(
            'storage_payload_too_large',
            'Logical recovery record exceeds its configured per-record budget',
        )
    if pending_bytes + record_bytes > policy.max_pending_bytes:
        raise StorageError(
            'database_busy',
            'Logical recovery backlog reached its configured durability budget',
            True,
            250,
        )
    session.execute(
        'INSERT INTO storage_logical_outbox('
        'sequence, event_id, operation, schema_version, registry_version, '
        'request_id, request_digest, command_id, tenant_id, owner_user_id, '
        'encryption_key_id, payload_ciphertext, record_bytes, committed_at_ms) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            sequence,
            event_id,
            operation,
            schema_version,
            REGISTRY_VERSION,
            request_id,
            request_digest,
            resolved_command_id,
            tenant_id,
            owner_user_id,
            cipher.key_id,
            payload_ciphertext,
            record_bytes,
            committed_at_ms,
        ),
    )
    _set_meta(session, _META_LAST_SEQUENCE, sequence)
    _set_meta(session, _META_PENDING_BYTES, pending_bytes + record_bytes)
    return record_bytes


def _fetch_pending(session: Session, limit: int) -> list[LogicalOutboxRecord]:
    rows = session.fetch_all(
        'SELECT sequence, event_id, operation, request_id, request_digest, '
        'schema_version, registry_version, command_id, tenant_id, '
        'owner_user_id, encryption_key_id, payload_ciphertext, record_bytes, '
        'committed_at_ms FROM storage_logical_outbox '
        'ORDER BY sequence LIMIT ?',
        (limit,),
    )
    return [
        LogicalOutboxRecord(
            sequence=int(row['sequence']),
            event_id=str(row['event_id']),
            operation=str(row['operation']),
            schema_version=int(row['schema_version']),
            registry_version=int(row['registry_version']),
            request_id=str(row['request_id']),
            request_digest=str(row['request_digest']),
            command_id=str(row['command_id'] or ''),
            tenant_id=str(row['tenant_id']),
            owner_user_id=int(row['owner_user_id']),
            encryption_key_id=str(row['encryption_key_id']),
            payload_ciphertext=str(row['payload_ciphertext']),
            record_bytes=int(row['record_bytes']),
            committed_at_ms=int(row['committed_at_ms']),
        )
        for row in rows
    ]


def _acknowledge(session: Session, record: LogicalOutboxRecord) -> dict[str, int]:
    session.lock_key('logical_outbox', 'sequence')
    row = session.fetch_one(
        'SELECT event_id, record_bytes FROM storage_logical_outbox '
        'WHERE sequence = ?',
        (record.sequence,),
    )
    published = _meta_int(session, _META_PUBLISHED_SEQUENCE)
    if row is None:
        if published >= record.sequence:
            return {'published_sequence': published, 'pending_bytes': _meta_int(
                session, _META_PENDING_BYTES)}
        raise StorageError(
            'database_integrity', 'Logical outbox row disappeared before publish')
    if str(row['event_id']) != record.event_id:
        raise StorageError(
            'database_integrity', 'Logical outbox event identity changed')
    if record.sequence != published + 1:
        raise StorageError(
            'database_integrity', 'Logical outbox acknowledgement is out of order')
    deleted = session.execute(
        'DELETE FROM storage_logical_outbox WHERE sequence = ? AND event_id = ?',
        (record.sequence, record.event_id),
    )
    if deleted != 1:
        raise StorageError(
            'database_integrity', 'Logical outbox acknowledgement lost its row')
    pending_bytes = max(
        0,
        _meta_int(session, _META_PENDING_BYTES) - int(row['record_bytes']),
    )
    _set_meta(session, _META_PUBLISHED_SEQUENCE, record.sequence)
    _set_meta(session, _META_PENDING_BYTES, pending_bytes)
    return {
        'published_sequence': record.sequence,
        'pending_bytes': pending_bytes,
    }


def policy_from_config(
    config: SidecarConfig,
    backend: Backend,
) -> LogicalOutboxPolicy:
    """Resolve one fail-closed policy after backend topology probing."""
    mode = config.logical_shadow_mode
    root = config.logical_shadow_dir
    if mode == 'off':
        return LogicalOutboxPolicy(
            mode, False, False, None,
            'disabled by TOFU_STORAGE_LOGICAL_SHADOW=off',
            config.logical_outbox_max_bytes,
            config.logical_record_max_bytes,
            config.logical_shadow_segment_bytes,
            config.logical_shadow_max_bytes,
            config.logical_publish_batch_size,
            config.logical_shadow_access,
        )
    if config.distributed_preview_read_only:
        if mode == 'required':
            raise RuntimeError(
                'required logical publishing is incompatible with read-only preview')
        return LogicalOutboxPolicy(
            mode, False, False, None, 'read-only preview has no command stream',
            config.logical_outbox_max_bytes,
            config.logical_record_max_bytes,
            config.logical_shadow_segment_bytes,
            config.logical_shadow_max_bytes,
            config.logical_publish_batch_size,
            config.logical_shadow_access,
        )

    explicit_root = root is not None
    if config.backend == 'postgres' and not explicit_root:
        if mode == 'required':
            raise RuntimeError(
                'distributed logical publishing requires an explicit shared '
                'TOFU_STORAGE_LOGICAL_SHADOW_DIR')
        return LogicalOutboxPolicy(
            mode, False, False, None,
            'distributed auto mode requires an explicit shared sink',
            config.logical_outbox_max_bytes,
            config.logical_record_max_bytes,
            config.logical_shadow_segment_bytes,
            config.logical_shadow_max_bytes,
            config.logical_publish_batch_size,
            config.logical_shadow_access,
        )

    if root is None:
        root = config.data_dir / 'logical-commits'
    if mode == 'auto' and config.backend == 'sqlite' and not explicit_root:
        metrics = backend.metrics()
        fastpath = metrics.get('fastpath', {})
        if not isinstance(fastpath, Mapping) or not fastpath.get('active'):
            return LogicalOutboxPolicy(
                mode, False, False, None,
                'auto mode waits for an active measured local write front',
                config.logical_outbox_max_bytes,
                config.logical_record_max_bytes,
                config.logical_shadow_segment_bytes,
                config.logical_shadow_max_bytes,
                config.logical_publish_batch_size,
                config.logical_shadow_access,
            )

    publisher_enabled = config.process_role in {'all', 'scheduler'}
    return LogicalOutboxPolicy(
        mode,
        True,
        publisher_enabled,
        root,
        'transactional capture active',
        config.logical_outbox_max_bytes,
        config.logical_record_max_bytes,
        config.logical_shadow_segment_bytes,
        config.logical_shadow_max_bytes,
        config.logical_publish_batch_size,
        config.logical_shadow_access,
    )


class LogicalOutboxPipeline:
    """One backend-neutral outbox capture/publish lifecycle."""

    def __init__(
        self,
        backend: Backend,
        policy: LogicalOutboxPolicy,
        *,
        sink_factory: SinkFactory = LogicalCommitShadow,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self._sink_factory = sink_factory
        self._sink_kind = str(
            getattr(sink_factory, 'kind', '')
            or ('filesystem' if sink_factory is LogicalCommitShadow
                else 'custom'))
        self._sink: LogicalCommitSink | None = None
        self._payload_cipher: BoundPayloadCipher | None = None
        self._stream_id = ''
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._startup_done = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._status: dict[str, Any] = {
            'mode': policy.mode,
            'capture_enabled': policy.capture_enabled,
            'publisher_enabled': policy.publisher_enabled,
            'state': 'disabled' if not policy.capture_enabled else 'starting',
            'reason': policy.reason,
            'pending_records': 0,
            'pending_bytes': 0,
            'max_pending_bytes': policy.max_pending_bytes,
            'last_sequence': 0,
            'published_sequence': 0,
            'captured_total': 0,
            'published_total': 0,
            'duplicate_retries': 0,
            'publish_failures': 0,
            'last_publish_at_ms': 0,
        }

    @classmethod
    def from_config(
        cls,
        config: SidecarConfig,
        backend: Backend,
        *,
        sink_factory: SinkFactory = LogicalCommitShadow,
    ) -> 'LogicalOutboxPipeline':
        return cls(
            backend,
            policy_from_config(config, backend),
            sink_factory=sink_factory,
        )

    @property
    def capture_enabled(self) -> bool:
        return bool(self.policy.capture_enabled and self._stream_id)

    def _backend_command(
        self, name: str, operation: Callable[[Session], Any], *, timeout_s: float = 10,
    ) -> Any:
        return self.backend.command(
            name,
            hashlib.sha256(name.encode('utf-8')).hexdigest(),
            None,
            'maintenance',
            operation,
            time.monotonic() + timeout_s,
            receipt_required=False,
        )

    def start(self) -> dict[str, Any]:
        if not self.policy.capture_enabled:
            return self.status()
        self._payload_cipher = bound_payload_cipher()
        assert self._payload_cipher is not None
        bootstrap = self._backend_command(
            'logical_outbox.bootstrap',
            lambda session: _bootstrap(
                session,
                encryption_key_id=self._payload_cipher.key_id,
            ),
        )
        self._stream_id = str(bootstrap['stream_id'])
        with self._state_lock:
            self._status.update(bootstrap)
            self._status['state'] = (
                'capture-only'
                if not self.policy.publisher_enabled else 'starting-publisher')
        if not self.policy.publisher_enabled:
            return self.status()
        self._thread = threading.Thread(
            target=self._run,
            name='storage-logical-publisher',
            daemon=True,
        )
        self._thread.start()
        if self.policy.mode == 'required':
            if not self._startup_done.wait(_SINK_STARTUP_TIMEOUT_S):
                with self._state_lock:
                    self._status.update({
                        'state': 'degraded',
                        'reason': 'required sink preflight timed out',
                    })
                raise RuntimeError('required logical sink preflight timed out')
            state = self.status()['state']
            if state not in {'ready', 'standby'}:
                raise RuntimeError(
                    f'required logical sink failed preflight ({state})')
        return self.status()

    def capture(
        self,
        session: Session,
        *,
        operation: str,
        request_id: str,
        request_digest: str,
        command_id: str | None,
        payload: Mapping[str, Any],
        response: Any,
        mutations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> int | None:
        if not self.capture_enabled:
            return None
        if self._payload_cipher is None:
            raise StorageError(
                'database_unavailable', 'Logical payload cipher is unavailable')
        return _capture(
            session,
            self.policy,
            cipher=self._payload_cipher,
            stream_id=self._stream_id,
            operation=operation,
            request_id=request_id,
            request_digest=request_digest,
            command_id=command_id,
            payload=payload,
            response=response,
            mutations=mutations,
        )

    def execute_and_capture(
        self,
        session: Session,
        semantic_callback: Callable[[Session], Any],
        *,
        operation: str,
        request_id: str,
        request_digest: str,
        command_id: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, int | None]:
        """Run one handler while collecting its portable mutation program."""
        recorder = LogicalMutationRecordingSession(session)
        response = semantic_callback(recorder)
        # Natural-idempotency operations deliberately bypass command receipts.
        # Their retry path may verify existing state without changing a row.
        # Such a transaction has no logical commit to publish; assigning the
        # original command identity a second stream sequence would both waste
        # capacity and collide while the first event is still pending. Keep
        # zero-row statements when any sibling did mutate, because they are
        # useful divergence assertions during replay.
        if not any(
            int(mutation['rowcount']) > 0 for mutation in recorder.mutations
        ):
            return response, None
        record_bytes = self.capture(
            session,
            operation=operation,
            request_id=request_id,
            request_digest=request_digest,
            command_id=command_id,
            payload=payload,
            response=response,
            mutations=recorder.mutations,
        )
        return response, record_bytes

    def notify(self, committed_record_bytes: int | None = None) -> None:
        if committed_record_bytes is not None:
            with self._state_lock:
                self._status['captured_total'] = int(
                    self._status.get('captured_total', 0)) + 1
        self._wake.set()

    def _state_snapshot(self) -> dict[str, int]:
        def read(session: Session) -> dict[str, int]:
            row = session.fetch_one(
                'SELECT COUNT(*) AS pending_records, '
                'COALESCE(SUM(record_bytes), 0) AS pending_bytes '
                'FROM storage_logical_outbox')
            assert row is not None
            return {
                'pending_records': int(row['pending_records']),
                'pending_bytes': int(row['pending_bytes']),
                'last_sequence': _meta_int(session, _META_LAST_SEQUENCE),
                'published_sequence': _meta_int(
                    session, _META_PUBLISHED_SEQUENCE),
            }

        return self.backend.query(
            'logical_outbox.state', read, time.monotonic() + 10)

    def _open_and_validate_sink(self) -> None:
        if self.policy.sink_root is None:
            raise LogicalShadowCorruptionError('logical sink root is missing')
        sink = self._sink_factory(
            self.policy.sink_root,
            stream_id=self._stream_id,
            max_segment_bytes=self.policy.max_segment_bytes,
            max_record_bytes=self.policy.max_record_bytes,
            max_total_bytes=self.policy.max_shadow_bytes,
            access_mode=self.policy.access_mode,
        )
        try:
            state = self._state_snapshot()
            sink_status = _sink_status_document(sink)
            shadow_last = int(sink_status['next_sequence']) - 1
            published = state['published_sequence']
            if shadow_last < published:
                raise LogicalShadowCorruptionError(
                    'logical sink is behind the acknowledged publish cursor')
            if shadow_last > state['last_sequence']:
                raise LogicalShadowCorruptionError(
                    'logical sink is ahead of the database stream')
        except BaseException:
            sink.close()
            raise
        self._sink = sink
        with self._state_lock:
            self._status.update(state)
            self._status.update({
                'state': 'ready',
                'reason': 'publisher owns the durable sink',
                'sink': sink_status,
            })
        self._startup_done.set()

    def _pending_batch(self) -> list[LogicalOutboxRecord]:
        def read(session: Session) -> tuple[
            list[LogicalOutboxRecord], dict[str, int]
        ]:
            records = _fetch_pending(session, self.policy.publish_batch_size)
            row = session.fetch_one(
                'SELECT COUNT(*) AS pending_records, '
                'COALESCE(SUM(record_bytes), 0) AS pending_bytes '
                'FROM storage_logical_outbox')
            assert row is not None
            state = {
                'pending_records': int(row['pending_records']),
                'pending_bytes': int(row['pending_bytes']),
                'last_sequence': _meta_int(session, _META_LAST_SEQUENCE),
                'published_sequence': _meta_int(
                    session, _META_PUBLISHED_SEQUENCE),
            }
            return records, state

        records, state = self.backend.query(
            'logical_outbox.pending', read, time.monotonic() + 10)
        with self._state_lock:
            self._status.update(state)
        return records

    def _publish(self, record: LogicalOutboxRecord) -> None:
        assert self._sink is not None
        receipt = self._sink.append(
            operation=record.operation,
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            payload={
                'contract': {
                    'encryption_key_id': record.encryption_key_id,
                    'operation_registry_version': record.registry_version,
                    'payload_codec': _PAYLOAD_CODEC,
                    'schema_version': record.schema_version,
                },
                'ciphertext': record.payload_ciphertext,
            },
            command_id=record.command_id or None,
            request_digest=record.request_digest,
            committed_at_ms=record.committed_at_ms,
            event_id=record.event_id,
            expected_sequence=record.sequence,
        )
        result = self._backend_command(
            'logical_outbox.ack',
            lambda session: _acknowledge(session, record),
        )
        with self._state_lock:
            self._status.update({
                'state': 'ready',
                'reason': 'publisher owns the durable sink',
                'published_sequence': int(result['published_sequence']),
                'pending_bytes': int(result['pending_bytes']),
                'pending_records': max(
                    0, int(self._status.get('pending_records', 0)) - 1),
                'published_total': int(
                    self._status.get('published_total', 0)) + 1,
                'last_publish_at_ms': int(time.time() * 1000),
                'sink': _sink_status_document(self._sink),
            })
            if receipt.duplicate:
                self._status['duplicate_retries'] = int(
                    self._status.get('duplicate_retries', 0)) + 1

    def _drop_sink(self) -> None:
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                logger.debug('logical sink close failed', exc_info=True)

    def _run(self) -> None:
        backoff_s = 0.25
        while not self._stop.is_set():
            try:
                if self._sink is None:
                    self._open_and_validate_sink()
                batch = self._pending_batch()
                if not batch:
                    backoff_s = 0.25
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                for record in batch:
                    if self._stop.is_set():
                        return
                    self._publish(record)
                backoff_s = 0.25
            except LogicalShadowBusyError:
                self._drop_sink()
                with self._state_lock:
                    self._status.update({
                        'state': 'standby',
                        'reason': 'another publisher owns the sink writer lock',
                    })
                self._startup_done.set()
                self._stop.wait(min(5.0, max(1.0, backoff_s)))
                backoff_s = min(5.0, backoff_s * 2)
            except LogicalShadowCapacityError:
                with self._state_lock:
                    self._status.update({
                        'state': 'blocked',
                        'reason': 'durable logical sink reached its byte budget',
                        'publish_failures': int(
                            self._status.get('publish_failures', 0)) + 1,
                    })
                self._startup_done.set()
                self._stop.wait(30.0)
            except LogicalShadowCorruptionError:
                self._drop_sink()
                with self._state_lock:
                    self._status.update({
                        'state': 'poisoned',
                        'reason': 'logical sink continuity validation failed',
                        'publish_failures': int(
                            self._status.get('publish_failures', 0)) + 1,
                    })
                self._startup_done.set()
                self._stop.wait(30.0)
            except Exception as exc:
                self._drop_sink()
                logger.warning(
                    'logical publisher retrying after %s', type(exc).__name__)
                with self._state_lock:
                    self._status.update({
                        'state': 'degraded',
                        'reason': f'publisher retrying ({type(exc).__name__})',
                        'publish_failures': int(
                            self._status.get('publish_failures', 0)) + 1,
                    })
                self._startup_done.set()
                self._stop.wait(backoff_s)
                backoff_s = min(30.0, backoff_s * 2)

    def health_ready(self) -> bool:
        status = self.status()
        if self.policy.mode != 'required' or not self.policy.capture_enabled:
            return True
        return status['state'] not in {'blocked', 'poisoned', 'degraded'}

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            status = dict(self._status)
            sink = status.get('sink')
            if isinstance(sink, Mapping):
                status['sink'] = dict(sink)
        status['stream_id'] = self._stream_id
        status['encryption_key_id'] = (
            self._payload_cipher.key_id
            if self._payload_cipher is not None else '')
        status['sink_kind'] = self._sink_kind
        status['durability'] = (
            'database-transactional-outbox-then-sink-fsync'
            if self.policy.capture_enabled else 'database-authority-only')
        return status

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(
                timeout=10 if self._startup_done.is_set() else 0.25)
            if self._thread.is_alive():
                with self._state_lock:
                    self._status['state'] = 'abandoned-blocked-io'
                self._thread = None
                return
            self._thread = None
        self._drop_sink()
        with self._state_lock:
            self._status['state'] = 'closed'


__all__ = [
    'LogicalCommitSink',
    'LogicalOutboxPipeline',
    'LogicalOutboxPolicy',
    'LogicalOutboxRecord',
    'LogicalMutationRecordingSession',
    'decode_logical_payload',
    'policy_from_config',
]

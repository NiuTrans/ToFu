"""Backend-neutral logical replay, projection verification, and cutover gates.

Responsibility: consume validated ``tofu.logical-commit.v1`` records through a
target that atomically applies state and advances its checkpoint; summarize
ordered projections without loading them into memory; and decide whether a
shadow/canary/authority transition is safe. This module performs no implicit
cutover and never selects a storage authority by itself.

Entry points are ``BackendReplayTarget``, ``replay_records``,
``projection_digest``, ``select_canary_read``, and ``assess_cutover``. The
built-in target applies authenticated mutation programs to SQLite or
PostgreSQL and advances its checkpoint in the same transaction. Alternative
projections can implement ``ReplayTarget.apply_and_checkpoint`` with the same
atomic boundary.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import time
from typing import Any, Protocol

from lib.secret_envelope import BoundPayloadCipher, bound_payload_cipher
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Backend, Session
from lib.storage_sidecar.logical_outbox import decode_logical_payload
from lib.storage_sidecar.logical_shadow import LogicalCommitShadow, RECORD_FORMAT


_GENESIS_DIGEST = hashlib.sha256(b'tofu.logical-replay.genesis.v1').hexdigest()
_REPLAY_MUTATION_PREFIXES = (
    'delete ', 'insert ', 'replace ', 'update ', 'with ',
)


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
        raise ValueError('replay value is not canonical JSON') from exc


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    stream_id: str
    last_sequence: int = 0
    chain_digest: str = _GENESIS_DIGEST


class ReplayTarget(Protocol):
    """Projection/recovery target with an atomic apply-and-cursor boundary."""

    def checkpoint(self) -> ReplayCheckpoint: ...

    def apply_and_checkpoint(
        self,
        record: Mapping[str, Any],
        checkpoint: ReplayCheckpoint,
    ) -> None: ...


def _unwire_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_unwire_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'$bytes'}:
            encoded = value['$bytes']
            if not isinstance(encoded, str):
                raise ValueError('logical mutation binary value is invalid')
            try:
                return base64.b64decode(encoded.encode('ascii'), validate=True)
            except (UnicodeError, ValueError) as exc:
                raise ValueError(
                    'logical mutation binary value is invalid') from exc
        return {key: _unwire_value(item) for key, item in value.items()}
    return value


def apply_logical_mutations(
    session: Session,
    record: Mapping[str, Any],
    *,
    cipher: BoundPayloadCipher | None = None,
) -> int:
    """Apply one authenticated portable mutation program to a target session."""
    document = decode_logical_payload(record, cipher=cipher)
    contract = document.get('contract')
    if not isinstance(contract, Mapping):
        raise StorageError(
            'database_integrity', 'Logical replay contract is missing')
    current = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        ('schema_version',),
    )
    try:
        current_schema = int(current['meta_value']) if current is not None else 0
        record_schema = int(contract['schema_version'])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(
            'database_integrity', 'Logical replay schema contract is invalid') from exc
    if current_schema != record_schema:
        raise StorageError(
            'database_integrity',
            'Logical replay requires an explicit schema-version adapter',
        )
    mutations = document.get('mutations')
    if not isinstance(mutations, list) or len(mutations) > 4096:
        raise StorageError(
            'database_integrity', 'Logical replay mutation program is invalid')
    applied = 0
    for mutation in mutations:
        if not isinstance(mutation, Mapping):
            raise StorageError(
                'database_integrity', 'Logical replay mutation is invalid')
        sql = mutation.get('sql')
        params = mutation.get('params')
        expected_rowcount = mutation.get('rowcount')
        if (
            not isinstance(sql, str)
            or len(sql.encode('utf-8')) > 64 * 1024
            or not sql.lstrip().lower().startswith(_REPLAY_MUTATION_PREFIXES)
            or not isinstance(params, list)
            or len(params) > 4096
            or not isinstance(expected_rowcount, int)
            or isinstance(expected_rowcount, bool)
            or expected_rowcount < 0
        ):
            raise StorageError(
                'database_integrity', 'Logical replay mutation is invalid')
        decoded_params = _unwire_value(params)
        actual_rowcount = session.execute(sql, tuple(decoded_params))
        if actual_rowcount != expected_rowcount:
            raise StorageError(
                'database_conflict',
                'Logical replay target state diverged from the source mutation',
            )
        applied += 1
    return applied


class BackendReplayTarget:
    """SQLite/PostgreSQL replay target with an atomic durable checkpoint."""

    def __init__(
        self,
        backend: Backend,
        *,
        target_name: str,
        stream_id: str,
        cipher: BoundPayloadCipher | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not target_name or len(target_name) > 128:
            raise ValueError('replay target_name is invalid')
        if not stream_id or len(stream_id) > 128:
            raise ValueError('replay stream_id is invalid')
        if not 1.0 <= timeout_s <= 300.0:
            raise ValueError('replay timeout_s must be between 1 and 300')
        self.backend = backend
        self.target_name = target_name
        self.stream_id = stream_id
        self.cipher = cipher or bound_payload_cipher()
        self.timeout_s = timeout_s

    def checkpoint(self) -> ReplayCheckpoint:
        def read(session: Session) -> ReplayCheckpoint:
            row = session.fetch_one(
                'SELECT stream_id, last_sequence, chain_digest '
                'FROM storage_logical_replay_checkpoints '
                'WHERE target_name = ?',
                (self.target_name,),
            )
            if row is None:
                return ReplayCheckpoint(self.stream_id)
            if str(row['stream_id']) != self.stream_id:
                raise StorageError(
                    'database_integrity',
                    'Replay target already belongs to another stream')
            return ReplayCheckpoint(
                stream_id=self.stream_id,
                last_sequence=int(row['last_sequence']),
                chain_digest=str(row['chain_digest']),
            )

        return self.backend.query(
            'logical_replay.checkpoint',
            read,
            time.monotonic() + self.timeout_s,
        )

    def apply_and_checkpoint(
        self,
        record: Mapping[str, Any],
        checkpoint: ReplayCheckpoint,
    ) -> None:
        if checkpoint.stream_id != self.stream_id:
            raise ValueError('replay checkpoint stream differs')

        def apply(session: Session) -> None:
            session.lock_key('logical_replay.target', self.target_name)
            row = session.fetch_one(
                'SELECT stream_id, last_sequence, chain_digest '
                'FROM storage_logical_replay_checkpoints '
                'WHERE target_name = ?',
                (self.target_name,),
            )
            current = (
                ReplayCheckpoint(self.stream_id)
                if row is None else ReplayCheckpoint(
                    stream_id=str(row['stream_id']),
                    last_sequence=int(row['last_sequence']),
                    chain_digest=str(row['chain_digest']),
                )
            )
            if current.stream_id != self.stream_id:
                raise StorageError(
                    'database_integrity',
                    'Replay target already belongs to another stream')
            if current.last_sequence == checkpoint.last_sequence:
                if current.chain_digest != checkpoint.chain_digest:
                    raise StorageError(
                        'database_integrity',
                        'Replay checkpoint digest diverged at one sequence')
                return
            if current.last_sequence != checkpoint.last_sequence - 1:
                raise StorageError(
                    'database_conflict', 'Replay target cursor is not contiguous')
            apply_logical_mutations(session, record, cipher=self.cipher)
            session.execute(
                'INSERT INTO storage_logical_replay_checkpoints('
                'target_name, stream_id, last_sequence, chain_digest, '
                'updated_at_ms) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(target_name) DO UPDATE SET '
                'stream_id = excluded.stream_id, '
                'last_sequence = excluded.last_sequence, '
                'chain_digest = excluded.chain_digest, '
                'updated_at_ms = excluded.updated_at_ms',
                (
                    self.target_name,
                    self.stream_id,
                    checkpoint.last_sequence,
                    checkpoint.chain_digest,
                    int(time.time() * 1000),
                ),
            )

        digest = hashlib.sha256(_canonical_bytes({
            'sequence': checkpoint.last_sequence,
            'stream_id': checkpoint.stream_id,
            'target': self.target_name,
        })).hexdigest()
        self.backend.command(
            'logical_replay.apply',
            digest,
            None,
            'maintenance',
            apply,
            time.monotonic() + self.timeout_s,
            receipt_required=False,
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    stream_id: str
    start_sequence: int
    last_sequence: int
    applied_records: int
    chain_digest: str
    complete_input: bool


def _next_chain_digest(previous: str, record: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(bytes.fromhex(previous))
    digest.update(_canonical_bytes(dict(record)))
    return digest.hexdigest()


def _validate_record(
    record: Mapping[str, Any], *, stream_id: str, expected_sequence: int,
) -> None:
    if record.get('format') != RECORD_FORMAT:
        raise ValueError('logical replay record format mismatch')
    if record.get('stream_id') != stream_id:
        raise ValueError('logical replay stream lineage mismatch')
    if record.get('sequence') != expected_sequence:
        raise ValueError('logical replay sequence is not contiguous')
    if not isinstance(record.get('event_id'), str):
        raise ValueError('logical replay event identity is missing')
    if not isinstance(record.get('operation'), str):
        raise ValueError('logical replay operation is missing')
    if not isinstance(record.get('payload'), Mapping):
        raise ValueError('logical replay payload is missing')


def replay_records(
    records: Iterable[Mapping[str, Any]],
    target: ReplayTarget,
    *,
    max_records: int = 10_000,
) -> ReplayResult:
    """Apply one bounded contiguous page after the target's durable cursor."""
    if not 1 <= max_records <= 1_000_000:
        raise ValueError('max_records must be between 1 and 1000000')
    checkpoint = target.checkpoint()
    if not checkpoint.stream_id or checkpoint.last_sequence < 0:
        raise ValueError('replay checkpoint is invalid')
    try:
        bytes.fromhex(checkpoint.chain_digest)
    except ValueError as exc:
        raise ValueError('replay checkpoint digest is invalid') from exc
    if len(checkpoint.chain_digest) != 64:
        raise ValueError('replay checkpoint digest is invalid')

    start_sequence = checkpoint.last_sequence + 1
    applied = 0
    complete_input = True
    for record in records:
        expected = checkpoint.last_sequence + 1
        _validate_record(
            record,
            stream_id=checkpoint.stream_id,
            expected_sequence=expected,
        )
        checkpoint = ReplayCheckpoint(
            stream_id=checkpoint.stream_id,
            last_sequence=expected,
            chain_digest=_next_chain_digest(
                checkpoint.chain_digest, record),
        )
        # This is intentionally one target call: an implementation that writes
        # state and cursor separately cannot claim crash-safe replay.
        target.apply_and_checkpoint(record, checkpoint)
        applied += 1
        if applied >= max_records:
            # Conservatively ask the caller for another page. Avoid probing
            # the iterator for one extra item: that would consume a record
            # whose apply/checkpoint transaction has not run.
            complete_input = False
            break
    return ReplayResult(
        stream_id=checkpoint.stream_id,
        start_sequence=start_sequence,
        last_sequence=checkpoint.last_sequence,
        applied_records=applied,
        chain_digest=checkpoint.chain_digest,
        complete_input=complete_input,
    )


def replay_shadow_page(
    shadow: LogicalCommitShadow,
    target: ReplayTarget,
    *,
    max_records: int = 1000,
) -> ReplayResult:
    """Resume one bounded offline page directly from a durable shadow."""
    checkpoint = target.checkpoint()
    shadow_status = shadow.status()
    if checkpoint.stream_id != shadow_status.stream_id:
        raise ValueError('replay target and logical shadow lineage differ')
    source_last_sequence = shadow_status.next_sequence - 1
    if checkpoint.last_sequence > source_last_sequence:
        raise ValueError('replay target cursor is ahead of the logical shadow')
    records = shadow.read_records(
        start_sequence=checkpoint.last_sequence + 1,
        max_records=max_records,
    )
    result = replay_records(records, target, max_records=max_records)
    return ReplayResult(
        stream_id=result.stream_id,
        start_sequence=result.start_sequence,
        last_sequence=result.last_sequence,
        applied_records=result.applied_records,
        chain_digest=result.chain_digest,
        complete_input=result.last_sequence == source_last_sequence,
    )


@dataclass(frozen=True, slots=True)
class ProjectionDigest:
    name: str
    rows: int
    digest: str
    first_key: str
    last_key: str


def projection_digest(
    name: str,
    rows: Iterable[tuple[str, Any]],
    *,
    max_rows: int = 10_000_000,
) -> ProjectionDigest:
    """Hash an already key-ordered projection with constant memory."""
    if not name or len(name) > 128:
        raise ValueError('projection name is invalid')
    if not 1 <= max_rows <= 100_000_000:
        raise ValueError('max_rows is invalid')
    digest = hashlib.sha256()
    digest.update(b'tofu.projection-digest.v1\0')
    digest.update(name.encode('utf-8'))
    previous_key: str | None = None
    first_key = ''
    count = 0
    for key, document in rows:
        if not isinstance(key, str) or not key:
            raise ValueError('projection key is invalid')
        if previous_key is not None and key <= previous_key:
            raise ValueError('projection rows must be uniquely ordered by key')
        count += 1
        if count > max_rows:
            raise ValueError('projection row budget exceeded')
        if not first_key:
            first_key = key
        key_bytes = key.encode('utf-8')
        document_bytes = _canonical_bytes(document)
        digest.update(len(key_bytes).to_bytes(8, 'big'))
        digest.update(key_bytes)
        digest.update(len(document_bytes).to_bytes(8, 'big'))
        digest.update(document_bytes)
        previous_key = key
    return ProjectionDigest(
        name=name,
        rows=count,
        digest=digest.hexdigest(),
        first_key=first_key,
        last_key=previous_key or '',
    )


class CutoverStage(str, Enum):
    DATABASE_AUTHORITY = 'database-authority'
    SHADOW = 'shadow'
    CANARY_READS = 'canary-reads'
    LOGICAL_AUTHORITY = 'logical-authority'


@dataclass(frozen=True, slots=True)
class CutoverPolicy:
    canary_read_percent: int = 1
    minimum_verified_records: int = 1000
    require_verified_rollback: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.canary_read_percent <= 100:
            raise ValueError('canary_read_percent must be between 0 and 100')
        if not 0 <= self.minimum_verified_records <= 1_000_000_000:
            raise ValueError('minimum_verified_records is out of bounds')


def select_canary_read(
    stable_request_key: str,
    *,
    percent: int,
    salt: str = 'tofu.logical-canary.v1',
) -> bool:
    """Deterministically route a bounded percentage without sticky state."""
    if not 0 <= percent <= 100:
        raise ValueError('percent must be between 0 and 100')
    if percent in {0, 100}:
        return percent == 100
    bucket = int.from_bytes(
        hashlib.sha256(
            f'{salt}\0{stable_request_key}'.encode('utf-8')).digest()[:8],
        'big',
    ) % 10_000
    return bucket < percent * 100


@dataclass(frozen=True, slots=True)
class CutoverEvidence:
    current_stage: CutoverStage
    requested_stage: CutoverStage
    explicit_operator_request: bool
    source_sequence: int
    sink_sequence: int
    replay_sequence: int
    pending_records: int
    publisher_state: str
    verified_records: int
    source_projection: ProjectionDigest
    target_projection: ProjectionDigest
    rollback_checkpoint_verified: bool


@dataclass(frozen=True, slots=True)
class CutoverDecision:
    allowed: bool
    next_stage: CutoverStage
    reasons: tuple[str, ...]
    rollback_stage: CutoverStage = CutoverStage.DATABASE_AUTHORITY


_FORWARD_STAGE = {
    CutoverStage.DATABASE_AUTHORITY: CutoverStage.SHADOW,
    CutoverStage.SHADOW: CutoverStage.CANARY_READS,
    CutoverStage.CANARY_READS: CutoverStage.LOGICAL_AUTHORITY,
}


def assess_cutover(
    evidence: CutoverEvidence,
    policy: CutoverPolicy = CutoverPolicy(),
) -> CutoverDecision:
    """Fail closed unless every cursor, projection, and rollback gate agrees."""
    reasons: list[str] = []
    expected_stage = _FORWARD_STAGE.get(evidence.current_stage)
    if not evidence.explicit_operator_request:
        reasons.append('cutover was not explicitly requested')
    if expected_stage != evidence.requested_stage:
        reasons.append('cutover stages must advance one step at a time')
    if evidence.pending_records != 0:
        reasons.append('transactional outbox is not empty')
    if not (
        evidence.source_sequence
        == evidence.sink_sequence
        == evidence.replay_sequence
    ):
        reasons.append('source, sink, and replay cursors differ')
    if evidence.publisher_state != 'ready':
        reasons.append('logical publisher is not ready')
    if evidence.verified_records < policy.minimum_verified_records:
        reasons.append('verified record sample is below policy')
    source = evidence.source_projection
    target = evidence.target_projection
    if (
        source.name != target.name
        or source.rows != target.rows
        or source.digest != target.digest
    ):
        reasons.append('source and replay projections differ')
    if policy.require_verified_rollback and not evidence.rollback_checkpoint_verified:
        reasons.append('rollback checkpoint is not verified')
    return CutoverDecision(
        allowed=not reasons,
        next_stage=(
            evidence.requested_stage if not reasons else evidence.current_stage),
        reasons=tuple(reasons),
    )


__all__ = [
    'CutoverDecision',
    'CutoverEvidence',
    'CutoverPolicy',
    'CutoverStage',
    'BackendReplayTarget',
    'ProjectionDigest',
    'ReplayCheckpoint',
    'ReplayResult',
    'ReplayTarget',
    'apply_logical_mutations',
    'assess_cutover',
    'projection_digest',
    'replay_records',
    'replay_shadow_page',
    'select_canary_read',
]

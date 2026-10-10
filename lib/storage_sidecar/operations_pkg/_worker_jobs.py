"""Durable worker-job claims, leases, fencing, cancellation, and settlement.

The database is the task authority.  Redis or process-local notifications may
wake a worker, but every execution starts with ``worker_job.claim_next`` and
every subsequent write proves the returned fencing token.  SQLite serializes
claims through its one writer; PostgreSQL uses the adapter-owned
``FOR UPDATE SKIP LOCKED`` primitive.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage.generated_worker_job_contract import (
    WORKER_TERMINAL_STATES, WORKER_JOB_MAX_CLOCK_MS, WORKER_LEASE_MAX_NOW_MS,
    WORKER_LEASE_EPOCH_CHARACTERS, WORKER_LEASE_MAX_MS, WorkerLeaseAuthoritySnapshot,
)
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


_WORKER_JOB_COLUMNS = (
    "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, "
    "request_digest, status, priority, available_at_ms, claim_owner, "
    "lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, "
    "cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, "
    "result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms"
)
_TERMINAL_STATUSES = WORKER_TERMINAL_STATES
_MAX_JOB_PAYLOAD_BYTES = 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
_MAX_CLOCK_MS = WORKER_JOB_MAX_CLOCK_MS
# Caller clocks are bounded observation/evidence, never lease authority.
_MAX_CLOCK_SKEW_MS = 24 * 60 * 60 * 1000


def _optional_text(
    payload: Mapping[str, Any], key: str, maximum: int,
) -> str:
    value = payload.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in storage request')
    return value


def _job_owner(payload: Mapping[str, Any]) -> int:
    return _integer(payload, 'user_id', minimum=1)


def _job_now(payload: Mapping[str, Any]) -> int:
    """Validate the bounded caller observation without using it as lease time."""
    client_now = _integer(
        payload, 'now_ms', minimum=0, maximum=_MAX_CLOCK_MS)
    server_now = int(time.time() * 1000)
    if client_now - server_now > _MAX_CLOCK_SKEW_MS:
        raise StorageError(
            'database_protocol_error',
            'Worker job clock is too far ahead of the server clock',
        )
    return client_now


def _lease_authority(session: Session) -> WorkerLeaseAuthoritySnapshot:
    """Read this backend's shared lifecycle and advancing lease clock.

    Startup/maintenance sessions have no clock; fail closed rather than deriving
    authority from the caller, creating a per-session epoch, or using wall time.
    """
    try:
        snapshot = session.worker_lease_authority()
        epoch, now, utc = snapshot
        if (not isinstance(epoch, str) or len(epoch) != WORKER_LEASE_EPOCH_CHARACTERS
                or any(byte not in '0123456789abcdef' for byte in epoch)
                or type(now) is not int or not 0 < now <= WORKER_LEASE_MAX_NOW_MS
                or type(utc) is not int or not 0 < utc <= now):
            raise ValueError('invalid authority time or identity')
    except (ValueError, OverflowError, OSError) as exc:
        raise StorageError('database_internal', 'Worker lease authority clock is invalid') from exc
    return snapshot


def _live_claim(row, worker_id, fence, epoch, now):
    return (row is not None and str(row['status']) == 'running'
            and str(row['claim_owner']) == worker_id
            and int(row['fencing_token']) == fence
            and str(row['lease_authority_epoch']) == epoch
            and int(row['lease_deadline_ms']) > now)


def _job_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'taskId': str(row['task_id']),
        'userId': int(row['user_id']),
        'tenantId': str(row['tenant_id'] or ''),
        'taskKind': str(row['task_kind']),
        'payload': _load(row['payload_json']) or {},
        'idempotencyKey': str(row['idempotency_key']),
        'status': str(row['status']),
        'priority': int(row['priority']),
        'availableAtMs': int(row['available_at_ms']),
        'claimOwner': str(row['claim_owner'] or ''),
        'leaseDeadlineMs': int(row['lease_deadline_ms']),
        'fencingToken': int(row['fencing_token']),
        'attempt': int(row['attempt_no']),
        'heartbeatAtMs': int(row['heartbeat_at_ms']),
        'cancelSequence': int(row['cancel_sequence']),
        'wakeSequence': int(row['wake_sequence']),
        'cancelRequestedAtMs': int(row['cancel_requested_at_ms']),
        'cancelReason': str(row['cancel_reason'] or ''),
        'replayCursor': int(row['replay_cursor']),
        'resultRef': str(row['result_ref'] or ''),
        'error': _load(row['error_json']) or {},
        'createdAtMs': int(row['created_at_ms']),
        'updatedAtMs': int(row['updated_at_ms']),
        'terminalAtMs': int(row['terminal_at_ms']),
    }


def _job_row(
    session: Session, task_id: str, *, lock: bool = False,
) -> Mapping[str, Any] | None:
    fetch = session.fetch_one_for_update if lock else session.fetch_one
    return fetch(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE task_id = ?',
        (task_id,),
    )


def _worker_job_get(
    session: Session, payload: Mapping[str, Any], *, lock: bool = False,
) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    user_id = _job_owner(payload)
    fetch = session.fetch_one_for_update if lock else session.fetch_one
    row = fetch(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE task_id = ? AND user_id = ?',
        (task_id, user_id),
    )
    return None if row is None else _job_document(row)


def _worker_job_enqueue(session: Session, payload: Mapping[str, Any]) -> Any:
    """Generic producers cannot bypass automation's atomic job owners."""
    from lib.automations.contract import RESERVED_WORKER_TASK_KINDS
    if _required_text(payload,'task_kind',128) in RESERVED_WORKER_TASK_KINDS:
        raise StorageError('database_protocol_error','Automation jobs require their atomic owner')
    return _worker_job_enqueue_owned(session,payload)


def _worker_job_enqueue_owned(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    user_id = _job_owner(payload)
    tenant_id = _optional_text(payload, 'tenant_id', 256)
    task_kind = _required_text(payload, 'task_kind', 128)
    idempotency_key = _required_text(payload, 'idempotency_key', 256)
    priority = _integer(payload, 'priority', default=100, minimum=0, maximum=1000)
    now_ms = _job_now(payload)
    available_at_ms = _integer(
        payload, 'available_at_ms', default=now_ms,
        minimum=0, maximum=_MAX_CLOCK_MS,
    )
    job_payload = payload.get('payload', {})
    if not isinstance(job_payload, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid payload in worker job')
    encoded_payload = _dump(dict(job_payload))
    if len(encoded_payload) > _MAX_JOB_PAYLOAD_BYTES:
        raise StorageError(
            'storage_payload_too_large',
            f'Worker job payload exceeds {_MAX_JOB_PAYLOAD_BYTES} bytes',
        )
    request_digest = hashlib.sha256(_dump({
        'tenantId': tenant_id,
        'taskKind': task_kind,
        'payload': dict(job_payload),
    })).hexdigest()

    session.lock_key('worker_job_idempotency', f'{user_id}:{idempotency_key}')
    existing = session.fetch_one(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE user_id = ? AND idempotency_key = ?',
        (user_id, idempotency_key),
    )
    if existing is not None:
        if str(existing['request_digest']) != request_digest:
            raise StorageError(
                'database_conflict',
                'Worker job idempotency key was reused for another request',
            )
        return {'created': False, 'job': _job_document(existing)}
    if _job_row(session, task_id) is not None:
        raise StorageError(
            'database_conflict', 'Worker job task_id already exists')

    session.execute(
        'INSERT INTO storage_worker_jobs('
        'task_id, user_id, tenant_id, task_kind, payload_json, '
        'idempotency_key, request_digest, status, priority, available_at_ms, '
        'created_at_ms, updated_at_ms) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        (
            task_id, user_id, tenant_id, task_kind, encoded_payload,
            idempotency_key, request_digest, priority, available_at_ms,
            now_ms, now_ms,
        ),
    )
    created = _job_row(session, task_id)
    if created is None:  # pragma: no cover - transaction invariant
        raise StorageError('database_internal', 'Worker job insert was not visible')
    return {'created': True, 'job': _job_document(created)}


def _claim_kind_filter(payload: Mapping[str, Any]) -> tuple[str, tuple[Any, ...]]:
    values = payload.get('task_kinds')
    if values is None:
        # A worker must advertise the kinds for which it has a complete,
        # production-safe handler.  Claiming every queued kind by omission
        # would strand unsupported work behind a live lease and could execute
        # a payload with no accounting or side-effect fencing contract.
        raise StorageError(
            'database_protocol_error',
            'worker_job.claim_next requires explicit task_kinds',
        )
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise StorageError(
            'database_protocol_error', 'Invalid task_kinds in storage request')
    kinds = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise StorageError(
                'database_protocol_error',
                'Invalid task_kinds in storage request',
            )
        if value not in kinds:
            kinds.append(value)
    placeholders = ', '.join('?' for _ in kinds)
    return f' AND task_kind IN ({placeholders})', tuple(kinds)


def _claim_candidate(
    session: Session, *, now_ms: int, utc_now_ms: int, epoch: str, kinds: tuple[str, ...],
) -> Mapping[str, Any] | None:
    # Each kind has at most three indexed lease probes. Epoch ranges recover
    # legacy/restarted holders even if their old wall-clock deadline is future.
    expired = []
    for kind in kinds:
        for relation, bound in (('<', epoch), ('>', epoch), ('=', epoch)):
            current = relation == '='
            row = session.fetch_one_for_update_skip_locked(
                f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
                "WHERE status='running' AND task_kind=? "
                f'AND lease_authority_epoch {relation} ? '
                + ('AND lease_deadline_ms <= ? ' if current else '')
                + 'ORDER BY lease_authority_epoch, lease_deadline_ms, priority, '
                'created_at_ms, task_id LIMIT 1',
                (kind, bound, now_ms) if current else (kind, bound),
            )
            if row is not None:
                expired.append(row)
    if expired:
        return min(expired, key=lambda row: (
            int(row['lease_deadline_ms']) if row['lease_authority_epoch'] == epoch else 0,
            int(row['priority']), int(row['created_at_ms']), str(row['task_id'])))
    return session.fetch_one_for_update_skip_locked(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        "WHERE status IN ('queued', 'suspended') AND available_at_ms <= ? "
        f"AND task_kind IN ({', '.join('?' for _ in kinds)}) "
        'ORDER BY priority, available_at_ms, created_at_ms, task_id LIMIT 1',
        (utc_now_ms, *kinds),
    )


def _worker_job_claim_next(session: Session, payload: Mapping[str, Any]) -> Any:
    worker_id = _required_text(payload, 'worker_id', 256)
    _job_now(payload)
    lease_ms = _integer(
        payload, 'lease_ms', default=60_000, minimum=10_000, maximum=WORKER_LEASE_MAX_MS)
    _, kinds = _claim_kind_filter(payload)
    epoch, now_ms, utc_now_ms = _lease_authority(session)
    row = _claim_candidate(session, now_ms=now_ms, utc_now_ms=utc_now_ms, epoch=epoch, kinds=kinds)
    if row is None:
        return None
    # Candidate acquisition may block behind another transaction. Read authority
    # again after the row is owned, so the new lease starts here.
    epoch, now_ms, utc_now_ms = _lease_authority(session)
    task_id = str(row['task_id'])
    old_fence = int(row['fencing_token'])
    new_fence = old_fence + 1
    changed = session.execute(
        "UPDATE storage_worker_jobs SET status='running', claim_owner=?, "
        'lease_deadline_ms=?, lease_authority_epoch=?, fencing_token=?, '
        'attempt_no=attempt_no+1, heartbeat_at_ms=?, updated_at_ms=? '
        'WHERE task_id=? AND fencing_token=?',
        (worker_id, now_ms + lease_ms, epoch, new_fence, now_ms, now_ms,
         task_id, old_fence),
    )
    if changed != 1:  # pragma: no cover - serialized writer / row lock invariant
        raise StorageError('database_conflict', 'Worker job claim lost its fence')
    claimed = _job_row(session, task_id)
    if claimed is None:  # pragma: no cover - transaction invariant
        raise StorageError('database_internal', 'Claimed worker job disappeared')
    return _job_document(claimed)


def _worker_job_defer(session: Session, payload: Mapping[str, Any]) -> Any:
    """Release a proved claim into indexed durable nonterminal waiting.

    Previous holder identity is diagnostic evidence, while status and the
    zero lease revoke authority. An exact lost-ACK replay performs no write.
    """
    required = {
        "task_id",
        "worker_id",
        "fencing_token",
        "now_ms",
        "available_at_ms",
        "replay_cursor",
        "observed_cancel_sequence",
    }
    if set(payload) - {'observed_wake_sequence'} != required:
        raise StorageError("database_protocol_error", "Invalid worker defer fields")
    task_id = _required_text(payload, "task_id", 256)
    worker_id = _required_text(payload, "worker_id", 256)
    fence = _integer(payload, "fencing_token", minimum=1)
    now_ms = _job_now(payload)
    available = _integer(
        payload, "available_at_ms", minimum=now_ms, maximum=_MAX_CLOCK_MS
    )
    cursor = _integer(payload, "replay_cursor", minimum=0)
    if available - now_ms > _MAX_CLOCK_SKEW_MS:
        raise StorageError(
            "database_protocol_error", "Worker defer exceeds its scheduling bound"
        )
    session.lock_key("worker_job", task_id)
    row = _job_row(session, task_id, lock=True)
    if row is None:
        return {"ok": False, "error": "stale_fence"}
    observed_cancel = _integer(payload, "observed_cancel_sequence", minimum=0)
    observed_wake = _integer(payload, "observed_wake_sequence", minimum=0, default=0)
    if observed_wake > int(row['wake_sequence']):
        raise StorageError('database_protocol_error', 'Worker cannot acknowledge a future wakeup')
    if observed_cancel > int(row["cancel_sequence"]):
        raise StorageError(
            "database_protocol_error", "Worker cannot acknowledge a future cancellation"
        )
    effective_available = now_ms if int(row['cancel_sequence']) > observed_cancel else available
    if int(row['wake_sequence']) > observed_wake:
        effective_available = min(now_ms, int(row['available_at_ms']))
    if (
        str(row["status"]) == "suspended"
        and str(row["claim_owner"]) == worker_id
        and int(row["fencing_token"]) == fence
        and int(row["available_at_ms"]) == effective_available
        and int(row["replay_cursor"]) >= cursor
    ):
        return {"ok": True, "replayed": True, "job": _job_document(row)}
    epoch, authority_now, _ = _lease_authority(session)
    if not _live_claim(row, worker_id, fence, epoch, authority_now):
        return {"ok": False, "error": "stale_fence"}
    count = session.execute(
        "UPDATE storage_worker_jobs SET status=?, lease_deadline_ms=0, "
        "available_at_ms=?, replay_cursor=?, updated_at_ms=? "
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        "AND fencing_token=? AND lease_authority_epoch=? AND lease_deadline_ms>?",
        (
            "suspended",
            effective_available,
            max(cursor, int(row["replay_cursor"])),
            now_ms,
            task_id,
            worker_id,
            fence,
            epoch,
            authority_now,
        ),
    )
    if not count:
        return {"ok": False, "error": "stale_fence"}
    return {
        "ok": True,
        "replayed": False,
        "job": _job_document(_job_row(session, task_id)),
    }


def _worker_job_wake_owned(session: Session, task_id: str, owner: int, tenant: str, now_ms: int):
    """Advance a domain wake and availability under the canonical job lock."""
    session.lock_key('worker_job', task_id)
    row = _job_row(session, task_id, lock=True)
    if (row is None or int(row['user_id']) != owner or row['tenant_id'] != tenant
            or row['status'] in _TERMINAL_STATUSES):
        return None
    sequence = int(row['wake_sequence']) + 1
    if sequence > _MAX_CLOCK_MS:
        raise StorageError('database_integrity', 'Worker wake sequence overflows')
    wake_at = _lease_authority(session).utc_now_ms
    session.execute('UPDATE storage_worker_jobs SET wake_sequence=?, '
                    'available_at_ms=?, updated_at_ms=? WHERE task_id=?',
                    (sequence, min(int(row['available_at_ms']), wake_at), now_ms, task_id))
    return _job_document(_job_row(session, task_id))


def _worker_job_heartbeat(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    worker_id = _required_text(payload, 'worker_id', 256)
    fence = _integer(payload, 'fencing_token', minimum=1)
    now_ms = _job_now(payload)
    lease_ms = _integer(
        payload, 'lease_ms', default=60_000, minimum=10_000, maximum=WORKER_LEASE_MAX_MS)
    replay_cursor = _integer(
        payload, 'replay_cursor', default=0, minimum=0)
    session.lock_key('worker_job', task_id)
    row = _job_row(session, task_id, lock=True)
    epoch, authority_now, _ = _lease_authority(session)
    if not _live_claim(row, worker_id, fence, epoch, authority_now):
        return {'ok': False, 'error': 'stale_fence'}
    deadline = authority_now + lease_ms
    changed = session.execute(
        'UPDATE storage_worker_jobs SET '
        'lease_deadline_ms=CASE WHEN lease_deadline_ms > ? '
        'THEN lease_deadline_ms ELSE ? END, '
        'heartbeat_at_ms=CASE WHEN heartbeat_at_ms > ? '
        'THEN heartbeat_at_ms ELSE ? END, '
        'replay_cursor=CASE WHEN replay_cursor > ? '
        'THEN replay_cursor ELSE ? END, updated_at_ms=? '
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        'AND fencing_token=? AND lease_authority_epoch=? AND lease_deadline_ms>?',
        (
            deadline, deadline, authority_now, authority_now, replay_cursor, replay_cursor,
            now_ms, task_id, worker_id, fence, epoch, authority_now,
        ),
    )
    if changed != 1:
        return {'ok': False, 'error': 'stale_fence'}
    row = _job_row(session, task_id)
    return {'ok': True, 'job': _job_document(row)}


def _worker_job_claim_state(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    worker_id = _required_text(payload, 'worker_id', 256)
    fence = _integer(payload, 'fencing_token', minimum=1)
    _job_now(payload)
    row = _job_row(session, task_id)
    epoch, authority_now, _ = _lease_authority(session)
    if not _live_claim(row, worker_id, fence, epoch, authority_now):
        return {'ok': False, 'error': 'stale_fence'}
    job = _job_document(row)
    return {
        'ok': True,
        'cancelSequence': job['cancelSequence'],
        'cancelRequestedAtMs': job['cancelRequestedAtMs'],
        'cancelReason': job['cancelReason'],
        'leaseDeadlineMs': job['leaseDeadlineMs'],
        'replayCursor': job['replayCursor'],
    }


def _worker_job_request_cancel(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    task_id = _required_text(payload, "task_id", 256)
    user_id = _job_owner(payload)
    now_ms = _job_now(payload)
    reason = _optional_text(payload, "reason", 1000)
    session.lock_key("worker_job", task_id)
    row = session.fetch_one_for_update(
        f"SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs "
        "WHERE task_id=? AND user_id=?",
        (task_id, user_id),
    )
    if row is None:
        return None
    if str(row["status"]) in _TERMINAL_STATUSES:
        return {
            "accepted": False,
            "alreadyTerminal": True,
            "job": _job_document(row),
        }
    if int(row["cancel_requested_at_ms"]) > 0:
        return {
            "accepted": True,
            "alreadyRequested": True,
            "job": _job_document(row),
        }

    queued = str(row["status"]) == "queued"
    next_status = "cancelled" if queued else str(row["status"])
    terminal_at_ms = now_ms if queued else 0
    session.execute(
        "UPDATE storage_worker_jobs SET status=?, cancel_sequence=?, "
        "cancel_requested_at_ms=?, cancel_reason=?, updated_at_ms=?, "
        "terminal_at_ms=?, available_at_ms=? WHERE task_id=? AND user_id=?",
        (
            next_status,
            int(row["cancel_sequence"]) + 1,
            now_ms,
            reason,
            now_ms,
            terminal_at_ms,
            now_ms
            if str(row["status"]) == "suspended"
            else int(row["available_at_ms"]),
            task_id,
            user_id,
        ),
    )
    updated = _job_row(session, task_id)
    return {"accepted": True, "alreadyRequested": False, "job": _job_document(updated)}


def _worker_job_complete(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    worker_id = _required_text(payload, 'worker_id', 256)
    fence = _integer(payload, 'fencing_token', minimum=1)
    now_ms = _job_now(payload)
    terminal_status = _required_text(payload, 'terminal_status', 32)
    if terminal_status not in _TERMINAL_STATUSES:
        raise StorageError(
            'database_protocol_error',
            'Invalid terminal_status in storage request',
        )
    result_ref = _optional_text(payload, 'result_ref', 1024)
    replay_cursor = _integer(
        payload, 'replay_cursor', default=0, minimum=0)
    error = payload.get('error', {})
    if not isinstance(error, Mapping):
        raise StorageError(
            'database_protocol_error', 'Invalid worker job error document')
    encoded_error = _dump(dict(error))
    if len(encoded_error) > _MAX_ERROR_BYTES:
        raise StorageError(
            'storage_payload_too_large',
            f'Worker job error exceeds {_MAX_ERROR_BYTES} bytes',
        )
    session.lock_key('worker_job', task_id)
    row = _job_row(session, task_id, lock=True)
    epoch, authority_now, _ = _lease_authority(session)
    if not _live_claim(row, worker_id, fence, epoch, authority_now):
        return {'ok': False, 'error': 'stale_fence_or_cancelled'}
    changed = session.execute(
        'UPDATE storage_worker_jobs SET status=?, result_ref=?, error_json=?, '
        'replay_cursor=CASE WHEN replay_cursor > ? '
        'THEN replay_cursor ELSE ? END, lease_deadline_ms=0, '
        'updated_at_ms=?, terminal_at_ms=? '
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        'AND fencing_token=? AND lease_authority_epoch=? AND lease_deadline_ms>? '
        "AND (cancel_requested_at_ms=0 OR ?='cancelled')",
        (
            terminal_status, result_ref, encoded_error, replay_cursor,
            replay_cursor, now_ms, now_ms, task_id, worker_id, fence, epoch, authority_now,
            terminal_status,
        ),
    )
    if changed != 1:
        return {'ok': False, 'error': 'stale_fence_or_cancelled'}
    row = _job_row(session, task_id)
    return {'ok': True, 'job': _job_document(row)}


__all__ = [
    "_MAX_JOB_PAYLOAD_BYTES",
    "_WORKER_JOB_COLUMNS",
    "_worker_job_claim_next",
    "_worker_job_claim_state",
    "_worker_job_complete",
    "_worker_job_defer",
    "_worker_job_enqueue",
    "_worker_job_get",
    "_worker_job_heartbeat",
    "_worker_job_request_cancel",
]

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
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


_WORKER_JOB_COLUMNS = (
    'task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, '
    'request_digest, status, priority, available_at_ms, claim_owner, '
    'lease_deadline_ms, fencing_token, attempt_no, heartbeat_at_ms, '
    'cancel_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, '
    'result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms'
)
_TERMINAL_STATUSES = frozenset({'succeeded', 'failed', 'cancelled'})
_MAX_JOB_PAYLOAD_BYTES = 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024
_MAX_CLOCK_MS = 9_223_372_036_854_775_000
# Fencing skew bound: ``now_ms`` feeds ``available_at_ms`` and
# ``lease_deadline_ms``, so a caller clock far ahead of the authority would
# strand a job past every reaper horizon.  A far-behind clock merely makes
# leases immediately due, so the bound is one-sided toward the future.
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
    """Return a client wall clock that cannot strand a durable job.

    ``now_ms`` drives ``available_at_ms`` and ``lease_deadline_ms``.  A clock
    far in the future would make a queued job unclaimable or a running job
    unreapable, so reject it instead of silently fencing the job forever.
    """
    client_now = _integer(
        payload, 'now_ms', minimum=0, maximum=_MAX_CLOCK_MS)
    server_now = int(time.time() * 1000)
    if client_now - server_now > _MAX_CLOCK_SKEW_MS:
        raise StorageError(
            'database_protocol_error',
            'Worker job clock is too far ahead of the server clock',
        )
    return client_now


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
        'cancelRequestedAtMs': int(row['cancel_requested_at_ms']),
        'cancelReason': str(row['cancel_reason'] or ''),
        'replayCursor': int(row['replay_cursor']),
        'resultRef': str(row['result_ref'] or ''),
        'error': _load(row['error_json']) or {},
        'createdAtMs': int(row['created_at_ms']),
        'updatedAtMs': int(row['updated_at_ms']),
        'terminalAtMs': int(row['terminal_at_ms']),
    }


def _job_row(session: Session, task_id: str) -> Mapping[str, Any] | None:
    return session.fetch_one(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE task_id = ?',
        (task_id,),
    )


def _worker_job_get(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    user_id = _job_owner(payload)
    row = session.fetch_one(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE task_id = ? AND user_id = ?',
        (task_id, user_id),
    )
    return None if row is None else _job_document(row)


def _worker_job_enqueue(session: Session, payload: Mapping[str, Any]) -> Any:
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
    session: Session,
    *,
    now_ms: int,
    kind_sql: str,
    kind_params: tuple[Any, ...],
) -> Mapping[str, Any] | None:
    # Recover an expired accepted task before taking fresh work.  This keeps
    # crash takeover bounded even while the queue receives a steady stream.
    expired = session.fetch_one_for_update_skip_locked(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        "WHERE status = 'running' AND lease_deadline_ms <= ?"
        f'{kind_sql} ORDER BY lease_deadline_ms, priority, created_at_ms, '
        'task_id LIMIT 1',
        (now_ms, *kind_params),
    )
    if expired is not None:
        return expired
    return session.fetch_one_for_update_skip_locked(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        "WHERE status = 'queued' AND available_at_ms <= ?"
        f'{kind_sql} ORDER BY priority, available_at_ms, created_at_ms, '
        'task_id LIMIT 1',
        (now_ms, *kind_params),
    )


def _worker_job_claim_next(session: Session, payload: Mapping[str, Any]) -> Any:
    worker_id = _required_text(payload, 'worker_id', 256)
    now_ms = _job_now(payload)
    lease_ms = _integer(
        payload, 'lease_ms', default=60_000, minimum=10_000, maximum=300_000)
    kind_sql, kind_params = _claim_kind_filter(payload)
    row = _claim_candidate(
        session, now_ms=now_ms, kind_sql=kind_sql, kind_params=kind_params)
    if row is None:
        return None

    task_id = str(row['task_id'])
    old_fence = int(row['fencing_token'])
    new_fence = old_fence + 1
    changed = session.execute(
        "UPDATE storage_worker_jobs SET status='running', claim_owner=?, "
        'lease_deadline_ms=?, fencing_token=?, attempt_no=attempt_no+1, '
        'heartbeat_at_ms=?, updated_at_ms=? '
        'WHERE task_id=? AND fencing_token=?',
        (
            worker_id, now_ms + lease_ms, new_fence, now_ms, now_ms,
            task_id, old_fence,
        ),
    )
    if changed != 1:  # pragma: no cover - row lock / serialized writer invariant
        raise StorageError('database_conflict', 'Worker job claim lost its fence')
    claimed = _job_row(session, task_id)
    if claimed is None:  # pragma: no cover - transaction invariant
        raise StorageError('database_internal', 'Claimed worker job disappeared')
    return _job_document(claimed)


def _worker_job_heartbeat(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    worker_id = _required_text(payload, 'worker_id', 256)
    fence = _integer(payload, 'fencing_token', minimum=1)
    now_ms = _job_now(payload)
    lease_ms = _integer(
        payload, 'lease_ms', default=60_000, minimum=10_000, maximum=300_000)
    replay_cursor = _integer(
        payload, 'replay_cursor', default=0, minimum=0)
    deadline = now_ms + lease_ms
    changed = session.execute(
        'UPDATE storage_worker_jobs SET '
        'lease_deadline_ms=CASE WHEN lease_deadline_ms > ? '
        'THEN lease_deadline_ms ELSE ? END, '
        'heartbeat_at_ms=CASE WHEN heartbeat_at_ms > ? '
        'THEN heartbeat_at_ms ELSE ? END, '
        'replay_cursor=CASE WHEN replay_cursor > ? '
        'THEN replay_cursor ELSE ? END, updated_at_ms=? '
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        'AND fencing_token=? AND lease_deadline_ms>?',
        (
            deadline, deadline, now_ms, now_ms, replay_cursor, replay_cursor,
            now_ms, task_id, worker_id, fence, now_ms,
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
    now_ms = _job_now(payload)
    row = session.fetch_one(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        'AND fencing_token=? AND lease_deadline_ms>?',
        (task_id, worker_id, fence, now_ms),
    )
    if row is None:
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
    session: Session, payload: Mapping[str, Any],
) -> Any:
    task_id = _required_text(payload, 'task_id', 256)
    user_id = _job_owner(payload)
    now_ms = _job_now(payload)
    reason = _optional_text(payload, 'reason', 1000)
    session.lock_key('worker_job', task_id)
    row = session.fetch_one(
        f'SELECT {_WORKER_JOB_COLUMNS} FROM storage_worker_jobs '
        'WHERE task_id=? AND user_id=?',
        (task_id, user_id),
    )
    if row is None:
        return None
    if str(row['status']) in _TERMINAL_STATUSES:
        return {
            'accepted': False,
            'alreadyTerminal': True,
            'job': _job_document(row),
        }
    if int(row['cancel_requested_at_ms']) > 0:
        return {
            'accepted': True,
            'alreadyRequested': True,
            'job': _job_document(row),
        }

    queued = str(row['status']) == 'queued'
    next_status = 'cancelled' if queued else 'running'
    terminal_at_ms = now_ms if queued else 0
    session.execute(
        'UPDATE storage_worker_jobs SET status=?, cancel_sequence=?, '
        'cancel_requested_at_ms=?, cancel_reason=?, updated_at_ms=?, '
        'terminal_at_ms=? WHERE task_id=? AND user_id=?',
        (
            next_status, int(row['cancel_sequence']) + 1, now_ms, reason,
            now_ms, terminal_at_ms, task_id, user_id,
        ),
    )
    updated = _job_row(session, task_id)
    return {'accepted': True, 'alreadyRequested': False,
            'job': _job_document(updated)}


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
    changed = session.execute(
        'UPDATE storage_worker_jobs SET status=?, result_ref=?, error_json=?, '
        'replay_cursor=CASE WHEN replay_cursor > ? '
        'THEN replay_cursor ELSE ? END, lease_deadline_ms=0, '
        'updated_at_ms=?, terminal_at_ms=? '
        "WHERE task_id=? AND status='running' AND claim_owner=? "
        'AND fencing_token=? AND lease_deadline_ms>? '
        "AND (cancel_requested_at_ms=0 OR ?='cancelled')",
        (
            terminal_status, result_ref, encoded_error, replay_cursor,
            replay_cursor, now_ms, now_ms, task_id, worker_id, fence, now_ms,
            terminal_status,
        ),
    )
    if changed != 1:
        return {'ok': False, 'error': 'stale_fence_or_cancelled'}
    row = _job_row(session, task_id)
    return {'ok': True, 'job': _job_document(row)}


__all__ = [
    '_MAX_JOB_PAYLOAD_BYTES',
    '_WORKER_JOB_COLUMNS',
    '_worker_job_claim_next',
    '_worker_job_claim_state',
    '_worker_job_complete',
    '_worker_job_enqueue',
    '_worker_job_get',
    '_worker_job_heartbeat',
    '_worker_job_request_cancel',
]

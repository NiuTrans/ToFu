"""Atomic conversation-attempt dispatch into durable worker authority.

This storage operation is the crash boundary between an accepted conversation
attempt and a claimable worker job.  It deliberately persists only durable
references plus the authenticated principal; an executor reconstructs model
context from turn authority after claiming the job.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _integer, _required_text
from lib.storage_sidecar.operations_pkg._turns import (
    _attempt_public,
    _turn_attempt_bind,
)
from lib.storage_sidecar.operations_pkg._worker_jobs import (
    _job_row,
    _worker_job_enqueue,
)


_CONVERSATION_ATTEMPT_JOB_CONTRACT = 'tofu.conversation-attempt-job/v1'
_CONVERSATION_ATTEMPT_JOB_KIND = 'conversation-attempt'
_MAX_CLOCK_MS = 9_223_372_036_854_775_000


def _dispatch_principal(
    payload: Mapping[str, Any], *, owner_user_id: int,
) -> PrincipalContext:
    raw_principal = payload.get('principal')
    if not isinstance(raw_principal, Mapping):
        raise StorageError(
            'database_protocol_error',
            'Conversation worker dispatch requires an explicit principal',
        )
    try:
        principal = PrincipalContext.from_payload(raw_principal)
        principal_owner = principal.require_owner(
            context='conversation worker dispatch')
    except (PermissionError, TypeError, ValueError) as exc:
        raise StorageError(
            'database_protocol_error',
            'Conversation worker dispatch principal is invalid',
        ) from exc
    if dict(raw_principal) != principal.to_payload():
        raise StorageError(
            'database_protocol_error',
            'Conversation worker dispatch principal is not canonical',
        )
    if principal_owner != owner_user_id:
        raise StorageError(
            'database_conflict',
            'Conversation worker dispatch principal does not own the attempt',
        )
    return principal


def _conversation_attempt_job_payload(
    *,
    attempt: Mapping[str, Any],
    principal: PrincipalContext,
) -> dict[str, Any]:
    """Build the bounded reference carrier; no conversation projection lives here."""
    return {
        'contract': _CONVERSATION_ATTEMPT_JOB_CONTRACT,
        'conversationId': str(attempt['conversation_id']),
        'turnId': str(attempt['turn_id']),
        'attemptId': str(attempt['attempt_id']),
        'principal': principal.to_payload(),
        'baseProjectionRevision': int(
            attempt['base_projection_revision'] or 0),
        'operation': str(attempt['operation']),
    }


def _turn_attempt_dispatch_worker(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Atomically bind one accepted attempt and enqueue its durable job.

    A lost ACK is a semantic replay: the deterministic task/idempotency keys
    resolve the already-bound job.  Any mismatched legacy/in-process binding
    fails closed instead of creating a second executor authority.
    """
    attempt_id = _required_text(payload, 'attempt_id', 128)
    user_id = _integer(payload, 'user_id', minimum=1)
    now_ms = _integer(
        payload, 'now_ms', minimum=0, maximum=_MAX_CLOCK_MS)
    priority = _integer(
        payload, 'priority', default=100, minimum=0, maximum=1000)
    task_id = f'conversation-attempt:{attempt_id}'
    idempotency_key = task_id

    session.lock_key('attempt_dispatch', attempt_id)
    attempt = session.fetch_one(
        'SELECT a.* FROM storage_generation_attempts AS a '
        'JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id '
        'WHERE a.attempt_id=? AND t.user_id=?',
        (attempt_id, user_id),
    )
    if attempt is None:
        return None
    turn = session.fetch_one(
        'SELECT * FROM storage_conversation_turns WHERE turn_id=? AND user_id=?',
        (attempt['turn_id'], user_id),
    )
    if turn is None:
        return None

    principal = _dispatch_principal(payload, owner_user_id=user_id)
    job_payload = _conversation_attempt_job_payload(
        attempt=attempt, principal=principal)
    enqueue_payload = {
        'task_id': task_id,
        'user_id': user_id,
        'tenant_id': principal.tenant_id or '',
        'task_kind': _CONVERSATION_ATTEMPT_JOB_KIND,
        'idempotency_key': idempotency_key,
        'payload': job_payload,
        'priority': priority,
        'available_at_ms': now_ms,
        'now_ms': now_ms,
    }

    existing_task_id = str(attempt['task_id'] or '')
    if existing_task_id:
        if existing_task_id != task_id:
            raise StorageError(
                'database_conflict',
                'Conversation attempt is already bound to another executor',
            )
        # Replays may validate but never recreate a missing job behind an
        # already-running attempt: that state could mean an in-process worker
        # still owns billable or externally visible side effects.
        if _job_row(session, task_id) is None:
            raise StorageError(
                'database_conflict',
                'Conversation attempt binding has no durable worker job',
            )
        replay = _worker_job_enqueue(session, enqueue_payload)
        return {
            'created': False,
            'idempotentReplay': True,
            'attempt': _attempt_public(attempt),
            'job': replay['job'],
        }

    if (str(attempt['status']) != 'pending'
            or str(turn['current_attempt_id'] or '') != attempt_id):
        raise StorageError(
            'database_conflict',
            'Conversation attempt is not dispatchable',
        )

    enqueued = _worker_job_enqueue(session, enqueue_payload)
    bound = _turn_attempt_bind(session, {
        'attempt_id': attempt_id,
        'task_id': task_id,
        'user_id': user_id,
    })
    if not isinstance(bound, Mapping):
        raise StorageError(
            'database_conflict',
            'Conversation attempt disappeared during worker dispatch',
        )
    public_attempt = dict(bound)
    attempt_events = public_attempt.pop('_conversationSyncAttemptEvents', [])
    return {
        'created': bool(enqueued['created']),
        'attempt': public_attempt,
        'job': enqueued['job'],
        '_conversationSyncAttemptEvents': attempt_events,
    }


__all__ = [
    '_CONVERSATION_ATTEMPT_JOB_CONTRACT',
    '_CONVERSATION_ATTEMPT_JOB_KIND',
    '_turn_attempt_dispatch_worker',
]

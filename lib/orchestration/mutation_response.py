"""HTTP projection for canonical orchestration mutation results."""

from __future__ import annotations

from lib.orchestration.mutation_result import (
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    OrchestrationMutationResult,
)


_HTTP_STATUS_BY_REASON = {
    MUTATION_NOT_FOUND: 404,
    MUTATION_TERMINAL: 409,
    MUTATION_ACTIVE: 409,
    MUTATION_CONFLICT: 409,
    MUTATION_PERSISTENCE_FAILED: 500,
}

_ERROR_MESSAGES = {
    (MUTATION_ACTION_ABORT_RUN, MUTATION_NOT_FOUND): 'Run not found',
    (MUTATION_ACTION_ABORT_RUN, MUTATION_TERMINAL):
        'Run is already terminal',
    (MUTATION_ACTION_ABORT_RUN, MUTATION_CONFLICT):
        'Run status changed before abort could be recorded',
    (MUTATION_ACTION_ABORT_RUN, MUTATION_PERSISTENCE_FAILED):
        'Failed to record orchestration run abort',
    (MUTATION_ACTION_DELETE_RUN, MUTATION_NOT_FOUND): 'Run not found',
    (MUTATION_ACTION_DELETE_RUN, MUTATION_ACTIVE):
        'Active run must be aborted before deletion',
    (MUTATION_ACTION_DELETE_RUN, MUTATION_PERSISTENCE_FAILED):
        'Failed to delete orchestration run',
    (MUTATION_ACTION_APPROVE_GATE, MUTATION_NOT_FOUND):
        'Approval request not found or expired',
    (MUTATION_ACTION_INPUT_GATE, MUTATION_NOT_FOUND):
        'Input request not found or expired',
}


def mutation_reason_http_status(reason: str) -> int:
    return _HTTP_STATUS_BY_REASON.get(reason, 409)


def mutation_http_status(result: OrchestrationMutationResult) -> int:
    return 200 if result.ok else mutation_reason_http_status(
        result.canonical_reason)


def mutation_error_message(result: OrchestrationMutationResult) -> str:
    if result.ok:
        return ''
    return _ERROR_MESSAGES.get(
        (result.action, result.canonical_reason),
        'Orchestration state changed before the action completed',
    )


def mutation_response(
    result: OrchestrationMutationResult,
) -> tuple[dict, int]:
    """Project the canonical mutation result and HTTP status."""
    payload = {
        'ok': bool(result.ok),
        'mutation': result.payload(),
    }
    if not result.ok:
        payload['error'] = mutation_error_message(result)
    return payload, mutation_http_status(result)


__all__ = [
    'mutation_error_message',
    'mutation_http_status',
    'mutation_reason_http_status',
    'mutation_response',
]

"""Owner-bound blocking confirmation for write-capable task tools."""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.human_gate_registry import (
    GATE_WRITE_APPROVAL,
    human_gate_registry,
)


logger = get_logger(__name__)


def request_write_approval(
    approval_id: str,
    timeout: int = 120,
    *,
    owner_user_id: int,
) -> bool:
    """Block until this owner approves/rejects; return True if approved."""
    logger.info('[Approval] Request %s waiting (timeout=%ds)',
                approval_id, timeout)
    entry = human_gate_registry.register(
        GATE_WRITE_APPROVAL,
        approval_id,
        owner_user_id=owner_user_id,
    )
    if entry is None:
        logger.error('[Approval] duplicate or capacity-exhausted request '
                     'approval_id=%s; refusing registration', approval_id)
        return False

    entry.event.wait(timeout=timeout)
    resolution = human_gate_registry.take(
        GATE_WRITE_APPROVAL, approval_id, entry)
    approved = bool(resolution.response) if resolution.resolved else False
    if resolution.resolved:
        logger.info('[Approval] Resolved %s → approved=%s',
                    approval_id, approved)
    else:
        logger.warning('[Approval] Request %s timed out after %ds',
                       approval_id, timeout)
    return approved


def resolve_write_approval(
    approval_id: str,
    approved: bool,
    *,
    owner_user_id: int,
) -> bool:
    """Resolve one pending approval owned by the authenticated user."""
    if not isinstance(approved, bool):
        raise ValueError('approved must be a boolean')
    resolved = human_gate_registry.resolve(
        GATE_WRITE_APPROVAL,
        approval_id,
        owner_user_id=owner_user_id,
        response=approved,
    )
    if not resolved:
        logger.warning('[Approval] resolve called for unknown, foreign, or '
                       'already resolved approval_id=%s', approval_id)
        return False
    logger.info('[Approval] User resolved %s → approved=%s',
                approval_id, approved)
    return True


def is_write_approval_pending(
    approval_id: str,
    *,
    owner_user_id: int,
) -> bool:
    """Return whether this owner currently has the named pending gate."""
    return human_gate_registry.is_pending(
        GATE_WRITE_APPROVAL,
        approval_id,
        owner_user_id=owner_user_id,
    )


__all__ = [
    'is_write_approval_pending',
    'request_write_approval',
    'resolve_write_approval',
]

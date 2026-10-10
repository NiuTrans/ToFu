"""Owner-bound blocking confirmation for write-capable task tools."""

from __future__ import annotations

import time
from collections.abc import Callable

from lib.log import get_logger
from lib.tasks_pkg.human_gate_registry import (
    GATE_WRITE_APPROVAL,
    human_gate_registry,
)


logger = get_logger(__name__)

_STORE_POLL_INTERVAL = 2.0


def request_write_approval(
    approval_id: str,
    timeout: int = 120,
    *,
    owner_user_id: int,
    task=None,
    on_registered: Callable[[], None] | None = None,
) -> bool:
    """Register, publish via on_registered, then wait for the owner decision.

    The callback is a required publication step, not a best-effort observer:
    failure propagates after releasing the registration.
    """
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

    # Keep cross-replica resolution and cancellation bounded by the same
    # existing poll interval. No additional thread or timer is needed.
    cancelled = False
    try:
        if on_registered is not None:
            on_registered()
        deadline = time.monotonic() + timeout
        while True:
            if task is not None:
                if task.get('aborted'):
                    cancelled = True
                    break
                try:
                    task['_dispatch_heartbeat'] = time.time()
                except (TypeError, AttributeError):
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if entry.event.wait(timeout=min(_STORE_POLL_INTERVAL, remaining)):
                break
            if human_gate_registry.poll_store_resolution(
                GATE_WRITE_APPROVAL, approval_id, entry,
            ):
                break
    finally:
        # Even a failed abort probe/wait must release this exact registration.
        # take is atomic with resolve and cannot remove a replacement waiter.
        resolution = human_gate_registry.take(
            GATE_WRITE_APPROVAL, approval_id, entry)
    approved = bool(resolution.response) if resolution.resolved else False
    if resolution.resolved:
        logger.info('[Approval] Resolved %s → approved=%s',
                    approval_id, approved)
    elif cancelled:
        logger.info('[Approval] Request %s cancelled', approval_id)
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

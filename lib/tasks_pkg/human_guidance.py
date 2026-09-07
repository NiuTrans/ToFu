"""Owner-bound blocking wait for a task's human guidance response.

The LLM can call ``ask_human`` to pose a free-text or choice question. The
task thread waits indefinitely, with abort heartbeats, while the shared human
gate registry owns authorization and first-resolution-wins concurrency.
"""

from __future__ import annotations

import time

from lib.human_gate_contract import MAX_HUMAN_GATE_RESPONSE_LENGTH
from lib.log import get_logger
from lib.tasks_pkg.human_gate_registry import (
    GATE_GUIDANCE,
    human_gate_registry,
)


logger = get_logger(__name__)
_ABORT_POLL_INTERVAL = 2.0


def _request_owner(task, owner_user_id):
    if owner_user_id is not None:
        from lib.identity import require_user_id
        return require_user_id(
            owner_user_id, context='human guidance request owner')
    from lib.tasks_pkg.manager import task_user_id
    return task_user_id(task)


def request_human_guidance(
    guidance_id: str,
    task=None,
    *,
    owner_user_id: int | None = None,
) -> str | None:
    """Wait until this owner responds, the task aborts, or it is cancelled."""
    owner = _request_owner(task, owner_user_id)
    logger.info('[HumanGuidance] Request %s blocking (no timeout, '
                'abort_poll=%.1fs, task=%s)', guidance_id,
                _ABORT_POLL_INTERVAL,
                task.get('id', '?')[:8] if task else 'none')
    entry = human_gate_registry.register(
        GATE_GUIDANCE,
        guidance_id,
        owner_user_id=owner,
    )
    if entry is None:
        logger.error('[HumanGuidance] duplicate or capacity-exhausted request '
                     'guidance_id=%s; refusing registration', guidance_id)
        return None

    while True:
        if entry.event.wait(timeout=_ABORT_POLL_INTERVAL):
            break
        # A task blocked on a human is alive, not wedged. Keep the reaper's
        # positive-liveness clock fresh while retaining the abort poll.
        if task is not None:
            try:
                task['_dispatch_heartbeat'] = time.time()
            except (TypeError, AttributeError):
                pass
        if task and task.get('aborted'):
            # If a response acquired the registry lock at this boundary it
            # wins; otherwise atomically discard the abandoned waiter.
            if not human_gate_registry.discard_unresolved(
                GATE_GUIDANCE, guidance_id, entry,
            ):
                continue
            logger.info('[HumanGuidance] Request %s — task aborted, '
                        'unblocking thread', guidance_id)
            return None

    resolution = human_gate_registry.take(
        GATE_GUIDANCE, guidance_id, entry)
    response = resolution.response if resolution.resolved else None
    logger.info('[HumanGuidance] Resolved %s → response_len=%d',
                guidance_id, len(response) if isinstance(response, str) else 0)
    return response if isinstance(response, str) else None


def cancel_human_guidance(
    guidance_id: str,
    *,
    owner_user_id: int,
) -> bool:
    """Cancel this owner's pending guidance request."""
    resolved = human_gate_registry.resolve(
        GATE_GUIDANCE,
        guidance_id,
        owner_user_id=owner_user_id,
        response=None,
    )
    if resolved:
        logger.info('[HumanGuidance] Cancelled guidance_id=%s', guidance_id)
    return resolved


def resolve_human_guidance(
    guidance_id: str,
    response_text: str,
    *,
    owner_user_id: int,
) -> bool:
    """Resolve this owner's pending human-guidance request."""
    if not isinstance(response_text, str):
        raise ValueError('human guidance response must be a string')
    if not response_text.strip():
        raise ValueError('human guidance response is required')
    if len(response_text) > MAX_HUMAN_GATE_RESPONSE_LENGTH:
        raise ValueError(
            'human guidance response exceeds '
            f'{MAX_HUMAN_GATE_RESPONSE_LENGTH} characters')
    resolved = human_gate_registry.resolve(
        GATE_GUIDANCE,
        guidance_id,
        owner_user_id=owner_user_id,
        response=response_text,
    )
    if not resolved:
        logger.warning('[HumanGuidance] resolve called for unknown, foreign, '
                       'or already resolved guidance_id=%s', guidance_id)
        return False
    logger.info('[HumanGuidance] User resolved %s → response_len=%d',
                guidance_id, len(response_text))
    return True


def is_human_guidance_pending(
    guidance_id: str,
    *,
    owner_user_id: int,
) -> bool:
    return human_gate_registry.is_pending(
        GATE_GUIDANCE,
        guidance_id,
        owner_user_id=owner_user_id,
    )


__all__ = [
    'cancel_human_guidance',
    'is_human_guidance_pending',
    'request_human_guidance',
    'resolve_human_guidance',
]

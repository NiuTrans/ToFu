"""Owner-bound blocking wait for interactive subprocess stdin.

The wait policy is stdin-specific (a finite timeout), while registration,
authorization, capacity, and first-resolution-wins behavior come from the
shared human-gate registry.
"""

from __future__ import annotations

import time

from lib.human_gate_contract import MAX_HUMAN_GATE_RESPONSE_LENGTH
from lib.log import get_logger
from lib.tasks_pkg.human_gate_registry import GATE_STDIN, human_gate_registry


logger = get_logger(__name__)

# TODO(enterprise, R3): Events are process-local, so cross-replica response
# routing needs a durable request/response lease before horizontal execution.
_ABORT_POLL_INTERVAL = 1.0
_STDIN_TIMEOUT = 120.0


def _request_owner(task, owner_user_id):
    if owner_user_id is not None:
        from lib.identity import require_user_id
        return require_user_id(owner_user_id, context='stdin request owner')
    from lib.tasks_pkg.manager import task_user_id
    return task_user_id(task)


def request_stdin(
    stdin_id: str,
    task=None,
    *,
    owner_user_id: int | None = None,
) -> str | None:
    """Wait for this owner's stdin text, EOF, abort, or timeout."""
    owner = _request_owner(task, owner_user_id)
    logger.info('[StdinHandler] Request %s blocking '
                '(abort_poll=%.1fs, timeout=%.0fs, task=%s)',
                stdin_id, _ABORT_POLL_INTERVAL, _STDIN_TIMEOUT,
                task.get('id', '?')[:8] if task else 'none')
    entry = human_gate_registry.register(
        GATE_STDIN,
        stdin_id,
        owner_user_id=owner,
    )
    if entry is None:
        logger.error('[StdinHandler] duplicate or capacity-exhausted request '
                     'stdin_id=%s; refusing registration', stdin_id)
        return None

    deadline = time.monotonic() + _STDIN_TIMEOUT
    while True:
        if entry.event.wait(timeout=_ABORT_POLL_INTERVAL):
            break
        if task and task.get('aborted'):
            reason = 'task aborted'
        elif time.monotonic() >= deadline:
            reason = f'timed out after {_STDIN_TIMEOUT:.0f}s'
        else:
            continue
        if not human_gate_registry.discard_unresolved(
            GATE_STDIN, stdin_id, entry,
        ):
            continue
        logger.info('[StdinHandler] Request %s — %s, closing stdin',
                    stdin_id, reason)
        return None

    resolution = human_gate_registry.take(GATE_STDIN, stdin_id, entry)
    response = resolution.response if resolution.resolved else None
    logger.info('[StdinHandler] Resolved %s → response_len=%d',
                stdin_id, len(response) if isinstance(response, str) else 0)
    return response if isinstance(response, str) else None


def resolve_stdin(
    stdin_id: str,
    input_text: str | None,
    *,
    owner_user_id: int,
) -> bool:
    """Resolve this owner's pending stdin request; None means EOF."""
    if input_text is not None and not isinstance(input_text, str):
        raise ValueError('stdin input must be a string or null EOF')
    if (isinstance(input_text, str)
            and len(input_text) > MAX_HUMAN_GATE_RESPONSE_LENGTH):
        raise ValueError(
            f'stdin input exceeds {MAX_HUMAN_GATE_RESPONSE_LENGTH} characters')
    resolved = human_gate_registry.resolve(
        GATE_STDIN,
        stdin_id,
        owner_user_id=owner_user_id,
        response=input_text,
    )
    if not resolved:
        logger.warning('[StdinHandler] resolve called for unknown, foreign, '
                       'or already resolved stdin_id=%s', stdin_id)
        return False
    logger.info('[StdinHandler] User resolved %s → input_len=%s',
                stdin_id,
                len(input_text) if input_text is not None else 'EOF')
    return True


def cancel_stdin(stdin_id: str, *, owner_user_id: int) -> bool:
    """Cancel this owner's pending stdin request."""
    resolved = human_gate_registry.resolve(
        GATE_STDIN,
        stdin_id,
        owner_user_id=owner_user_id,
        response=None,
    )
    if resolved:
        logger.info('[StdinHandler] Cancelled stdin_id=%s', stdin_id)
    return resolved


def is_stdin_pending(stdin_id: str, *, owner_user_id: int) -> bool:
    return human_gate_registry.is_pending(
        GATE_STDIN,
        stdin_id,
        owner_user_id=owner_user_id,
    )


__all__ = [
    'cancel_stdin',
    'is_stdin_pending',
    'request_stdin',
    'resolve_stdin',
]

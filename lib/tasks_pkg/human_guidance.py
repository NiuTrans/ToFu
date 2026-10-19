"""Owner-bound blocking wait for a task's human guidance response.

The LLM can call ``ask_human`` to pose a free-text or choice question. The
task thread waits indefinitely, with abort heartbeats, while the shared human
gate registry owns authorization and first-resolution-wins concurrency.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from lib.human_gate_contract import MAX_HUMAN_GATE_RESPONSE_LENGTH
from lib.log import get_logger
from lib.tasks_pkg.human_gate_registry import (
    GATE_GUIDANCE,
    human_gate_registry,
)


logger = get_logger(__name__)
_ABORT_POLL_INTERVAL = 2.0

#: Free-text replies that mean "proceed as-is" at an interactive review gate
#: (outline / storyboard). Everything else is treated as a change request.
#: Kept on the shared wait's owner so every capability gates on ONE
#: vocabulary instead of growing per-recipe dialects.
APPROVAL_REPLIES = frozenset({
    '继续', '好', '好的', '可以', '行', '没问题', '开始', '继续生成', '通过',
    'ok', 'OK', 'okay', 'go', 'go ahead', 'looks good', 'lgtm', 'yes',
    'proceed', 'continue', 'approve', 'approved',
})


def _request_owner(task, owner_user_id):
    if owner_user_id is not None:
        from lib.identity import require_user_id
        return require_user_id(
            owner_user_id, context='human guidance request owner')
    from lib.tasks_pkg.manager import task_user_id
    return task_user_id(task)


def _durable_guidance_response(task, guidance_id: str, owner: int) -> str | None:
    if not task or not task.get('convId') or not task.get('_turnId'):
        return None
    from lib.turn_lifecycle import get_turn
    turn = get_turn(task['convId'], task['_turnId'], user_id=owner)
    for question in (turn.get('projection') or {}).get('toolRounds', []):
        if (isinstance(question, dict) and question.get('guidanceId') == guidance_id
                and question.get('guidanceAnswerCommandId')
                and isinstance(question.get('guidanceResponse'), str)):
            return question['guidanceResponse']
    return None


def request_human_guidance(
    guidance_id: str,
    task=None,
    *,
    owner_user_id: int | None = None,
    on_registered: Callable[[], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> str | None:
    """Register before publication, then wait for an answer or cancellation.

    on_registered runs after owner-bound admission and inside cleanup. A
    production caller without a chat task supplies its own abort_check.
    """
    owner = _request_owner(task, owner_user_id)
    logger.info('[HumanGuidance] Request %s blocking (no timeout, '
                'abort_poll=%.1fs, task=%s)', guidance_id,
                _ABORT_POLL_INTERVAL,
                task.get('id', '?')[:8] if task else 'none')
    entry = human_gate_registry.register(
        GATE_GUIDANCE,
        guidance_id,
        owner_user_id=owner,
        durable_scope=(task['convId'], task['_turnId']) if task
        and task.get('convId') and task.get('_turnId') else None,
    )

    if entry is None:
        logger.error('[HumanGuidance] duplicate or capacity-exhausted request '
                     'guidance_id=%s; refusing registration', guidance_id)
        return None

    durable_response = None
    try:
        if on_registered is not None:
            on_registered()
        while True:
            try:
                durable_response = _durable_guidance_response(task, guidance_id, owner)
            except Exception:
                # Storage authority outages delay consumption, never downgrade
                # the accepted answer into a volatile response.
                logger.warning('Guidance receipt read failed', exc_info=True)
            if durable_response is not None:
                break
            if abort_check is not None and abort_check():
                break
            if task is not None:
                if task.get('aborted'):
                    break
                # Human waiting is positive liveness, including the first slice.
                try:
                    task['_dispatch_heartbeat'] = time.time()
                except (TypeError, AttributeError):
                    pass
            notified = entry.event.wait(timeout=_ABORT_POLL_INTERVAL)
            if not notified:
                notified = human_gate_registry.poll_store_resolution(GATE_GUIDANCE, guidance_id, entry)
            if notified:
                if entry.durable_scope is None:
                    break
                # A distributed flag or legacy local callback is only a wakeup.
                # Scoped consumers must re-read the authoritative receipt; never
                # return volatile text or spin on a permanently set event.
                entry.event.clear()
    finally:
        # Atomic take preserves a response that won the cancellation race;
        # exceptions also release the exact entry rather than leaking capacity.
        resolution = human_gate_registry.take(
            GATE_GUIDANCE, guidance_id, entry)
    response = durable_response if durable_response is not None else (
        resolution.response if entry.durable_scope is None and resolution.resolved else None)
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
    durably_accepted: bool = False,
) -> bool:
    """Commit scoped legacy answers too; unscoped non-chat gates keep compatibility."""
    if not isinstance(response_text, str):
        raise ValueError('human guidance response must be a string')
    if not response_text.strip():
        raise ValueError('human guidance response is required')
    if len(response_text) > MAX_HUMAN_GATE_RESPONSE_LENGTH:
        raise ValueError(
            'human guidance response exceeds '
            f'{MAX_HUMAN_GATE_RESPONSE_LENGTH} characters')
    scope = human_gate_registry.owned_durable_scope(
        GATE_GUIDANCE, guidance_id, owner_user_id=owner_user_id)
    if scope is not None and not durably_accepted:
        from lib.turn_lifecycle import accept_live_guidance_answer
        accept_live_guidance_answer(
            scope[0], scope[1], guidance_id=guidance_id, response=response_text,
            command_id='legacy-guidance:' + guidance_id[:240],
            expected_projection_revision=0, user_id=owner_user_id)
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
    'APPROVAL_REPLIES',
    'cancel_human_guidance',
    'is_human_guidance_pending',
    'request_human_guidance',
    'resolve_human_guidance',
]

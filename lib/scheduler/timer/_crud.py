"""lib/scheduler/timer/_crud.py — Timer CRUD + resume guardrail helpers.

create / cancel / force-trigger / get / list / poll-log accessors, plus the
env-tunable resume guardrail helpers (``_resume_max_age_seconds`` /
``_resume_concurrency_cap``) consulted by the resume-on-restart path.
"""

from __future__ import annotations

import os as _os
import uuid
from datetime import datetime
from typing import Any

from lib.log import get_logger

from ._state import _active_timers, _cmd_outputs_lock, _last_cmd_outputs, _timers_lock

logger = get_logger(__name__)


def _timer_client(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


# ── Boot-time resume guardrails (env-tunable) ───────────────────────────────
# A timer that is still ``active`` long after its own poll budget should have
# elapsed is, by definition, failing to make progress (e.g. its poll_count
# never advanced because a DB error swallowed the increment). Resuming such a
# zombie on every restart caused the 2026-06-26 search storm. On resume we
# auto-expire any active timer older than a generous age cap, and we cap how
# many timers a single boot will re-spawn so a leaked batch can never flood the
# poll workers again.


def _resume_max_age_seconds(timer: dict[str, Any]) -> float:
    """Max wall-clock age (seconds) before an active timer is force-expired.

    Defaults to the larger of 24h and 1.5× the timer's own theoretical poll
    budget (poll_interval × max_polls), so a legitimately long timer is never
    expired prematurely. Override the 24h floor via TOFU_TIMER_MAX_AGE_HOURS.
    """
    try:
        floor_hours = float(_os.environ.get('TOFU_TIMER_MAX_AGE_HOURS', '24'))
    except (TypeError, ValueError) as e:
        logger.debug('[Timer] TOFU_TIMER_MAX_AGE_HOURS parse failed, using default: %s', e)
        floor_hours = 24.0
    floor = max(floor_hours, 0.0) * 3600.0
    try:
        budget = float(timer.get('poll_interval') or 60) * float(timer.get('max_polls') or 0)
    except (TypeError, ValueError) as e:
        logger.debug('[Timer] timer poll-budget computation failed, using 0: %s', e)
        budget = 0.0
    return max(floor, budget * 1.5)


def _resume_concurrency_cap() -> int:
    """Max number of timers a single server boot will re-spawn (0 = unlimited)."""
    try:
        return int(_os.environ.get('TOFU_TIMER_RESUME_CAP', '20'))
    except (TypeError, ValueError) as e:
        logger.debug('[Timer] TOFU_TIMER_RESUME_CAP parse failed, using default: %s', e)
        return 20


# ═════════════════════════════════════════════════════════════════════════════
#  CRUD
# ═════════════════════════════════════════════════════════════════════════════

def create_timer(*, user_id: int,
                 conv_id: str,
                 check_instruction: str,
                 continuation_message: str,
                 poll_interval: int = 60,
                 max_polls: int = 120,
                 check_command: str = '',
                 tools_config: dict | None = None,
                 source_task_id: str = '',
                 condition_command: str = '',
                 condition_regex: str = '') -> dict[str, Any]:
    """Create a timer watcher and persist to DB.

    Args:
        conv_id: Conversation to inject the continuation into.
        check_instruction: Natural-language instruction for the LLM poll.
        continuation_message: The user message to inject when ready.
        poll_interval: Seconds between polls (minimum 10).
        max_polls: Maximum number of polls before exhaustion (0=unlimited).
        check_command: Optional shell command whose output grounds the LLM poll.
        tools_config: Tool settings for the continuation task.
        source_task_id: The task that created this timer.
        condition_command: Optional pure-code PREDICATE command. When set the
            timer runs the predicate-promotion paradigm: with an instruction it
            starts 'hybrid' (LLM authoritative + reconcile the predicate, then
            auto-promote to 'code'); alone it starts 'code' (zero-LLM). Derived,
            not caller-specified: see ``derive_condition_kind``.
        condition_regex: Optional regex over the predicate's stdout; empty →
            use the exit code (0=ready) per the Unix contract.
    Returns:
        Timer record dict.
    """
    from lib.scheduler._shared import derive_condition_kind

    timer_id = 'tmr_' + str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    condition_kind = derive_condition_kind(check_instruction, condition_command)

    # ── Defensive coercion: LLM tool-calls sometimes arrive with
    #    string-valued numeric args (e.g. "60"). Coerce to int with a
    #    safe fallback so ``max()`` below never raises TypeError.
    def _coerce_int(name, raw, default):
        try:
            return int(raw)
        except (TypeError, ValueError) as _e:
            logger.warning('[Timer] Non-integer %s=%r — coerced to default %d '
                           '(reason: %s)', name, raw, default, _e)
            return default
    poll_interval = _coerce_int('poll_interval', poll_interval, 60)
    max_polls = _coerce_int('max_polls', max_polls, 120)

    poll_interval = max(poll_interval, 10)  # floor at 10s

    payload = {
        'timer_id': timer_id, 'user_id': int(user_id), 'conv_id': conv_id,
        'source_task_id': source_task_id,
        'check_instruction': check_instruction,
        'check_command': check_command,
        'continuation_message': continuation_message,
        'poll_interval': poll_interval, 'max_polls': max_polls,
        'tools_config': tools_config or {}, 'created_at': now,
        'updated_at': now, 'condition_kind': condition_kind,
        'condition_command': condition_command,
        'condition_regex': condition_regex, 'origin': 'background',
    }
    timer = _timer_client(write=True).command(
        'timer.create', payload, timer_id)['timer']
    from ._notify import notify_timer_changed
    notify_timer_changed('created', user_id=user_id)
    logger.info('[Timer:%s] Created — conv=%s poll_interval=%ds max_polls=%d kind=%s check_cmd=%s pred=%s',
                timer_id, conv_id[:12], poll_interval, max_polls, condition_kind,
                (check_command[:80] + '…') if len(check_command) > 80 else check_command or '(none)',
                (condition_command[:80] + '…') if len(condition_command) > 80 else condition_command or '(none)')
    return timer


def cancel_timer(timer_id: str, *, user_id: int) -> bool:
    """Cancel an active timer."""
    now = datetime.now().isoformat()
    changed = bool(_timer_client(write=True).command(
        'timer.cancel', {
            'timer_id': timer_id, 'user_id': int(user_id), 'now': now},
        f'timer.cancel:{timer_id}:{now}').get('changed'))

    # Signal the background thread to stop
    with _timers_lock:
        _active_timers.pop(timer_id, None)
    with _cmd_outputs_lock:
        _last_cmd_outputs.pop(timer_id, None)
    from ._poll import _reconcile_audit, _reconcile_audit_lock
    with _reconcile_audit_lock:
        _reconcile_audit.pop(timer_id, None)

    if changed:
        from ._notify import notify_timer_changed
        notify_timer_changed('cancelled', user_id=user_id)
        logger.info('[Timer:%s] Cancelled', timer_id)
    return changed


def force_trigger_timer(timer_id: str, *, user_id: int) -> str | None:
    """Force-trigger a timer, skipping the poll.

    Returns:
        The execution task_id, or None on failure.
    """
    from ._loop import _execute_continuation

    timer = get_timer(timer_id, user_id=user_id)
    if not timer:
        return None
    if timer['status'] != 'active':
        logger.warning('[Timer:%s] Cannot trigger — status=%s', timer_id, timer['status'])
        return None

    return _execute_continuation(timer)


def get_timer(timer_id: str, *, user_id: int) -> dict[str, Any] | None:
    """Get a single timer by ID."""
    return _get_timer_row(timer_id, user_id=user_id)


def list_active_timers(*, user_id: int) -> list[dict[str, Any]]:
    """Return all timers (active first, then recent triggered/cancelled)."""
    return _timer_client().query(
        'timer.list', {'user_id': int(user_id), 'limit': 50})


def has_timer_history(*, user_id: int) -> bool:
    """Whether the timer panel has any durable row to surface.

    The closed badge needs only this bit plus the in-memory active count.  Do
    not materialize up to 50 wide watcher rows on every push-disconnected
    fallback poll merely to evaluate ``timers.length > 0`` in the browser.
    """
    return bool(_timer_client().query(
        'timer.history', {'user_id': int(user_id)}))


def get_timer_poll_log(
    timer_id: str, *, user_id: int, limit: int = 30,
) -> list[dict]:
    """Retrieve recent poll log entries for a timer."""
    return _timer_client().query(
        'timer.poll.log', {
            'timer_id': timer_id, 'user_id': int(user_id),
            'limit': limit})


def _get_timer_row(timer_id: str, *, user_id: int) -> dict[str, Any] | None:
    """Fetch an owner-scoped timer record from the storage authority."""
    return _timer_client().query(
        'timer.get', {'timer_id': timer_id, 'user_id': int(user_id)})

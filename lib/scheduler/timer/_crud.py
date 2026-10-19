"""lib/scheduler/timer/_crud.py — Owner-scoped Timer CRUD.

Create, cancel, force-trigger, get, list and poll-log accessors.
Restart restores active work without rewriting state based on creation age.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from lib.log import get_logger

from ._state import _active_timers as _active_timers, _timers_lock as _timers_lock, remove_live_watcher as remove_live_watcher

logger = get_logger(__name__)


def _timer_client(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


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
    import re

    if not str(continuation_message or '').strip():
        raise ValueError('continuation_message is required')
    if not str(check_instruction or '').strip() and not str(condition_command or '').strip():
        raise ValueError('a readiness instruction or predicate is required')
    if condition_regex:
        if not str(condition_command or '').strip():
            raise ValueError('condition_regex requires condition_command')
        try:
            re.compile(condition_regex)
        except re.error as exc:
            raise ValueError(f'invalid condition_regex: {exc}') from exc

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
    if max_polls < 0:
        raise ValueError('max_polls must be nonnegative (0 means unlimited)')

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
    if not changed:
        # A denied/foreign-owner cancellation has no process-local authority.
        return False

    # Wake the background watcher to stop
    remove_live_watcher(timer_id)
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

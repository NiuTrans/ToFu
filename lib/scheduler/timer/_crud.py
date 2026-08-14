"""lib/scheduler/timer/_crud.py — Timer CRUD + resume guardrail helpers.

create / cancel / force-trigger / get / list / poll-log accessors, plus the
env-tunable resume guardrail helpers (``_resume_max_age_seconds`` /
``_resume_concurrency_cap``) consulted by the resume-on-restart path.
"""

from __future__ import annotations

import json
import os as _os
import uuid
from datetime import datetime
from typing import Any

from lib.log import get_logger

from ._state import _active_timers, _cmd_outputs_lock, _last_cmd_outputs, _timers_lock

logger = get_logger(__name__)


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

def create_timer(conv_id: str,
                 check_instruction: str,
                 continuation_message: str,
                 poll_interval: int = 60,
                 max_polls: int = 120,
                 check_command: str = '',
                 tools_config: dict | None = None,
                 source_task_id: str = '',
                 condition_command: str = '',
                 condition_regex: str = '',
                 origin: str = 'inline') -> dict[str, Any]:
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
        origin: Provenance marker — 'inline' (the timer_create tool, which
            BLOCKS its parent task and polls inline; the only path today) or
            'background' (a self-driving injector). The resume-on-restart path
            uses this to retire an orphaned inline timer whose parent task died
            with the process, instead of silently injecting a follow-up turn
            into an abandoned conversation.

    Returns:
        Timer record dict.
    """
    from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, pooled_db
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

    with pooled_db(DOMAIN_SYSTEM) as db:
        db_execute_with_retry(
            db,
            '''INSERT INTO timer_watchers
               (id, conv_id, source_task_id, check_instruction, check_command,
                continuation_message, poll_interval, max_polls, status,
                tools_config, created_at, updated_at,
                condition_kind, condition_command, condition_regex, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)''',
            [timer_id, conv_id, source_task_id, check_instruction, check_command,
             continuation_message, poll_interval, max_polls,
             json.dumps(tools_config or {}, ensure_ascii=False), now, now,
             condition_kind, condition_command, condition_regex, origin]
        )

    timer = _get_timer_row(timer_id)
    from ._notify import notify_timer_changed
    notify_timer_changed('created')
    logger.info('[Timer:%s] Created — conv=%s poll_interval=%ds max_polls=%d kind=%s check_cmd=%s pred=%s',
                timer_id, conv_id[:12], poll_interval, max_polls, condition_kind,
                (check_command[:80] + '…') if len(check_command) > 80 else check_command or '(none)',
                (condition_command[:80] + '…') if len(condition_command) > 80 else condition_command or '(none)')
    return timer


def cancel_timer(timer_id: str) -> bool:
    """Cancel an active timer."""
    from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, pooled_db

    now = datetime.now().isoformat()
    with pooled_db(DOMAIN_SYSTEM) as db:
        cursor = db_execute_with_retry(
            db,
            "UPDATE timer_watchers SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=? AND status='active'",
            [now, now, timer_id], return_cursor=True,
        )
        changed = cursor.rowcount > 0

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
        notify_timer_changed('cancelled')
        logger.info('[Timer:%s] Cancelled', timer_id)
    return changed


def force_trigger_timer(timer_id: str) -> str | None:
    """Force-trigger a timer, skipping the poll.

    Returns:
        The execution task_id, or None on failure.
    """
    from ._loop import _execute_continuation

    timer = get_timer(timer_id)
    if not timer:
        return None
    if timer['status'] != 'active':
        logger.warning('[Timer:%s] Cannot trigger — status=%s', timer_id, timer['status'])
        return None

    return _execute_continuation(timer)


def get_timer(timer_id: str) -> dict[str, Any] | None:
    """Get a single timer by ID."""
    return _get_timer_row(timer_id)


def list_active_timers() -> list[dict[str, Any]]:
    """Return all timers (active first, then recent triggered/cancelled)."""
    from lib.database import DOMAIN_SYSTEM, pooled_db
    with pooled_db(DOMAIN_SYSTEM) as db:
        rows = db.execute(
            '''SELECT * FROM timer_watchers
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                        created_at DESC
               LIMIT 50'''
        ).fetchall()
    return [dict(r) for r in rows]


def has_timer_history() -> bool:
    """Whether the timer panel has any durable row to surface.

    The closed badge needs only this bit plus the in-memory active count.  Do
    not materialize up to 50 wide watcher rows on every push-disconnected
    fallback poll merely to evaluate ``timers.length > 0`` in the browser.
    """
    from lib.database import DOMAIN_SYSTEM, pooled_db
    with pooled_db(DOMAIN_SYSTEM) as db:
        row = db.execute('SELECT 1 AS present FROM timer_watchers LIMIT 1').fetchone()
    return row is not None


def get_timer_poll_log(timer_id: str, limit: int = 30) -> list[dict]:
    """Retrieve recent poll log entries for a timer."""
    from lib.database import DOMAIN_SYSTEM, pooled_db
    try:
        with pooled_db(DOMAIN_SYSTEM) as db:
            rows = db.execute(
                'SELECT * FROM timer_poll_log WHERE timer_id=? ORDER BY poll_time DESC LIMIT ?',
                [timer_id, limit]
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning('[Timer] Failed to get poll log for %s: %s', timer_id, e, exc_info=True)
        return []


def _get_timer_row(timer_id: str) -> dict[str, Any] | None:
    """Fetch a timer record from DB."""
    from lib.database import DOMAIN_SYSTEM, pooled_db
    with pooled_db(DOMAIN_SYSTEM) as db:
        row = db.execute('SELECT * FROM timer_watchers WHERE id=?', [timer_id]).fetchone()
    return dict(row) if row else None

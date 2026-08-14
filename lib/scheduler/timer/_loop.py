"""lib/scheduler/timer/_loop.py — Continuation dispatch + background poll loop.

Owns the continuation executor (inject user message → start agentic task), the
background daemon poll loop that drives each timer at its interval, and the
resume-on-restart path (with age-sweep + concurrency-cap guardrails).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from lib.log import get_logger

from ._crud import _get_timer_row, _resume_concurrency_cap, _resume_max_age_seconds
from ._poll import (
    _increment_poll_count,
    _mark_exhausted,
    _mark_expired,
    _mark_orphaned,
    _record_poll,
    poll_timer,
)
from ._state import _active_timers, _cmd_outputs_lock, _last_cmd_outputs, _timers_lock

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  Continuation execution — inject user message + start agentic task
# ═════════════════════════════════════════════════════════════════════════════

def _execute_continuation(timer: dict[str, Any]) -> str | None:
    """Inject user message and start agentic task in the target conversation.

    Args:
        timer: The timer record dict.

    Returns:
        The agentic task_id, or None on failure.
    """
    from lib.scheduler._shared import inject_and_run_task

    timer_id = timer['id']
    conv_id = timer['conv_id']
    continuation_msg = timer['continuation_message']
    log_prefix = f'[Timer:{timer_id}]'

    logger.info('%s 🚀 Executing continuation in conv=%s', log_prefix, conv_id[:12])

    # Build the timer-specific user message
    user_message = {
        'role': 'user',
        'content': (
            f'⏱️ **[Timer Watcher Triggered — {timer_id}]**\n\n'
            f'{continuation_msg}'
        ),
        'timestamp': datetime.now().isoformat(),
        '_timer': True,
        '_timerId': timer_id,
    }

    agentic_task_id = inject_and_run_task(
        conv_id=conv_id,
        user_message=user_message,
        tools_config_json=timer.get('tools_config', '{}'),
        log_prefix=log_prefix,
    )

    if agentic_task_id:
        # Mark timer as triggered in DB
        try:
            from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, get_thread_db
            sysdb = get_thread_db(DOMAIN_SYSTEM)
            now_iso = datetime.now().isoformat()
            db_execute_with_retry(
                sysdb,
                "UPDATE timer_watchers SET status='triggered', triggered_at=?, "
                "execution_task_id=?, updated_at=? WHERE id=? AND status='active'",
                [now_iso, agentic_task_id, now_iso, timer_id]
            )
            from ._notify import notify_timer_changed
            notify_timer_changed('triggered')
        except Exception as e:
            logger.error('%s Failed to mark timer as triggered: %s',
                         log_prefix, e, exc_info=True)

    # Clean up in-memory state regardless of outcome
    with _timers_lock:
        _active_timers.pop(timer_id, None)
    with _cmd_outputs_lock:
        _last_cmd_outputs.pop(timer_id, None)

    return agentic_task_id


# ═════════════════════════════════════════════════════════════════════════════
#  Background poll loop
# ═════════════════════════════════════════════════════════════════════════════

def start_timer_loop(timer_id: str) -> None:
    """Start a background daemon thread that polls the timer at its interval.

    The thread self-terminates after:
      - Conditions are met and continuation is executed, OR
      - max_polls is exhausted, OR
      - Timer is cancelled.
    """
    timer = _get_timer_row(timer_id)
    if not timer:
        logger.error('[Timer:%s] Cannot start loop — timer not found', timer_id)
        return

    def _loop():
        tid = timer_id
        logger.info('[Timer:%s] Poll loop started (interval=%ds, max_polls=%d)',
                     tid, timer['poll_interval'], timer['max_polls'])
        poll_interval = timer['poll_interval']
        max_polls = timer['max_polls']

        while True:
            # Release any thread-local DB connection acquired in the PREVIOUS
            # iteration before we sleep again — a long-lived (or unlimited)
            # timer would otherwise pin a connection across every poll_interval
            # sleep, leaking a connection-semaphore slot for its whole life.
            # Placed at loop top so every continue/break path is covered.
            try:
                from lib.database import close_thread_db
                close_thread_db()
            except Exception as _ce:
                logger.debug('[Timer:%s] close_thread_db failed: %s', tid, _ce)

            # Check if still active
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry — stopping', tid)
                    break

            # Sleep first (give the initial task time to finish before first poll)
            time.sleep(poll_interval)

            # Re-check after sleep
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry after sleep — stopping', tid)
                    break

            # Refresh timer state from DB (in case of external cancel)
            current = _get_timer_row(tid)
            if not current or current['status'] != 'active':
                logger.info('[Timer:%s] Status is %s — stopping poll loop',
                            tid, current['status'] if current else 'deleted')
                break

            # Check max_polls
            poll_count = current.get('poll_count', 0)
            if max_polls > 0 and poll_count >= max_polls:
                logger.info('[Timer:%s] Max polls (%d) exhausted — marking exhausted',
                            tid, max_polls)
                _mark_exhausted(tid)
                break

            # poll_count is the DB count BEFORE this poll; the poll about to
            # run is therefore #(poll_count+1). Mint a stable id so this exact
            # check is locatable across the log, the DB row, and the UI.
            this_poll_num = poll_count + 1
            poll_id = f'{tid}.p{this_poll_num}'
            # Run poll
            try:
                (ready, reason, tokens_used, skipped, parse_error, cmd_output,
                 poll_model, _tool_trace, raw_content) = poll_timer(tid)
            except Exception as e:
                logger.error('[Timer:%s] Poll %s error: %s', tid, poll_id, e, exc_info=True)
                _record_poll(tid, 'error', str(e)[:200], 0, poll_id=poll_id,
                             raw_output=str(e)[:2000])
                _increment_poll_count(tid, 'error', str(e)[:200])
                continue

            # Skipped polls (unchanged command output) — no LLM call,
            # no DB record, no SSE event — just silently wait. We STILL
            # increment poll_count so a timer whose check_command output never
            # changes deterministically reaches max_polls and retires, instead
            # of polling forever (zombie-timer leak).
            if skipped:
                logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                             tid, this_poll_num)
                _increment_poll_count(tid, 'skipped', 'output unchanged')
                continue

            decision = 'ready' if ready else ('parse_error' if parse_error else 'wait')
            # Persist the raw LLM output only when it carries diagnostic value
            # (a malformed decision) — a clean wait/ready needs no raw dump.
            _raw_to_store = raw_content if parse_error else ''
            _record_poll(tid, decision, reason, tokens_used, cmd_output, poll_model,
                         poll_id=poll_id, raw_output=_raw_to_store)
            _increment_poll_count(tid, decision, reason)

            logger.info('[Timer:%s] Poll %s: %s — %s (tokens=%d, model=%s)',
                        tid, poll_id, decision, reason[:80], tokens_used,
                        poll_model or '?')

            if ready:
                logger.info('[Timer:%s] ✅ Conditions met — executing continuation', tid)
                exec_id = _execute_continuation(current)
                if exec_id:
                    logger.info('[Timer:%s] 🚀 Continuation started: task=%s', tid, exec_id[:8])
                else:
                    logger.error('[Timer:%s] ❌ Continuation execution failed', tid)
                break

        logger.info('[Timer:%s] Poll loop ended', tid)
        # Clean up registry
        with _timers_lock:
            _active_timers.pop(tid, None)
        # Final release of this thread's DB connection back to the pool.
        try:
            from lib.database import close_thread_db
            close_thread_db()
        except Exception as _ce:
            logger.debug('[Timer:%s] close_thread_db failed at loop end: %s', tid, _ce)

    # Register and start
    t = threading.Thread(target=_loop, daemon=True, name=f'timer-poll-{timer_id}')
    with _timers_lock:
        _active_timers[timer_id] = t
    t.start()
    logger.info('[Timer:%s] Background poll thread started', timer_id)


# ═════════════════════════════════════════════════════════════════════════════
#  Resume on server restart
# ═════════════════════════════════════════════════════════════════════════════

def resume_active_timers() -> int:
    """Resume all timers with status='active' from DB.

    Called on server startup. Returns the number of timers resumed.
    """
    # Resolve the hookable spawn point through the package facade so a
    # ``monkeypatch.setattr(lib.scheduler.timer, 'start_timer_loop', …)`` takes
    # effect here, exactly as it did when this all lived in one module.
    import lib.scheduler.timer as _timer_pkg

    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        rows = db.execute(
            "SELECT * FROM timer_watchers WHERE status='active' "
            "ORDER BY created_at ASC"
        ).fetchall()
        rows = [dict(r) for r in rows]

        now = datetime.now()
        cap = _resume_concurrency_cap()

        # ── Pass 1: age-sweep — expire zombies that outlived their budget ──
        survivors: list[dict] = []
        expired = 0
        for timer in rows:
            created_raw = timer.get('created_at') or ''
            age = None
            try:
                if created_raw:
                    age = (now - datetime.fromisoformat(created_raw)).total_seconds()
            except (TypeError, ValueError) as _pe:
                logger.debug('[Timer:%s] Unparseable created_at=%r: %s',
                             timer.get('id'), created_raw, _pe)
            if age is not None and age > _resume_max_age_seconds(timer):
                _mark_expired(timer['id'])
                expired += 1
                logger.warning('[Timer:%s] Auto-expired on resume — age %.0fh exceeds '
                               'budget (poll_count=%s/%s)', timer['id'], age / 3600.0,
                               timer.get('poll_count'), timer.get('max_polls'))
                continue
            survivors.append(timer)

        if expired:
            logger.warning('[Timer] Auto-expired %d over-age zombie timer(s) on startup',
                           expired)

        # ── Pass 1.5: retire orphaned INLINE timers ────────────────────────
        # An origin='inline' timer is parent-blocking: it exists only to feed
        # its result back into the in-memory task that ran `timer_create`. That
        # task died with the previous process, so at resume time the in-memory
        # registry (_active_timers) is empty by definition and any still-active
        # inline row is a definitional ORPHAN. Re-spawning it as a background
        # injector is exactly what floated abandoned conversations to the top
        # of the sidebar (_execute_continuation → notify_conv_changed). Retire
        # it to 'orphaned' (distinct from over-age 'expired') and never spawn /
        # never inject. Only genuine 'background' timers proceed to re-spawn.
        # (Rows with a non-'inline' origin — e.g. legacy/back-compat — take the
        # background path; the query already filters to status='active', so a
        # triggered/exhausted terminal row never reaches here.)
        respawnable: list[dict] = []
        orphaned = 0
        for timer in survivors:
            if (timer.get('origin') or 'inline') == 'inline':
                _mark_orphaned(timer['id'])
                orphaned += 1
                logger.info('[Timer:%s] Orphaned inline timer retired on resume '
                            '(parent task died with prior process) — not re-spawned, '
                            'no follow-up injected', timer['id'])
                continue
            respawnable.append(timer)

        if orphaned:
            logger.info('[Timer] Retired %d orphaned inline timer(s) on startup', orphaned)

        # ── Pass 2: re-spawn survivors, capped ─────────────────────────────
        count = 0
        skipped = 0
        for timer in respawnable:
            timer_id = timer['id']
            # NB: must NOT hold _timers_lock across start_timer_loop() — that
            # function re-acquires the (non-reentrant) _timers_lock to register
            # the thread, so calling it while holding the lock self-deadlocks
            # the resume thread and pins _timers_lock forever.
            with _timers_lock:
                already_active = timer_id in _active_timers
            if already_active:
                continue
            if cap > 0 and count >= cap:
                skipped += 1
                continue
            _timer_pkg.start_timer_loop(timer_id)
            count += 1
            logger.info('[Timer:%s] Resumed on server startup', timer_id)

        if skipped:
            logger.warning('[Timer] Resume cap (%d) reached — %d active timer(s) NOT '
                           'resumed this boot (will retry next restart). Set '
                           'TOFU_TIMER_RESUME_CAP to raise.', cap, skipped)
        if count > 0:
            logger.info('[Timer] Resumed %d active timer(s) on startup', count)
        return count
    except Exception as e:
        logger.warning('[Timer] Failed to resume active timers: %s', e, exc_info=True)
        return 0


def get_active_timer_count() -> int:
    """Return count of in-memory active timer threads."""
    with _timers_lock:
        return len(_active_timers)

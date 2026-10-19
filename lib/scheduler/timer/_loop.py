"""lib/scheduler/timer/_loop.py — Continuation dispatch + background poll loop.

Owns the continuation executor (inject user message → start agentic task), the
background daemon poll loop that drives each timer at its interval, and the
resume-on-restart path with durable lease ownership.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from lib.log import get_logger

from ._crud import (
    _get_timer_row,
    _timer_client,
)
from ._lease import (
    WATCHER_LEASE_TTL,
    claim_watcher_lease,
    release_watcher_lease,
    renew_watcher_lease,
)
from ._poll import (
    _increment_poll_count,
    _mark_exhausted,
    _record_poll,
    poll_timer,
)
from ._state import _active_timers, _timers_lock, remove_live_watcher
from ._runtime import TimerWatcherHandle, run_blocking, watcher_delay

logger = get_logger(__name__)


def _mark_dispatch_failed(timer: dict[str, Any], reason: str) -> None:
    """Retire a watcher whose continuation can never be dispatched.

    Lane contention and infrastructure errors are retryable and never reach
    this function. A missing target or a durable turn whose executor could not
    start is terminal: keeping that watcher active would create an unbounded
    retry loop with the same idempotency key.
    """
    timer_id = str(timer['id'])
    user_id = int(timer['user_id'])
    now_iso = datetime.now().isoformat()
    changed = False
    try:
        changed = bool(_timer_client(write=True).command(
            'timer.update', {
                'timer_id': timer_id,
                'user_id': user_id,
                'status': 'failed',
                'last_poll_decision': 'dispatch_failed',
                'last_poll_reason': reason[:500],
                'updated_at': now_iso,
                'expected_status': 'active',
            }, f'timer.dispatch-failed:{timer_id}').get('changed'))
    except Exception as exc:
        # Failure to persist retirement is transient. Keep the watcher alive
        # so durable work cannot disappear after an infrastructure fault.
        logger.error('[Timer:%s] Failed to persist dispatch failure: %s',
                     timer_id, exc, exc_info=True)
        return

    if not changed:
        return
    remove_live_watcher(timer_id)
    from ._poll import _reconcile_audit, _reconcile_audit_lock
    with _reconcile_audit_lock:
        _reconcile_audit.pop(timer_id, None)
    from ._notify import notify_timer_changed
    notify_timer_changed('failed', user_id=user_id)


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
    from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn

    current = _get_timer_row(str(timer['id']), user_id=int(timer['user_id']))
    if not current or current.get('status') != 'active':
        return None
    timer = current

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

    try:
        dispatch = dispatch_scheduled_turn(
            conversation_id=conv_id,
            user_message=user_message,
            tools_config=timer.get('tools_config', '{}'),
            user_id=int(timer['user_id']),
            command_id=f'timer:{timer_id}',
            log_prefix=log_prefix,
        )
    except Exception as exc:
        logger.error('%s Continuation dispatch unavailable: %s', log_prefix,
                     exc, exc_info=True)
        return None

    if dispatch.disposition == 'busy':
        return None
    if dispatch.disposition != 'started':
        reason = (
            'target conversation no longer exists'
            if dispatch.disposition == 'target_missing'
            else 'durable continuation could not start its executor'
        )
        logger.error('%s Terminal continuation failure: %s', log_prefix, reason)
        _mark_dispatch_failed(timer, reason)
        return None

    agentic_task_id = dispatch.task_id

    if agentic_task_id:
        # Mark timer as triggered in DB
        try:
            now_iso = datetime.now().isoformat()
            receipt = _timer_client(write=True).command(
                'timer.update', {'timer_id': timer_id,
                                 'user_id': int(timer['user_id']),
                                 'status': 'triggered',
                                 'triggered_at': now_iso,
                                 'execution_task_id': agentic_task_id,
                                 'updated_at': now_iso,
                                 'expected_status': 'active'},
                f'timer.triggered:{timer_id}:{agentic_task_id}')
            if not receipt.get('changed'):
                # Cancellation/another owner won the status predicate. Never
                # advertise a transition the storage authority did not accept.
                return None
            from ._notify import notify_timer_changed
            notify_timer_changed('triggered', user_id=int(timer['user_id']))
        except Exception as e:
            logger.error('%s Failed to mark timer as triggered: %s',
                         log_prefix, e, exc_info=True)
            # Keep the committed ready obligation live. The next pass replays
            # timer:<id> through the Turn command owner, which returns the same
            # attempt without launching another executor. If the write committed
            # but its ACK was lost, the next row read observes terminal status.
            return None

    if agentic_task_id:
        remove_live_watcher(timer_id)

    return agentic_task_id


# ═════════════════════════════════════════════════════════════════════════════
#  Background poll loop
# ═════════════════════════════════════════════════════════════════════════════

async def _sleep_with_lease(tid: str, token: str, seconds: float) -> bool:
    """Sleep ``seconds`` in lease-renewal slices.

    Returns False when the watcher must stop: the timer left the active
    registry, or the lease is now held by another replica (cooperative,
    idempotent — the DB row is untouched so the new owner, or a later
    resume sweep, keeps driving the timer).
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if not await watcher_delay(min(remaining, WATCHER_LEASE_TTL / 3.0)):
            return False
        with _timers_lock:
            if tid not in _active_timers:
                logger.info('[Timer:%s] Removed from active registry — stopping', tid)
                return False
        if not await run_blocking(renew_watcher_lease, tid, token):
            logger.info('[Timer:%s] Watcher lease taken over by another '
                        'replica — stopping local watcher', tid)
            return False


def start_timer_loop(timer_id: str, *, user_id: int) -> bool:
    """Start an owned coroutine that polls the timer at its interval.

    The watcher self-terminates after:
      - Conditions are met and continuation is executed, OR
      - max_polls is exhausted, OR
      - Timer is cancelled.
    """
    timer = _get_timer_row(timer_id, user_id=user_id)
    if not timer:
        logger.error('[Timer:%s] Cannot start loop — timer not found', timer_id)
        return

    # Cross-replica single-owner gate: only the lease holder runs the watcher
    # loop. With the inproc backend this claim always succeeds inside the one
    # process, so personal mode behaves exactly as before.
    lease_token = claim_watcher_lease(timer_id)
    if lease_token is None:
        logger.info('[Timer:%s] Watcher lease held by another replica — '
                    'not starting a local watcher', timer_id)
        return False

    async def _loop():
        tid = timer_id
        logger.info('[Timer:%s] Poll loop started (interval=%ds, max_polls=%d)',
                     tid, timer['poll_interval'], timer['max_polls'])
        poll_interval = timer['poll_interval']
        max_polls = timer['max_polls']

        while True:
            # Check if still active
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry — stopping', tid)
                    break

            # Sleep first (give the initial task time to finish before first
            # poll); the lease is renewed at ttl/3 cadence inside the sleep.
            if not await _sleep_with_lease(tid, lease_token, poll_interval):
                break

            # Re-check after sleep
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry after sleep — stopping', tid)
                    break

            # Refresh timer state from DB (in case of external cancel)
            current = await run_blocking(_get_timer_row, tid, user_id=user_id)
            if not current or current['status'] != 'active':
                logger.info('[Timer:%s] Status is %s — stopping poll loop',
                            tid, current['status'] if current else 'deleted')
                break

            # A committed ready verdict is a durable dispatch obligation.
            # Lane contention must not consume another poll, repeat a model
            # call, lose a transient condition, or exhaust the watcher.
            if current.get('last_poll_decision') == 'ready':
                exec_id = await run_blocking(_execute_continuation, current)
                if exec_id:
                    break
                continue

            # Check max_polls
            poll_count = current.get('poll_count', 0)
            if max_polls > 0 and poll_count >= max_polls:
                logger.info('[Timer:%s] Max polls (%d) exhausted — marking exhausted',
                            tid, max_polls)
                if await run_blocking(_mark_exhausted, tid, user_id=user_id):
                    break
                continue

            # poll_count is the DB count BEFORE this poll; the poll about to
            # run is therefore #(poll_count+1). Mint a stable id so this exact
            # check is locatable across the log, the DB row, and the UI.
            this_poll_num = poll_count + 1
            poll_id = f'{tid}.p{this_poll_num}'
            # Run poll
            try:
                (ready, reason, tokens_used, skipped, parse_error, cmd_output,
                 poll_model, _tool_trace, raw_content) = await run_blocking(poll_timer,
                    tid, user_id=user_id)
            except Exception as e:
                logger.error('[Timer:%s] Poll %s error: %s', tid, poll_id, e, exc_info=True)
                try:
                    await run_blocking(_record_poll,
                        tid, 'error', str(e)[:200], 0, poll_id=poll_id,
                        raw_output=str(e)[:2000], user_id=user_id)
                except Exception as persist_error:
                    logger.warning(
                        '[Timer:%s] Poll error could not cross durability '
                        'boundary; retrying without advancing: %s',
                        tid, persist_error)
                continue

            # Skipped polls (unchanged command output) — no LLM call,
            # no DB record, no SSE event — just silently wait. We STILL
            # increment poll_count so a timer whose check_command output never
            # changes deterministically reaches max_polls and retires, instead
            # of polling forever (zombie-timer leak).
            if skipped:
                logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                             tid, this_poll_num)
                try:
                    await run_blocking(_increment_poll_count,
                        tid, 'skipped', 'output unchanged', user_id=user_id)
                except Exception as persist_error:
                    logger.warning(
                        '[Timer:%s] Skipped poll could not advance durably; '
                        'retrying: %s', tid, persist_error)
                continue

            decision = ('ready' if ready else (
                ('error' if poll_model == 'predicate' else 'parse_error')
                if parse_error else 'wait'))
            # Persist the raw LLM output only when it carries diagnostic value
            # (a malformed decision) — a clean wait/ready needs no raw dump.
            _raw_to_store = raw_content if parse_error else ''
            try:
                await run_blocking(_record_poll,
                    tid, decision, reason, tokens_used, cmd_output, poll_model,
                    poll_id=poll_id, raw_output=_raw_to_store,
                    user_id=user_id)
            except Exception as persist_error:
                logger.warning(
                    '[Timer:%s] Poll result could not commit atomically; '
                    'retrying without advancing: %s', tid, persist_error)
                continue

            logger.info('[Timer:%s] Poll %s: %s — %s (tokens=%d, model=%s)',
                        tid, poll_id, decision, reason[:80], tokens_used,
                        poll_model or '?')

            if ready:
                logger.info('[Timer:%s] ✅ Conditions met — executing continuation', tid)
                exec_id = await run_blocking(_execute_continuation, current)
                if exec_id:
                    logger.info('[Timer:%s] 🚀 Continuation started: task=%s', tid, exec_id[:8])
                    break
                latest = await run_blocking(_get_timer_row, tid, user_id=user_id)
                if not latest or latest.get('status') != 'active':
                    logger.info('[Timer:%s] Continuation retired with status=%s',
                                tid, (latest or {}).get('status', 'deleted'))
                    break
                logger.info('[Timer:%s] Conversation lane or dispatcher unavailable; '
                            'watcher remains active and will retry', tid)
                continue

        logger.info('[Timer:%s] Poll loop ended', tid)

    async def _run_owned_loop():
        try:
            await _loop()
        except asyncio.CancelledError:
            logger.info('[Timer:%s] Watcher stopped; owned calls have settled', timer_id)
        except Exception:
            logger.exception('[Timer:%s] Watcher stopped unexpectedly; durable '
                             'state remains available for resume', timer_id)
        finally:
            # Storage/lease read faults must not leave a dead watcher registered
            # forever. Never remove a replacement loop admitted in the meantime.
            with _timers_lock:
                if _active_timers.get(timer_id) is t:
                    _active_timers.pop(timer_id, None)
            from ._poll import _reconcile_audit, _reconcile_audit_lock
            with _reconcile_audit_lock:
                _reconcile_audit.pop(timer_id, None)
            await run_blocking(release_watcher_lease, timer_id, lease_token,
                               _allow_stopping=True)

    # Register and start. The durable lease selects one replica; the local
    # registry protects duplicate starts across create/resume races.
    try:
        t = TimerWatcherHandle(target=_run_owned_loop,
                               name=f'timer-poll-{timer_id}')
    except Exception:
        release_watcher_lease(timer_id, lease_token)
        raise
    with _timers_lock:
        if timer_id in _active_timers:
            release_watcher_lease(timer_id, lease_token)
            return False
        _active_timers[timer_id] = t
    try:
        t.start()
    except Exception:
        with _timers_lock:
            if _active_timers.get(timer_id) is t:
                _active_timers.pop(timer_id, None)
        release_watcher_lease(timer_id, lease_token)
        raise
    logger.info('[Timer:%s] Background poll watcher started', timer_id)
    return True


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
        rows = _timer_client().query(
            'timer.active.list_all', {'limit': 0})

        # Durable active status and actual poll evidence own recovery.
        # Offline wall-clock time neither spends poll budget nor cancels work.
        count = 0
        for timer in rows:
            timer_id = timer['id']
            # NB: must NOT hold _timers_lock across start_timer_loop() — that
            # function re-acquires the (non-reentrant) _timers_lock to register
            # the thread, so calling it while holding the lock self-deadlocks
            # the resume thread and pins _timers_lock forever.
            with _timers_lock:
                already_active = timer_id in _active_timers
            if already_active:
                continue
            if _timer_pkg.start_timer_loop(
                    timer_id, user_id=int(timer['user_id'])) is False:
                # Another replica holds the watcher lease for this timer.
                continue
            count += 1
            logger.info('[Timer:%s] Resumed on server startup', timer_id)

        if count > 0:
            logger.info('[Timer] Resumed %d active timer(s) on startup', count)
        return count
    except Exception as e:
        logger.warning('[Timer] Failed to resume active timers: %s', e, exc_info=True)
        raise


def get_active_timer_count(*, user_id: int) -> int:
    """Return the owner's durable active-watcher count."""
    return int(_timer_client().query(
        'timer.active.count', {'user_id': int(user_id)}))

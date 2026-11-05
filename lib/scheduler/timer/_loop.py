"""lib/scheduler/timer/_loop.py — Continuation dispatch + background poll loop.

Owns the continuation executor (inject user message → start agentic task), the
background daemon poll loop that drives each timer at its interval, and the
resume-on-restart path with durable lease ownership.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

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

_CONTINUATION_COMMAND_PREFIX = 'timer:'


def continuation_command_id(timer_id: str) -> str:
    """The conversation command identity of a timer's single continuation."""
    return f'{_CONTINUATION_COMMAND_PREFIX}{timer_id}'


@dataclass(frozen=True, slots=True)
class TimerContinuationOutcome:
    """Where one timer continuation ended up.

    ``started`` — its own turn began and ``task_id`` is that executor.
    ``steered`` — committed into the attempt that was ALREADY running.
    ``queued`` — durable behind the running turn; the queue drain starts it.
    ``retry``  — nothing was accepted; the watcher must stay active.
    ``closed`` — the timer is settled (cancelled, already triggered, or
                 retired by a terminal dispatch failure).

    A single-shot watcher owns one obligation, so every accepted delivery
    retires it. Only ``retry`` leaves it watching.
    """

    kind: Literal["started", "steered", "queued", "retry", "closed"]
    task_id: str = ""
    queue_id: str = ""

    @property
    def accepted(self) -> bool:
        return self.kind in ("started", "steered", "queued")


def _execute_continuation(timer: dict[str, Any]) -> TimerContinuationOutcome:
    """Deliver the timer's continuation into its target conversation.

    A watcher never owns the target lane, so a busy conversation must not
    consume its one-shot obligation: the continuation is steered into the
    live attempt (committed as a durable injection block the model sees on
    its next round) with the durable queue as fallback.

    Args:
        timer: The timer record dict.

    Returns:
        A :class:`TimerContinuationOutcome` describing the delivery.
    """
    from lib.scheduler._shared import continuation_policy
    from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn

    current = _get_timer_row(str(timer['id']), user_id=int(timer['user_id']))
    if not current or current.get('status') != 'active':
        return TimerContinuationOutcome('closed')
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

    tools_config = timer.get('tools_config') or {}
    try:
        dispatch = dispatch_scheduled_turn(
            conversation_id=conv_id,
            user_message=user_message,
            tools_config=tools_config,
            user_id=int(timer['user_id']),
            command_id=continuation_command_id(timer_id),
            log_prefix=log_prefix,
            config_overrides=(continuation_policy(tools_config)
                              if isinstance(tools_config, Mapping) else None),
            # A busy lane is the normal case for a watcher: the conversation
            # is very often still working when the condition is met. Steer
            # commits the continuation into that live attempt and falls back
            # to the durable queue on its own.
            inject_mode='steer',
        )
    except Exception as exc:
        logger.error('%s Continuation dispatch unavailable: %s', log_prefix,
                     exc, exc_info=True)
        return TimerContinuationOutcome('retry')

    if dispatch.disposition == 'busy':
        return TimerContinuationOutcome('retry')
    if dispatch.disposition not in ('started', 'steered', 'queued'):
        reason = (
            'target conversation no longer exists'
            if dispatch.disposition == 'target_missing'
            else 'durable continuation could not start its executor'
        )
        logger.error('%s Terminal continuation failure: %s', log_prefix, reason)
        _mark_dispatch_failed(timer, reason)
        return TimerContinuationOutcome('closed')

    delivery = dispatch.disposition
    agentic_task_id = dispatch.task_id

    # Mark the obligation settled only after the delivery is durable. The
    # recorded decision IS the delivery receipt, so the timer panel and the
    # API can say whether the continuation joined the running turn, waits in
    # the queue, or started a turn of its own.
    try:
        now_iso = datetime.now().isoformat()
        update: dict[str, Any] = {
            'timer_id': timer_id,
            'user_id': int(timer['user_id']),
            'status': 'triggered',
            'triggered_at': now_iso,
            'last_poll_decision': delivery,
            'updated_at': now_iso,
            'expected_status': 'active',
        }
        if agentic_task_id:
            update['execution_task_id'] = agentic_task_id
        receipt = _timer_client(write=True).command(
            'timer.update', update,
            f'timer.triggered:{timer_id}:{delivery}')
        if not receipt.get('changed'):
            # Cancellation/another owner won the status predicate. Never
            # advertise a transition the storage authority did not accept.
            return TimerContinuationOutcome('closed')
        from ._notify import notify_timer_changed
        notify_timer_changed('triggered', user_id=int(timer['user_id']))
    except Exception as e:
        logger.error('%s Failed to mark timer as triggered: %s',
                     log_prefix, e, exc_info=True)
        # Keep the committed ready obligation live. The next pass replays
        # timer:<id> through the Turn command owner, which returns the same
        # attempt without launching another executor. If the write committed
        # but its ACK was lost, the next row read observes terminal status.
        return TimerContinuationOutcome('retry')

    remove_live_watcher(timer_id)
    logger.info('%s Continuation delivered (%s)', log_prefix, delivery)
    return TimerContinuationOutcome(
        delivery,
        task_id=agentic_task_id,
        queue_id=dispatch.queue_id,
    )


def record_requeued_continuation(command_id: str, *, user_id: int) -> bool:
    """Correct a steered continuation's receipt after it fell back to the queue.

    A steer is accepted into the running attempt but reaches the model only at
    that attempt's next round boundary. When the attempt ends first, the task
    finalizer re-queues the undelivered steer as the conversation's next turn,
    and the watcher's ``steered`` receipt would claim an injection that never
    happened. Commands of other producers are ignored. Best-effort: the queued
    turn, not this receipt, is the delivery authority.
    """
    if not command_id.startswith(_CONTINUATION_COMMAND_PREFIX):
        return False
    timer_id = command_id[len(_CONTINUATION_COMMAND_PREFIX):]
    try:
        changed = bool(_timer_client(write=True).command(
            'timer.update', {
                'timer_id': timer_id,
                'user_id': int(user_id),
                'last_poll_decision': 'queued',
                'updated_at': datetime.now().isoformat(),
                'expected_status': 'triggered',
            }, f'timer.requeued:{timer_id}').get('changed'))
    except Exception as exc:
        logger.warning('[Timer:%s] Re-queued continuation receipt not recorded: %s',
                       timer_id, exc, exc_info=True)
        return False
    if changed:
        from ._notify import notify_timer_changed
        notify_timer_changed('progress', user_id=int(user_id))
        logger.info('[Timer:%s] Steered continuation re-queued as the next turn',
                    timer_id)
    return changed


def _poll_decision(ready: bool, parse_error: bool, poll_model: str) -> str:
    """Ledger decision for one completed poll."""
    if ready:
        return 'ready'
    if not parse_error:
        return 'wait'
    return 'error' if poll_model == 'predicate' else 'parse_error'


@dataclass(frozen=True, slots=True)
class TimerCheckOutcome:
    """Verdict of one operator-requested readiness check.

    ``decision``/``reason`` are what the check left on the durable row
    (``wait``, ``error``, ``parse_error`` or ``ready``). ``delivery`` is set
    only for ``ready``: the continuation was dispatched exactly as a
    background poll would have dispatched it.
    """

    decision: str
    reason: str = ''
    delivery: TimerContinuationOutcome | None = None


def check_timer_now(timer_id: str, *, user_id: int) -> TimerCheckOutcome | None:
    """Run one readiness check now; deliver the continuation only when ready.

    The operator's counterpart of one background poll: it spends one poll of
    the declared budget, records its verdict in the same ledger, and a ready
    verdict becomes the same durable dispatch obligation. A row whose ready
    verdict is already committed is delivered without a new check, which
    could only lose a transient condition. Returns ``None`` when the timer is
    gone or settled, or when its poll budget is spent (it is then retired as
    exhausted, as the background loop would).
    """
    from lib.storage import StorageError

    timer = _get_timer_row(timer_id, user_id=user_id)
    if not timer or timer.get('status') != 'active':
        return None
    if timer.get('last_poll_decision') != 'ready':
        poll_count = int(timer.get('poll_count') or 0)
        max_polls = int(timer.get('max_polls') or 0)
        if max_polls > 0 and poll_count >= max_polls:
            _mark_exhausted(timer_id, user_id=user_id)
            return None
        poll_id = f'{timer_id}.p{poll_count + 1}'
        try:
            (ready, reason, tokens_used, _skipped, parse_error, cmd_output,
             poll_model, _tool_trace, raw_content) = poll_timer(
                timer_id, user_id=user_id)
            decision = _poll_decision(ready, parse_error, poll_model)
            raw_output = raw_content if parse_error else ''
        except Exception as exc:
            logger.error('[Timer:%s] Manual check %s error: %s',
                         timer_id, poll_id, exc, exc_info=True)
            decision, reason, tokens_used = 'error', str(exc)[:200], 0
            cmd_output, poll_model, raw_output = '', '', str(exc)[:2000]
        try:
            _record_poll(timer_id, decision, reason, tokens_used, cmd_output,
                         poll_model, poll_id=poll_id, raw_output=raw_output,
                         user_id=user_id)
        except StorageError as exc:
            # The poll number is one command identity. A background poll that
            # recorded it first owns that number, so the committed row below
            # decides instead of this call's verdict.
            if exc.code != 'database_conflict':
                raise
            logger.info('[Timer:%s] Manual check %s lost the poll number to '
                        'a concurrent poll', timer_id, poll_id)
        logger.info('[Timer:%s] Manual check %s: %s — %s',
                    timer_id, poll_id, decision, reason[:80])
        timer = _get_timer_row(timer_id, user_id=user_id)
        if not timer or timer.get('status') != 'active':
            return None
        if timer.get('last_poll_decision') != 'ready':
            return TimerCheckOutcome(
                str(timer.get('last_poll_decision') or decision),
                str(timer.get('last_poll_reason') or ''))
    return TimerCheckOutcome('ready', str(timer.get('last_poll_reason') or ''),
                             _execute_continuation(timer))


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
                outcome = await run_blocking(_execute_continuation, current)
                if outcome.kind != 'retry':
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

            decision = _poll_decision(ready, parse_error, poll_model)
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
                logger.info('[Timer:%s] ✅ Conditions met — delivering continuation', tid)
                outcome = await run_blocking(_execute_continuation, current)
                if outcome.kind != 'retry':
                    logger.info('[Timer:%s] Continuation settled: %s %s', tid,
                                outcome.kind, (outcome.task_id or outcome.queue_id)[:8])
                    break
                latest = await run_blocking(_get_timer_row, tid, user_id=user_id)
                if not latest or latest.get('status') != 'active':
                    logger.info('[Timer:%s] Continuation retired with status=%s',
                                tid, (latest or {}).get('status', 'deleted'))
                    break
                logger.info('[Timer:%s] Delivery lane unavailable; '
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

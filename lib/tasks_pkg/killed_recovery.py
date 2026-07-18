"""lib/tasks_pkg/killed_recovery.py — Auto-recover OS-killed chat turns.

The incident: in a shared cgroup the kernel OOM killer SIGKILLs us for a
NEIGHBOUR's memory pressure. ``recover_stale_tasks_on_startup`` restores the
interrupted assistant reply and (since the shutdown-marker landed) TAGS it
``interruptedReason='killed'`` — but a tag alone is inert: the turn just sits as
a dead interrupted bubble, exactly the state it was in before. This module
makes the tag ACTIONABLE: a killed turn is automatically re-dispatched so the
interrupted work actually completes, while a ``manual`` stop is left alone.

Mirrors the autopilot crash-resume shape (``resume_armed_autopilot_after_crash``):
a boot-time sweep over the durable recovered set that re-kicks via the standard
``create_task`` + ``spawn_task`` primitives (same ones ``dispatch_next_queued``
and ``kick_autopilot`` use). The turn is re-run as a REGENERATE (``excludeLast``)
so the partial killed answer is discarded and the model produces a fresh answer
to the last user turn.

TWO loop-protection guards against the restart-storm this incident WAS (3,286
process starts in one evening — blindly re-firing every killed billed LLM turn
would be a thundering herd that worsens the OOM and re-kills the same turns
forever):

  1. **Per-turn attempt cap.** A ``recoverAttempts`` counter is kept in the
     conversation's ``settings._killedRecovery`` keyed on the STABLE user-turn
     identity (``_msgId``/``timestamp`` of the last user message) — NOT the
     transient assistant message, which is recreated on every re-dispatch and
     would reset the counter to 1 each time = an infinite loop. After
     ``KILLED_RECOVERY_MAX_ATTEMPTS`` the turn degrades to
     ``interruptedReason='killed_exhausted'`` (surfaced for MANUAL resume,
     never auto-fired again).

  2. **Storm stand-down.** When the shutdown-marker reports a restart storm
     (boots arriving too fast), recovery does NOT re-dispatch anything — the
     turns stay tagged ``killed`` (visible) and are picked up on a later,
     calm boot.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import Any

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Max auto-recovery attempts for a single turn before it degrades to
# "surfaced for manual resume". Env-tunable; default 3 (one original kill +
# two auto-retries is plenty to ride out a transient neighbour-OOM blip
# without ever becoming a re-fire loop).
KILLED_RECOVERY_MAX_ATTEMPTS = int(
    os.environ.get('TOFU_KILLED_RECOVERY_MAX_ATTEMPTS', '3') or 3)

# Concurrency cap on SIMULTANEOUS killed-recovery carriers. The incident this
# module recovers from is a SHARED-cgroup OOM: re-firing every killed turn at
# once (the durable scan can surface 6+, one a 650k-token whale) is a
# synchronized memory burst that re-trips the very OOM that killed us. So the
# re-dispatch is bounded — at most this many carriers run concurrently; the rest
# are deferred to a drain daemon that releases them as slots free. Env-tunable;
# default 2 (one heavy turn plus headroom, safe on a contended cgroup).
KILLED_RECOVERY_MAX_CONCURRENT = int(
    os.environ.get('TOFU_KILLED_RECOVERY_MAX_CONCURRENT', '2') or 2)

# Gentle boot ramp: how many carriers to dispatch INLINE at boot before handing
# the rest to the drain daemon. Default 1 — a single heavy turn at boot is far
# safer on a shared cgroup than a synchronized burst; the drain adds the 2nd
# only once the 1st is live (and holding its slot), so the cap fills gradually.
KILLED_RECOVERY_INLINE_BOOT_DISPATCH = int(
    os.environ.get('TOFU_KILLED_RECOVERY_INLINE_BOOT', '1') or 1)

# Drain daemon poll cadence (seconds): how often it re-checks for a free slot
# while the deferred queue is non-empty. Env-tunable; default 15s.
KILLED_RECOVERY_DRAIN_POLL_SECS = float(
    os.environ.get('TOFU_KILLED_RECOVERY_DRAIN_POLL_SECS', '15') or 15)

# interruptedReason values this module reads/writes.
REASON_KILLED = 'killed'
REASON_EXHAUSTED = 'killed_exhausted'

# Durable-scan recency window. A killed turn is by definition recent (a crash
# interrupted it in the current operational window); a conv untouched for longer
# is stale — the user has moved on, and blindly re-firing a billed turn from
# weeks ago is undesirable. Bounding the scan by updated_at also keeps the
# (un-indexable ``CAST(messages AS TEXT) LIKE``) full-scan cheap on a large DB.
# Env-tunable; default 7 days.
KILLED_SCAN_MAX_AGE_SECS = int(
    os.environ.get('TOFU_KILLED_SCAN_MAX_AGE_SECS', str(7 * 24 * 3600)) or (7 * 24 * 3600))

# Error kinds that mean the RE-DISPATCH ITSELF failed before the model was
# reached (a recovery-internal fault — a config-build bug, a message-assembly
# error, an unhandled backend exception), as opposed to a genuine model/API
# outcome (rate limit, quota, permission, context-overflow, filter, …). Only
# these preserve the turn's 'killed' recoverability when a recovery carrier
# FATALs — a real model error is a COMPLETED turn (the model answered, with an
# error) and stays terminal for the user to regenerate. This is deliberately a
# TINY allow-list, not "everything non-transient": a recovery-internal fault is
# our bug to retry; a model verdict is not.
RECOVERY_INTERNAL_FATAL_KINDS = frozenset({'internal', 'generic'})


def _stop_requested(stop_event) -> bool:
    """True when a shutdown Event is present AND set. Fail-open to False."""
    try:
        return stop_event is not None and stop_event.is_set()
    except Exception as e:
        logger.debug('[KilledRecovery] stop-event probe failed, assuming not set: %s', e)
        return False


def _interruptible_wait(secs: float, stop_event) -> None:
    """Sleep ``secs``, but return EARLY if ``stop_event`` is set.

    When a shutdown Event is supplied the drain daemon must not stall for the
    full poll interval (up to 15s) after shutdown — ``Event.wait`` wakes the
    moment the flag is set. With no Event this is a plain ``time.sleep`` (legacy
    callers unchanged).
    """
    if stop_event is not None:
        try:
            stop_event.wait(secs)
            return
        except Exception as e:
            logger.debug('[KilledRecovery] stop-event wait failed, falling back to sleep: %s', e)
    time.sleep(secs)


def is_recovery_internal_fatal(error) -> bool:
    """True if ``error`` (a typed envelope) is a recovery-INTERNAL FATAL.

    Used by the orchestrator: when a carrier marked ``_killed_recovery`` FATALs
    with one of these kinds, the model was never reached, so the turn should
    stay recoverable (`killed`) rather than being downgraded to a terminal
    error. Any other kind (ratelimit/quota/permission/prompt_too_long/…) is a
    real model outcome and remains terminal.
    """
    if not isinstance(error, dict):
        # A non-envelope error on a recovery carrier is an unclassified backend
        # fault → treat as internal (recoverable). Fail toward re-recovery.
        return True
    return error.get('kind') in RECOVERY_INTERNAL_FATAL_KINDS


def _user_turn_key(messages: list[dict]) -> str | None:
    """Stable identity of the last user turn (survives assistant re-dispatch).

    Keying the attempt counter on this — not the assistant message — is the
    load-bearing loop guard: a re-dispatch creates a NEW assistant message, so
    a counter stored on the assistant would reset every attempt and never hit
    the cap.
    """
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get('role') == 'user':
            key = m.get('_msgId') or m.get('timestamp')
            return str(key) if key not in (None, '') else None
    return None


def next_attempt(settings: dict, user_key: str | None) -> tuple[int, dict]:
    """Compute the next attempt number for ``user_key`` and the settings patch.

    Pure/deterministic (unit-tested). When the stored key matches ``user_key``
    the count increments; a different (or absent) key means a NEW turn → the
    count resets to 1. Returns ``(attempts, new_killed_recovery_dict)``.
    """
    prev = settings.get('_killedRecovery') if isinstance(settings, dict) else None
    if isinstance(prev, dict) and prev.get('key') == user_key and user_key is not None:
        attempts = int(prev.get('attempts', 0)) + 1
    else:
        attempts = 1
    return attempts, {'key': user_key, 'attempts': attempts, 'ts': int(time.time() * 1000)}


def decide(messages: list[dict], settings: dict, *, storm: bool) -> dict:
    """Decide what to do with a conv whose tail was tagged ``killed``.

    Pure decision (unit-tested — no DB, no dispatch). Returns::

        {
          'action': 'redispatch' | 'exhausted' | 'storm_hold' | 'skip',
          'attempts': int,
          'settings_patch': dict | None,   # merge into settings when present
          'tag': 'killed' | 'killed_exhausted' | None,  # interruptedReason to stamp
        }

    * ``skip``        — the tail is not a recoverable killed turn (defensive).
    * ``storm_hold``  — a restart storm is active: leave tagged ``killed``, do
                        NOT re-dispatch, do NOT consume an attempt.
    * ``exhausted``   — the per-turn cap is reached: degrade to
                        ``killed_exhausted`` (manual resume only).
    * ``redispatch``  — re-run the turn; the attempt counter is advanced.
    """
    if not messages:
        return {'action': 'skip', 'attempts': 0, 'settings_patch': None, 'tag': None}
    last = messages[-1]
    # Only an interrupted assistant tail tagged 'killed' is a candidate. A
    # 'manual' tag or a completed turn is left alone.
    if (not isinstance(last, dict) or last.get('role') != 'assistant'
            or last.get('interruptedReason') != REASON_KILLED):
        return {'action': 'skip', 'attempts': 0, 'settings_patch': None, 'tag': None}

    if storm:
        # Stand down entirely during a crash storm — do not burn an attempt on
        # a kill that wasn't this turn's fault; retry on a calm boot.
        return {'action': 'storm_hold', 'attempts': 0, 'settings_patch': None,
                'tag': None}

    user_key = _user_turn_key(messages)
    attempts, patch = next_attempt(settings if isinstance(settings, dict) else {}, user_key)
    if attempts > KILLED_RECOVERY_MAX_ATTEMPTS:
        # Cap reached — surface for manual resume, never auto-fire again.
        return {'action': 'exhausted', 'attempts': attempts,
                'settings_patch': patch, 'tag': REASON_EXHAUSTED}
    return {'action': 'redispatch', 'attempts': attempts,
            'settings_patch': patch, 'tag': REASON_KILLED}


def list_killed_turn_convs(limit: int = 500) -> list[str]:
    """DURABLE SCAN: conv ids whose ASSISTANT TAIL is tagged ``killed``.

    This is the AUTHORITATIVE candidate set for killed-turn recovery — mirroring
    the autopilot ``list_armed_autopilot_convs`` lesson: resume must key on a
    durable scan of persisted state, NEVER a proxy set. The boot-time
    ``recover_stale_tasks_on_startup`` collects only convs freshly recovered
    from a *running* task THIS boot; that proxy set MISSES a conv whose killed
    turn was already persisted (e.g. a prior recovery carrier that FATALed and
    wrote finishReason='error' while KEEPING interruptedReason='killed'). Those
    would be stranded forever. Scanning the durable tag closes that gap.

    A cheap SQL ``LIKE`` prefilter narrows to rows that mention the tag anywhere;
    Python then verifies the tag is on the TAIL specifically (a mid-history
    killed tag from an older turn must not re-fire a settled conversation).
    """
    out: list[str] = []
    # Recency floor (ms — conversations.updated_at is epoch-ms). Bounds the
    # un-indexable LIKE scan AND scopes recovery to the current operational
    # window (a killed turn is recent by definition).
    _floor_ms = int((time.time() - KILLED_SCAN_MAX_AGE_SECS) * 1000)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        # Cheap prefilter on the value substring only (separator-agnostic —
        # ``"interruptedReason":"killed"`` vs ``"interruptedReason": "killed"``
        # both contain ``"killed"``). The Python tail check below is the real
        # gate, so an over-broad prefilter is safe, just slightly less
        # selective. CAST(...AS TEXT) keeps it valid on PG jsonb columns too.
        # The updated_at>=floor bound uses the index to shrink the scan.
        rows = db.execute(
            "SELECT id, messages FROM conversations "
            "WHERE user_id=1 AND updated_at >= ? AND CAST(messages AS TEXT) LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (_floor_ms, '%"killed"%', limit)
        ).fetchall()
    except Exception as e:
        logger.warning('[KilledRecovery] durable killed-scan query failed: %s', e)
        return out
    for r in rows:
        try:
            messages = json.loads(r['messages'] or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        if not messages or not isinstance(messages[-1], dict):
            continue
        tail = messages[-1]
        if tail.get('role') == 'assistant' and tail.get('interruptedReason') == REASON_KILLED:
            out.append(r['id'])
    if out:
        logger.info('[KilledRecovery] durable scan found %d conv(s) with a '
                    'killed tail', len(out))
    return out


def _conv_has_live_task(conv_id: str) -> bool:
    """True if a non-VU task is already running for the conv (don't double-drive)."""
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            for t in tasks.values():
                if (t.get('convId') == conv_id and t.get('status') == 'running'
                        and not t.get('_vu_subtask')):
                    return True
    except Exception as e:
        logger.debug('[KilledRecovery] live-task check failed conv=%s: %s',
                     conv_id[:8], e)
    return False


def _count_live_killed_carriers() -> int:
    """How many killed-recovery carriers are currently running.

    Counts tasks flagged ``_killed_recovery`` with ``status=='running'`` — the
    live-slot occupancy the concurrency cap gates on. Reuses the same
    ``tasks``/``tasks_lock`` snapshot pattern as :func:`_conv_has_live_task`.
    Fail-open to 0 (never let a counting error wedge the drain).
    """
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            return sum(1 for t in tasks.values()
                       if t.get('_killed_recovery') and t.get('status') == 'running')
    except Exception as e:
        logger.debug('[KilledRecovery] live-carrier count failed: %s', e)
        return 0


def _context_weight(messages: list[dict]) -> int:
    """Cheap ascending-cost proxy for a conv's re-dispatch weight.

    The durable scan returns convs ``updated_at DESC`` — arbitrary w.r.t. cost,
    so a 650k-token whale can lead the burst and is the turn most likely to
    re-trigger OOM. Ordering the queue by this weight ASCENDING drains light
    turns first and defers the heaviest carrier to the calmest slot (fewest live
    peers). ``len(json)`` of the messages is a zero-extra-fetch proxy (we already
    hold the JSON at classify time); message count breaks ties.
    """
    try:
        return len(json.dumps(messages, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(messages or [])


def _redispatch_conv(conv_id: str) -> str | None:
    """Re-run the killed turn: build a fresh regenerate task and spawn it.

    Uses ``resolve_conv_config(is_active=False)`` to faithfully rebuild the
    runtime config from the conversation's STORED settings (model, tools,
    systemPrompt, project paths — the config that produced the killed turn),
    and ``excludeLast=True`` so the partial killed assistant answer is dropped
    and the model regenerates the answer to the last user turn. Returns the new
    task id, or ``None`` when there is nothing to dispatch / a task is live.
    """
    if _conv_has_live_task(conv_id):
        logger.info('[KilledRecovery] conv=%s already has a live task — skipping '
                    're-dispatch', conv_id[:8])
        return None

    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT settings FROM conversations WHERE id=? AND user_id=1', (conv_id,)
    ).fetchone()
    try:
        settings = json.loads(row['settings'] or '{}') if row else {}
    except (json.JSONDecodeError, TypeError):
        settings = {}

    from lib.conv_config import resolve_conv_config
    # Inactive resolution: read the conversation's OWN stored settings (the UI
    # toolbar globals are irrelevant on a headless boot-time resume). Supply
    # server_defaults so no field resolves to None — WITHOUT it, maxTokens comes
    # back None (there is no toolbar/override on a boot-time resume) and the turn
    # FATALs at build_body → _clamp_max_tokens (``min(None, int)``). Mirror the
    # send path: a server model fallback + the standard 128000 completion cap.
    import lib as _lib
    server_defaults = {
        'serverModel': getattr(_lib, 'LLM_MODEL', '') or '',
        'maxTokens': 128000,
    }
    cfg = resolve_conv_config(conv_settings=settings, server_defaults=server_defaults,
                              is_active=False)
    cfg['excludeLast'] = True          # regenerate — discard the partial answer
    cfg['autopilot'] = False           # autopilot resume is a separate path
    cfg['_killedRecovery'] = True       # provenance marker for downstream/UI

    from lib.tasks_pkg import create_task, spawn_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    api_messages = build_api_messages_from_db(conv_id, cfg, exclude_last=True)
    if not api_messages:
        logger.warning('[KilledRecovery] conv=%s no API messages — cannot '
                       're-dispatch', conv_id[:8])
        return None

    task = create_task(conv_id, api_messages, cfg)
    task['_killed_recovery'] = True
    logger.info('[KilledRecovery] conv=%s re-dispatching killed turn → carrier %s',
                conv_id[:8], task['id'][:8])
    spawn_task(task)
    return task['id']


def restamp_killed_after_internal_fatal(task: dict) -> bool:
    """Re-tag a conv's assistant tail ``killed`` after a recovery-internal FATAL.

    Called by the orchestrator when a ``_killed_recovery`` carrier FATALs from a
    recovery-internal cause (the model was never reached). Re-stamping the tail
    ``interruptedReason='killed'`` (and NOT persisting the error turn) keeps the
    turn in the recoverable set so the next CALM boot re-dispatches it — bounded
    by the per-turn attempt counter, which was already advanced BEFORE this
    dispatch, so this cannot loop past the cap. Returns True on a successful
    re-stamp.

    If the attempt counter is already at/over the cap, degrade to
    ``killed_exhausted`` instead (surfaced for manual resume) — never re-arm a
    turn that has exhausted its budget.
    """
    conv_id = task.get('convId') or ''
    if not conv_id:
        return False
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return False
        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError):
            settings = {}
        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError):
            messages = []
        if not messages or not isinstance(messages[-1], dict):
            return False
        if messages[-1].get('role') != 'assistant':
            return False

        # Respect the cap: if this turn has already used its budget, do NOT
        # re-arm — degrade to killed_exhausted (manual resume only).
        kr = settings.get('_killedRecovery') if isinstance(settings, dict) else None
        attempts = int(kr.get('attempts', 0)) if isinstance(kr, dict) else 0
        if attempts >= KILLED_RECOVERY_MAX_ATTEMPTS:
            messages[-1]['interruptedReason'] = REASON_EXHAUSTED
            tag = REASON_EXHAUSTED
        else:
            messages[-1]['interruptedReason'] = REASON_KILLED
            messages[-1]['finishReason'] = 'interrupted'
            # Drop any partial/empty error content the FATAL left behind.
            messages[-1].pop('error', None)
            tag = REASON_KILLED
        db.execute(
            'UPDATE conversations SET messages=? WHERE id=? AND user_id=1',
            (json.dumps(messages, ensure_ascii=False), conv_id))
        db.commit()
        audit_log('killed_recovery_internal_fatal_restamp', conv_id=conv_id,
                  task_id=task.get('id', ''), tag=tag, attempts=attempts)
        return True
    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'warning', e,
                              f'[KilledRecovery] re-stamp after internal FATAL failed conv={conv_id[:8]}')
        return False


def _dispatch_one(conv_id: str, db, *, storm: bool) -> str:
    """Re-read → decide → persist the attempt counter → re-dispatch ONE conv.

    The single authoritative dispatch step, shared by the inline boot path AND
    the drain daemon. The attempt-counter persist lives HERE, coupled to the
    ACTUAL dispatch — so a DEFERRED conv burns NO attempt until this fires (the
    non-negotiable). Re-reading fresh also makes a deferred dispatch robust: by
    the time the drain reaches a conv it may have been completed by a live task
    or re-killed, and ``decide`` re-evaluates against current state.

    Returns one of ``'redispatched' | 'exhausted' | 'storm_held' | 'skipped'``.
    """
    row = db.execute(
        'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)
    ).fetchone()
    if not row:
        return 'skipped'
    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError):
        settings = {}
    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError):
        messages = []

    verdict = decide(messages, settings, storm=storm)
    action = verdict['action']
    if action == 'storm_hold':
        return 'storm_held'
    if action == 'skip':
        return 'skipped'

    # Persist the attempt counter + any degrade tag BEFORE the spawn, so a
    # re-kill of the resume carrier still sees the advanced count (idempotent,
    # loop-safe even across an immediate re-crash).
    patch = verdict.get('settings_patch')
    if patch is not None:
        settings['_killedRecovery'] = patch
    if verdict.get('tag') == REASON_EXHAUSTED and messages:
        messages[-1]['interruptedReason'] = REASON_EXHAUSTED
        db.execute(
            'UPDATE conversations SET settings=?, messages=? WHERE id=? AND user_id=1',
            (json.dumps(settings, ensure_ascii=False),
             json.dumps(messages, ensure_ascii=False), conv_id))
    else:
        db.execute(
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
            (json.dumps(settings, ensure_ascii=False), conv_id))
    db.commit()

    if action == 'exhausted':
        # verdict['attempts'] is the DECISION counter — it reaches cap+1 on the
        # degrade decision (the +1 IS the exhausted decision, not a re-dispatch).
        # Report the re-dispatches actually MADE (== cap) + the cap, so the line
        # never reads "after 4 attempts" when the cap is 3.
        _made = min(verdict['attempts'], KILLED_RECOVERY_MAX_ATTEMPTS)
        logger.error('[KilledRecovery] conv=%s EXHAUSTED after %d re-dispatch '
                     'attempt(s) (cap=%d) — degraded to killed_exhausted '
                     '(manual resume only)',
                     conv_id[:8], _made, KILLED_RECOVERY_MAX_ATTEMPTS)
        audit_log('killed_recovery_exhausted', conv_id=conv_id,
                  attempts_made=_made, cap=KILLED_RECOVERY_MAX_ATTEMPTS)
        return 'exhausted'

    # action == 'redispatch'
    new_tid = _redispatch_conv(conv_id)
    if new_tid:
        audit_log('killed_recovery_redispatch', conv_id=conv_id,
                  task_id=new_tid, attempt=verdict['attempts'])
        return 'redispatched'
    return 'skipped'


def _drain_deferred(deferred: list[str], *, storm: bool, stop_event=None) -> None:
    """Drain the deferred re-dispatch queue, honouring the concurrency cap.

    Runs on a daemon thread. Polls every ``KILLED_RECOVERY_DRAIN_POLL_SECS``:
    when live killed-carriers are below ``KILLED_RECOVERY_MAX_CONCURRENT`` it
    dispatches the next deferred conv (queue is pre-ordered ascending-weight, so
    the heaviest turn drains LAST, into the calmest slot), then jitters briefly
    before the next. A heavy turn naturally holds its slot longer, so the cap
    self-throttles to real memory pressure instead of a fixed rate.

    A conv is popped BEFORE its dispatch attempt, so the queue always drains
    even if a conv turns out ``skipped``/``exhausted`` — the loop terminates. On
    a re-kill mid-drain the un-dispatched convs keep their ``killed`` tag and are
    re-found by the next boot's durable scan (they burned no attempt here).

    SHUTDOWN GATE: ``stop_event`` (the server's ``_shutdown_requested`` Event) is
    checked BEFORE every slot-poll AND before every dispatch/DB touch, so once
    shutdown is requested the daemon stops immediately — it never spawns a fresh
    carrier or calls ``get_thread_db`` while PG is being stopped (the
    ``FATAL: the database system is shutting down`` cascade started HERE). The
    poll wait is interruptible so shutdown never blocks up to a full poll.
    """
    logger.info('[KilledRecovery] drain daemon started: %d deferred carrier(s), '
                'cap=%d poll=%.0fs', len(deferred),
                KILLED_RECOVERY_MAX_CONCURRENT, KILLED_RECOVERY_DRAIN_POLL_SECS)
    while deferred:
        if _stop_requested(stop_event):
            logger.info('[KilledRecovery] drain daemon stopping — shutdown '
                        'requested (%d carrier(s) left un-dispatched, kept '
                        'killed for next boot)', len(deferred))
            return
        live = _count_live_killed_carriers()
        if live >= KILLED_RECOVERY_MAX_CONCURRENT:
            _interruptible_wait(KILLED_RECOVERY_DRAIN_POLL_SECS, stop_event)
            continue
        # Re-check AFTER the wait — shutdown may have arrived while we slept.
        if _stop_requested(stop_event):
            logger.info('[KilledRecovery] drain daemon stopping — shutdown '
                        'requested (%d carrier(s) left un-dispatched)',
                        len(deferred))
            return
        conv_id = deferred.pop(0)
        try:
            db = get_thread_db(DOMAIN_CHAT)   # thread-local — must acquire here
            outcome = _dispatch_one(conv_id, db, storm=storm)
            logger.info('[KilledRecovery] drain dispatched conv=%s → %s '
                        '(%d remaining)', conv_id[:8], outcome, len(deferred))
        except Exception as e:
            from lib.database import log_db_finalize_error
            log_db_finalize_error(logger, 'warning', e,
                                  f'[KilledRecovery] drain dispatch failed conv={conv_id[:8]}')
        # Small jitter between dispatches so multiple carriers never spin up on
        # the exact same tick (per-host serialize-with-jitter).
        if deferred:
            _interruptible_wait(random.uniform(0.5, 1.5), stop_event)
    logger.info('[KilledRecovery] drain daemon finished — queue empty')


def _spawn_drain_daemon(deferred: list[str], *, storm: bool,
                        stop_event=None) -> threading.Thread:
    """Launch the drain daemon thread (daemon=True → dies with the process)."""
    t = threading.Thread(target=_drain_deferred,
                         kwargs={'deferred': deferred, 'storm': storm,
                                 'stop_event': stop_event},
                         daemon=True, name='killed-recovery-drain')
    t.start()
    return t


def run_killed_recovery(conv_ids: list[str], *, storm: bool = False,
                        stop_event=None) -> dict[str, Any]:
    """Auto-recover every conv whose recovered tail was tagged ``killed``.

    Called by ``recover_stale_tasks_on_startup`` AFTER its commit (so the
    re-dispatched carrier sees the merged, tagged messages).

    CONCURRENCY-BOUNDED (the shared-cgroup OOM fix): re-firing every killed turn
    at once is a synchronized memory burst that re-trips the OOM. So:

      1. Classify all candidates (``decide`` — pure). ``storm_hold`` / ``skip``
         are handled inline (no slot); ``exhausted`` is persisted+degraded inline
         (a terminal degrade, not a dispatch).
      2. Order the redispatch-eligible convs by ASCENDING context weight, so the
         light turns go first and the heaviest whale is deferred to the calmest
         slot (fewest live peers) — the turn most likely to re-trigger OOM runs
         when the burst has cleared.
      3. Dispatch only ``KILLED_RECOVERY_INLINE_BOOT_DISPATCH`` (default 1)
         carriers INLINE at boot, bounded further by the live-carrier headroom
         under ``KILLED_RECOVERY_MAX_CONCURRENT``; hand the rest to a drain
         daemon that releases them as slots free.

    Best-effort per conv — one failure never aborts the sweep. Returns
    ``{redispatched, exhausted, storm_held, skipped, deferred, conv_ids:[...]}``.
    """
    summary = {'redispatched': 0, 'exhausted': 0, 'storm_held': 0,
               'skipped': 0, 'deferred': 0, 'conv_ids': []}
    if not conv_ids:
        return summary
    if _stop_requested(stop_event):
        logger.info('[KilledRecovery] recovery skipped — shutdown requested')
        return summary

    db = get_thread_db(DOMAIN_CHAT)
    eligible: list[tuple[int, str]] = []   # (context_weight, conv_id)
    for conv_id in conv_ids:
        if not conv_id:
            continue
        try:
            row = db.execute(
                'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if not row:
                summary['skipped'] += 1
                continue
            try:
                settings = json.loads(row['settings'] or '{}')
            except (json.JSONDecodeError, TypeError):
                settings = {}
            try:
                messages = json.loads(row['messages'] or '[]')
            except (json.JSONDecodeError, TypeError):
                messages = []

            verdict = decide(messages, settings, storm=storm)
            action = verdict['action']

            if action == 'storm_hold':
                summary['storm_held'] += 1
                logger.warning('[KilledRecovery] conv=%s held — restart storm '
                               'active, will retry on a calm boot', conv_id[:8])
                continue
            if action == 'skip':
                summary['skipped'] += 1
                continue
            if action == 'exhausted':
                # Terminal degrade — persist tag+counter now, no dispatch, no slot.
                out = _dispatch_one(conv_id, db, storm=storm)
                summary['exhausted' if out == 'exhausted' else 'skipped'] += 1
                continue

            # redispatch-eligible — queue for weight-ordered bounded dispatch.
            eligible.append((_context_weight(messages), conv_id))
        except Exception as e:
            logger.warning('[KilledRecovery] classify failed for conv=%s: %s',
                           conv_id[:8], e, exc_info=True)

    if not eligible:
        return summary

    # Ascending weight → lightest first, heaviest deferred to the calmest slot.
    eligible.sort(key=lambda w_c: w_c[0])
    ordered = [conv_id for _, conv_id in eligible]

    # Inline boot budget = min(gentle-ramp count, live-carrier headroom).
    live = _count_live_killed_carriers()
    inline_budget = min(KILLED_RECOVERY_INLINE_BOOT_DISPATCH,
                        max(0, KILLED_RECOVERY_MAX_CONCURRENT - live))
    deferred: list[str] = []
    inline_n = 0
    for conv_id in ordered:
        if _stop_requested(stop_event):
            # Shutdown mid-inline-dispatch: stop spawning; the rest keep their
            # killed tag and are re-found by the next boot's durable scan.
            logger.info('[KilledRecovery] inline dispatch halted — shutdown '
                        'requested (%d candidate(s) left)',
                        len(ordered) - inline_n)
            deferred = []
            break
        if inline_n < inline_budget:
            try:
                out = _dispatch_one(conv_id, db, storm=storm)
            except Exception as e:
                logger.warning('[KilledRecovery] inline dispatch failed conv=%s: %s',
                               conv_id[:8], e, exc_info=True)
                out = 'skipped'
            if out == 'redispatched':
                summary['redispatched'] += 1
                summary['conv_ids'].append(conv_id)
                inline_n += 1
            elif out == 'exhausted':
                summary['exhausted'] += 1
            else:
                summary['skipped'] += 1
        else:
            deferred.append(conv_id)

    if deferred:
        summary['deferred'] = len(deferred)
        logger.info('[KilledRecovery] %d carrier(s) dispatched inline, %d deferred '
                    'to drain daemon (heaviest last)', inline_n, len(deferred))
        _spawn_drain_daemon(deferred, storm=storm, stop_event=stop_event)
    return summary


__all__ = [
    'KILLED_RECOVERY_MAX_ATTEMPTS', 'KILLED_RECOVERY_MAX_CONCURRENT',
    'KILLED_RECOVERY_INLINE_BOOT_DISPATCH', 'KILLED_RECOVERY_DRAIN_POLL_SECS',
    'REASON_KILLED', 'REASON_EXHAUSTED',
    'RECOVERY_INTERNAL_FATAL_KINDS', 'is_recovery_internal_fatal',
    'restamp_killed_after_internal_fatal',
    'next_attempt', 'decide', 'run_killed_recovery',
]

"""Registry maintenance — TTL cleanup, memory shedding under pressure, and the
stuck-task reaper (dual-clock wedged-task detection + full finalization).
"""

import json
import time

from lib.agent_core.events import EventType, build_event
from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger

from lib.tasks_pkg.manager._state import (
    _chat_runtime,
    _conv_latest_task,
    _conv_latest_task_lock,
    tasks,
    tasks_lock,
)
from lib.tasks_pkg.manager._events import append_event
from lib.tasks_pkg.manager._persist import _upsert_task_row, build_result_meta
from lib.tasks_pkg.manager._sync import (
    _dispatch_queued_message,
    _sync_result_to_conversation,
)

logger = get_logger(__name__)


def cleanup_old_tasks():
    """Drop finished tasks past TTL and prune the conv-latest-task index.

    Notes:
      - Legacy semantics removed tasks based on ``created_at`` regardless of
        finish time. TaskRuntime uses ``finished_at`` instead, which is more
        accurate (a task that ran for 30 minutes shouldn't be wiped 30 minutes
        after starting if it's still streaming).
      - We snapshot the task ids ABOUT to be cleaned BEFORE calling
        cleanup_stale() so we can prune the conv-latest-task index too.
    """
    now = time.time()
    finished_ids: set = set()
    with _chat_runtime._lock:
        for tid, t in _chat_runtime._tasks.items():
            if t['status'] in ('done', 'error', 'aborted'):
                finished_at = t.get('finished_at')
                # Fall back to created_at for tasks that don't have finished_at
                # (e.g. tests that mark status='done' directly without finish()).
                ref_t = finished_at if finished_at else t.get('created_at', 0)
                if now - ref_t > _chat_runtime.ttl:
                    finished_ids.add(tid)
    n = _chat_runtime.cleanup_stale()
    # Clean up _conv_latest_task entries whose tasks were just removed
    if finished_ids:
        with _conv_latest_task_lock:
            stale_convs = [cid for cid, tid in _conv_latest_task.items()
                           if tid in finished_ids]
            for cid in stale_convs:
                del _conv_latest_task[cid]
    if n:
        logger.debug('[Manager] cleanup_old_tasks removed %d tasks', n)
    # ★ Stuck-task backstop (rides the same tick). cleanup_stale only evicts
    #   FINISHED tasks, so a purely-wedged running task (never superseded) would
    #   otherwise live forever with no terminal state. See reap_stuck_running_tasks.
    try:
        reap_stuck_running_tasks()
    except Exception as e:
        logger.warning('[Manager] reap_stuck_running_tasks failed: %s', e, exc_info=True)
    # ★ Queue-lease reaper (rides the same tick, pt_4ab943fa): reclaim leases
    #   orphaned by a crash/exception mid-dispatch and re-dispatch them, so a
    #   queued human message is retried automatically instead of silently lost.
    try:
        from lib.message_queue import reap_expired_queue_leases
        reap_expired_queue_leases()
    except Exception as e:
        logger.warning('[Manager] reap_expired_queue_leases failed: %s', e, exc_info=True)
    # ★ Result-level reconciliation (DB, not registry). The reaper above can
    #   only see tasks still IN MEMORY; a carrier that finished and left the
    #   registry without a terminal write is invisible to it. See
    #   find_orphan_running_results.
    try:
        report_orphan_running_results()
    except Exception as e:
        logger.warning('[Manager] report_orphan_running_results failed: %s', e, exc_info=True)


def _malloc_trim() -> bool:
    """Ask glibc to return free heap arenas to the OS. Returns True on success.

    Python's allocator frees objects back to glibc, but glibc keeps the arenas
    mapped (RSS stays high) until ``malloc_trim`` releases them. Under memory
    pressure this is what actually turns a freed ``task['messages']`` into a
    lower RSS the kernel OOM-scorer sees. No-op / best-effort off glibc (musl,
    macOS) — mirrors the libc.so.6 ctypes pattern already used in server.py.
    """
    try:
        import ctypes
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        libc.malloc_trim(ctypes.c_size_t(0))
        return True
    except (OSError, AttributeError, Exception) as e:  # noqa: BLE001 - best-effort
        logger.debug('[Manager] malloc_trim unavailable/failed: %s', e)
        return False


def shed_memory_under_pressure() -> dict:
    """Aggressively reclaim process memory — the memory-watchdog back-pressure
    valve (lever C for the shared-cgroup OOM).

    When the cgroup nears its limit AND our own RSS is a meaningful share of
    it, this is called to shed memory BEFORE the kernel OOM-killer fires:

      1. Evict EVERY terminal (done/error/aborted) chat task immediately
         (``cleanup_stale(max_age=0)``) — bypassing the ttl=3600s retention.
         The durable result already lives in the DB; a late poller rebuilds
         from ``task_results``. This drops the released-``messages`` tasks and
         everything else finished, right now.
      2. ``gc.collect()`` to break any reference cycles pinning those dicts.
      3. ``malloc_trim(0)`` so glibc returns the freed arenas to the OS and RSS
         actually falls (without this the heap stays mapped and the OOM-scorer
         still sees us as the fattest process).

    Idempotent and best-effort — never raises. Returns a diagnostic dict
    ``{evicted, gc_collected, trimmed}`` for the caller to log.
    """
    import gc
    evicted = 0
    # Snapshot terminal ids for the conv-latest-task prune (mirror cleanup_old_tasks).
    finished_ids: set = set()
    try:
        with _chat_runtime._lock:
            for tid, t in _chat_runtime._tasks.items():
                if t['status'] in ('done', 'error', 'aborted'):
                    finished_ids.add(tid)
        evicted = _chat_runtime.cleanup_stale(max_age=0)
        if finished_ids:
            with _conv_latest_task_lock:
                for cid in [c for c, tid in _conv_latest_task.items()
                            if tid in finished_ids]:
                    del _conv_latest_task[cid]
    except Exception as e:
        logger.warning('[Manager] shed: terminal-task eviction failed: %s', e,
                       exc_info=True)
    collected = 0
    try:
        collected = gc.collect()
    except Exception as e:
        logger.debug('[Manager] shed: gc.collect failed: %s', e)
    trimmed = _malloc_trim()
    logger.info('[Manager] shed_memory_under_pressure: evicted=%d gc=%d trim=%s',
                evicted, collected, trimmed)
    return {'evicted': evicted, 'gc_collected': collected, 'trimmed': trimmed}


# Age (seconds) after which a running task that has produced ZERO output
# (no events, no content, no thinking) is considered wedged and force-failed.
# Env-tunable; 0 disables the backstop. Default 30 min — comfortably longer
# than any legitimate pre-first-token wait (queueing, long tool prep).
def _stuck_task_max_silent_secs() -> int:
    import os
    try:
        return int(os.environ.get('TOFU_STUCK_TASK_MAX_SILENT_SECS', '') or '1800')
    except (ValueError, TypeError) as e:
        logger.debug('[Manager] TOFU_STUCK_TASK_MAX_SILENT_SECS parse failed, using fallback: %s', e)
        return 1800


def reap_stuck_running_tasks() -> int:
    """Force-terminate running tasks that are WEDGED (generalized dual-clock).

    Targets the "produced-then-wedged zombie" the supersede/abort path does NOT
    cover: a task that emitted a first token (or a whole tool round) and THEN
    had its worker thread wedge — stuck in a socket read on a dead host, a hung
    MCP call, a stalled browser action, an LLM stream that never closes. Left
    alone it stays ``status='running'`` in memory forever; ``/api/chat/active``
    keeps reporting it running so the frontend reconnects an SSE that never
    delivers a byte, the sidebar "busy" dot never clears, and — never having
    finalized — it has no terminal ``task_results`` row, so a post-restart poll
    404s and the turn is lost.

    The ORIGINAL discriminator gated on ``content=='' AND thinking=='' AND
    n_events==0`` — it could ONLY see the never-produced-anything case, so the
    produced-then-wedged zombie fell straight through. The generalized
    discriminator uses TWO liveness clocks that must BOTH be stale to reap:

      • ``_t_last_event``      — bumped by every emitted event (deltas /
                                 keepalive / retry / waiting_model phase). A
                                 rate-limited-but-alive turn keeps emitting
                                 retry phases → stays fresh.
      • ``_dispatch_heartbeat`` — refreshed while a dispatch / cooldown-wait /
                                 tool call is genuinely in-flight, AND while
                                 blocking on human input (ask_user / approval /
                                 stdin poll loop). A turn stuck in a live socket
                                 read emits no event but IS heartbeating → never
                                 reaped; a human-waiting task likewise stays
                                 fresh via its poll loop.

    Requiring BOTH stale is exactly the signal a client-side poll CANNOT recover
    (poll only sees ``status='running'``, indistinguishable from a slow turn),
    so the reap MUST be server-side. Both clocks fall back to ``created_at`` so
    the legacy zero-output case is a strict subset (still caught).

    Marks the task aborted (reason ``stuck_no_progress``) and runs the FULL
    finalizer (``_finalize_reaped_stuck_task``: terminal floor → DONE(error)
    SSE → conv sync → queue drain) so a poll resolves terminally, the conv is
    detached from the dead stream, and any turn queued behind it is drained.
    Returns the number of tasks reaped.
    """
    max_silent = _stuck_task_max_silent_secs()
    if max_silent <= 0:
        return 0
    now = time.time()
    stuck = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('status') != 'running' or t.get('aborted'):
                continue
            created = t.get('created_at', now)
            # Both clocks fall back to created_at so a task that never set them
            # (legacy zero-output) is a subset: its clocks == created_at.
            last_event = t.get('_t_last_event', created)
            heartbeat = t.get('_dispatch_heartbeat', created)
            event_silent = now - last_event
            dispatch_silent = now - heartbeat
            # Reap ONLY when BOTH clocks are stale past the threshold. Either
            # one fresh = alive (slow/rate-limited dispatch, or emitting events,
            # or a human-input wait keeping the heartbeat warm).
            if event_silent < max_silent or dispatch_silent < max_silent:
                continue
            had_output = bool((t.get('content') or '') or (t.get('thinking') or ''))
            if not had_output:
                try:
                    with t['events_lock']:
                        had_output = len(t['events']) > 0
                except Exception as e:
                    logger.debug('[reap] task %s has no readable events '
                                 'structure: %s', t.get('id', '?'), e)
                    had_output = False
            t['aborted'] = True
            t['_abort_timestamp'] = now
            t['_abort_reason'] = 'stuck_no_progress'
            t['status'] = 'error'
            t['_reap_had_output'] = had_output
            from lib.error_envelope import make_envelope as _make_env
            t['error'] = _make_env(
                'internal',
                detail=('Task made no progress for %d seconds and was '
                        'terminated as wedged.' % int(min(event_silent, dispatch_silent))),
                model=(t.get('config') or {}).get('model', '') or '',
                context='stuck-task-reaper',
                source='lib.tasks_pkg.manager',
            )
            t['finishReason'] = 'error'
            t['finished_at'] = now
            stuck.append(t)
    for t in stuck:
        logger.warning('[Task %s] conv=%s ⚠️ WEDGED — no event/dispatch progress for '
                       '%.0fs (had_output=%s), force-failed and finalizing',
                       t['id'][:8], (t.get('convId') or '')[:8],
                       now - t.get('created_at', now), t.get('_reap_had_output'))
        _finalize_reaped_stuck_task(t)
    if stuck:
        logger.warning('[Manager] reap_stuck_running_tasks force-failed %d wedged task(s)',
                       len(stuck))
    return len(stuck)


# Age (seconds) past which a ``task_results`` row still marked
# ``status='running'`` counts as ORPHANED. Env-tunable; 0 disables the scan.
# Default 1 h — a live turn re-writes its row every ~5 s (checkpoint), so an
# hour of silence on the row means no writer is left.
def _orphan_result_max_age_secs() -> int:
    import os
    try:
        return int(os.environ.get('TOFU_ORPHAN_RESULT_MAX_AGE_SECS', '') or '3600')
    except (ValueError, TypeError) as e:
        logger.debug('[Manager] TOFU_ORPHAN_RESULT_MAX_AGE_SECS parse failed, '
                     'using fallback: %s', e)
        return 3600


def find_orphan_running_results(limit: int = 200) -> list[dict]:
    """Return ``task_results`` rows wedged at ``status='running'`` (DB-level).

    The complement to :func:`reap_stuck_running_tasks`, which scans the
    IN-MEMORY registry and therefore *structurally cannot* see this class: a
    carrier (autopilot VU sub-task / inline reporter) is discarded from the
    registry the moment its synchronous run returns, and it never reaches
    ``persist_task_result`` (``_endpoint_managed=True`` suppresses the terminal
    write). Its last ``checkpoint_task_partial`` therefore leaves a row at
    ``status='running'`` that no in-memory pass will ever revisit.

    ⚠️ The intuitive predicate ``status='running' AND completed_at IS NOT NULL``
    does NOT work. ``_upsert_task_row`` (``_persist.py``) stamps ``completed_at``
    on EVERY write — the terminal one *and* the ~5 s running checkpoint — so the
    column means "last written at", not "finished at". Measured 2026-07-26:
    all 69 ``running`` rows had it non-NULL and 6 of those were healthy live
    turns, i.e. that predicate flags every in-flight task. The usable signal is
    STALENESS plus absence from the live registry:

      * ``completed_at`` older than :func:`_orphan_result_max_age_secs`, and
      * ``task_id`` absent from the in-memory registry.

    Read-only by design. A carrier row legitimately *ends* at
    ``status='running'`` — it succeeded, it simply has no terminal writer — so
    flipping these to ``error`` would record a failure that never happened.
    Choosing a terminal status for a finished carrier is a contract change, not
    a reconciliation, and belongs in its own change.

    Returns a list of ``{task_id, conv_id, age_secs}`` sorted oldest-first.
    """
    max_age = _orphan_result_max_age_secs()
    if max_age <= 0:
        return []
    from lib.database import DOMAIN_CHAT, get_thread_db
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - max_age * 1000
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT task_id, conv_id, completed_at FROM task_results "
            "WHERE status='running' AND completed_at IS NOT NULL "
            "  AND completed_at < ? ORDER BY completed_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    except Exception as e:
        logger.warning('[Manager] orphan-result scan failed: %s', e, exc_info=True)
        return []
    if not rows:
        return []
    # A task still in the registry is someone's live work (or is about to be
    # finalized) — never report it, regardless of how stale its ROW looks.
    with tasks_lock:
        live_ids = set(tasks.keys())
    out = []
    for r in rows:
        tid = r[0]
        if tid in live_ids:
            continue
        out.append({
            'task_id': tid,
            'conv_id': r[1] or '',
            'age_secs': int((now_ms - (r[2] or 0)) / 1000),
        })
    return out


def report_orphan_running_results() -> int:
    """Log the orphaned-``running`` rows found by :func:`find_orphan_running_results`.

    This is the state-machine self-consistency gate that replaces log-watching
    for "a turn is wedged": the SSE layer only notices at its 7200 s ceiling and
    reports ``status=running`` from the very state machine that is stuck, so it
    cannot be the detector. Returns the number of orphans found.
    """
    orphans = find_orphan_running_results()
    if not orphans:
        return 0
    oldest = orphans[0]
    logger.warning(
        '[Manager] ⚠️ %d task_results row(s) orphaned at status=running '
        '(stale >%ds, absent from the live registry). Oldest: task=%s conv=%s '
        'age=%ds. These have no terminal writer — a poll on them never '
        'resolves. Most are finished autopilot/inline CARRIERS, which have no '
        'terminal write path by design.',
        len(orphans), _orphan_result_max_age_secs(),
        oldest['task_id'][:8], (oldest['conv_id'] or '-')[:8], oldest['age_secs'],
    )
    return len(orphans)


def _write_stuck_terminal_floor(task) -> None:
    """Persist a terminal ``status='error'`` row for a reaped stuck task so a
    later poll (even post-restart) resolves terminally instead of 404.

    Best-effort; reuses the shared ``_upsert_task_row`` (keyed on task_id).
    """
    try:
        conv_id = task.get('convId', '') or ''
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='error',
                         error_json=error_json, tr_json=None, meta_json=meta_json)
        logger.debug('[Task %s] conv=%s Wrote stuck terminal floor to task_results',
                     task['id'][:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Task %s] Failed to write stuck terminal floor: %s',
                       task.get('id', '?')[:8], e, exc_info=True)



def _finalize_reaped_stuck_task(task) -> None:
    """Run the FULL terminal path for a reaped wedged task.

    A reaped task must undergo the SAME finalization as the happy path, not
    just a ``task_results`` floor. Writing only the floor (the old behaviour)
    avoided the 404-on-poll bug but left the conversation perpetually attached
    to a dead stream: the assistant error bubble never landed on
    ``conversations.messages``, ``settings.activeTaskId`` still pointed at the
    dead task, no terminal ``DONE(error)`` SSE fired, and any turn queued behind
    the wedged one was stranded (the completion-hook chain never runs for a
    reaped task).

    Steps (each independently guarded so one failure can't abort the rest):
      1. Terminal floor FIRST — a poll always resolves terminally, even if a
         later step throws.
      2. Terminal ``DONE(error)`` SSE — any still-connected client settles.
      3. ``_sync_result_to_conversation`` — appends/fills the assistant error
         bubble AND clears ``settings.activeTaskId`` (its normal side effect).
      4. ``_dispatch_queued_message`` — drains a turn queued behind the wedge.

    The reaper set ``_abort_reason='stuck_no_progress'``, which the anti-
    resurrection guard in ``_sync_result_to_conversation`` treats as an
    EXCEPTION (it owns the trailing user turn and is still the conv's latest
    task) so the error bubble is allowed to answer an unanswered user message.
    """
    tid = task.get('id', '?')[:8]
    conv_id = (task.get('convId') or '')[:8]

    # (1) Terminal floor first — poll resolves terminally regardless.
    _write_stuck_terminal_floor(task)

    # (2) Terminal DONE(error) SSE so a live client settles immediately.
    try:
        err_done = build_event(EventType.DONE,
                               error=task.get('error'), finishReason='error')
        if task.get('preset'):
            err_done['preset'] = task['preset']
        if task.get('model'):
            err_done['model'] = task['model']
        append_event(task, err_done)
    except Exception as e:
        logger.warning('[Task %s] reaped-finalize: DONE(error) SSE failed: %s',
                       tid, e, exc_info=True)

    # (3) Sync the assistant error bubble onto the conversation + clear the
    #     pinned activeTaskId (a normal side effect of the conv sync).
    try:
        _sync_result_to_conversation(task, build_result_meta(task))
    except Exception as e:
        logger.warning('[Task %s] conv=%s reaped-finalize: conv sync failed: %s',
                       tid, conv_id, e, exc_info=True)

    # (4) Drain any turn queued behind the wedged one.
    try:
        _dispatch_queued_message(task)
    except Exception as e:
        logger.warning('[Task %s] conv=%s reaped-finalize: queue drain failed: %s',
                       tid, conv_id, e, exc_info=True)

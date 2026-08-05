"""lib/message_queue.py — Unified priority turn-source queue for conversations.

The queue holds the *sources* of upcoming conversation turns, ordered by
priority.  Three kinds of source share one table:

  • ``real``          — a human message (highest priority).
  • ``workflow_step`` — a turn injected by an orchestration workflow
                        (medium priority; reserved for the workflow engine).
  • ``autopilot``     — a persistent armed-marker sentinel (lowest priority).

``real`` / ``workflow_step`` rows are *dispatchable*: when the active task
finishes, the highest-priority dispatchable row is dequeued and started as a
new task.  The ``autopilot`` row is NOT dispatched as a task — it is a flag
that the end-of-turn autopilot hook (:mod:`lib.tasks_pkg.autopilot`) consults
to decide whether the virtual user should take over.  It stays in the queue
(surviving page reloads) until the VU emits ``[VU: TASK_DONE]`` or the user
cancels it.

Because a human ``real`` row sorts ahead of the ``autopilot`` sentinel, a
message the user types while autopilot is armed is ALWAYS processed first;
autopilot only resumes once no dispatchable row remains.

This replaces the frontend-only ``pendingMessageQueue`` Map that was lost
on page refresh.

Dispatch durability (pt_4ab943fa): dequeue LEASES a row
(``leased_until``/``lease_task_id``) instead of deleting it. The delete lands
only after ``spawn_task`` succeeds; every failure path releases the lease; a
reaper (:func:`reap_expired_queue_leases`, riding the manager maintenance
tick) reclaims rows whose lease expired without a live task in the registry
and re-dispatches them. A crash or exception mid-dispatch therefore triggers
an automatic retry instead of silently losing the queued message.
"""

import json
import os
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# Lock for dispatch coordination (prevent double-dispatch races)
_dispatch_lock = threading.Lock()

# ── Turn-source kinds + their default priorities (lower = higher) ──
KIND_REAL = 'real'
KIND_PEER_MSG = 'peer_msg'
KIND_WORKFLOW = 'workflow_step'
KIND_AUTOPILOT = 'autopilot'

_PRIORITY_FOR_KIND = {
    KIND_REAL: 10,
    # A peer message from a sibling conversation is advisory — the target sees
    # it on its NEXT turn (dispatchable, never interrupts a live turn). It
    # sorts AFTER a human 'real' turn (so a human always wins) but BEFORE a
    # brain-dispatch 'workflow_step' kickoff.
    KIND_PEER_MSG: 40,
    KIND_WORKFLOW: 50,
    KIND_AUTOPILOT: 90,
}


def _priority_for_kind(kind: str) -> int:
    return _PRIORITY_FOR_KIND.get(kind, 100)


# ── Dispatch lease (pt_4ab943fa) ──
# How long a dequeued-but-not-yet-spawned row stays invisible to other drains.
# Must comfortably exceed the slowest in-dispatch step (auto-translate of a
# long queued message is an LLM call). Only a true process crash mid-dispatch
# ever waits out the full TTL — every failure path releases the lease
# immediately, and the success path deletes the row outright.
_QUEUE_LEASE_MS = 120 * 1000


def _reaper_max_dispatch_per_tick() -> int:
    """Max stranded-drain dispatches per reaper tick (default 4).

    A crash/restart can strand MANY conversations at once (each holding a
    queued human message). Draining them all in a single tick would spawn N
    tasks simultaneously and slam the LLM rate limit — the steady-state tick
    drains oldest-first, K per tick; the rest retry on the next tick.
    """
    try:
        return max(1, int(os.environ.get(
            'TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK', '') or '4'))
    except (ValueError, TypeError) as e:
        logger.debug('[Queue] TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK parse failed: %s', e)
        return 4


def _ensure_table():
    """Create the message_queue table if it doesn't exist (migration-safe)."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('''
            CREATE TABLE IF NOT EXISTS message_queue (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                config TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL DEFAULT 1,
                created_at BIGINT NOT NULL
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
        db.commit()
    except Exception as e:
        logger.warning('[Queue] _ensure_table failed (queue will be unusable): %s', e, exc_info=True)
        try:
            db.rollback()
        except Exception as re:
            logger.debug('[Queue] rollback after _ensure_table failure: %s', re)

# Auto-create table on module load (safe for existing DBs)
_table_ensured = False

def _maybe_ensure_table():
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True


def _existing_board_kickoff(db, conv_id: str, message_data: dict,
                            kind: str) -> str | None:
    """Queue id of an EXISTING kickoff for the same ``(conv_id, boardTaskId)``,
    or None when this row is not a duplicate (or not dedup-able at all).

    The structural floor under brain-dispatch de-duplication. Producer-side
    probes (``_epic_already_queued`` in ``sweep_dispatch`` / ``on_epic_posted``
    / ``on_epic_completed``) each have to REMEMBER to ask; this asks once, for
    every producer, present and future. That distinction is not theoretical:
    on 2026-07-28 conv ms4b67gmthqc17 accumulated 11 queued rows for 4 distinct
    epics because exactly ONE producer (``on_epic_completed``) lacked the probe
    while its target's claim lease lapsed under an hours-long task.

    Dedup-able means ALL of:
      • ``kind == KIND_WORKFLOW`` — only brain kickoffs. A human turn or a peer
        message is NEVER collapsed: repeating yourself is legitimate input.
      • the payload carries a non-empty ``boardTaskId`` — the epic identity the
        dedup is keyed on. A workflow row without one is not comparable.

    Scoped to ONE conversation on purpose: ``migrate_epic`` re-enqueues the same
    epic on a different (idle) conv, and that is a genuinely new row — a
    cross-conv dedup would silently strand a migrated epic.

    Fails OPEN: any lookup error returns None (→ insert). The accepted failure
    mode is an occasional duplicate row (visible, and the consume-time discard
    still stops the billed task); the unacceptable one is silently swallowing a
    turn that no other row will deliver.
    """
    if kind != KIND_WORKFLOW:
        return None
    board_task_id = (message_data or {}).get('boardTaskId')
    if not board_task_id:
        return None
    try:
        rows = db.execute(
            'SELECT id, payload FROM message_queue WHERE conv_id=? AND kind=?',
            (conv_id, kind)).fetchall()
        for r in rows:
            try:
                p = json.loads(r['payload']) if r['payload'] else {}
            except (TypeError, ValueError) as e:
                logger.debug('[Queue] dedup payload parse failed (skipping): %s', e)
                continue
            if p.get('boardTaskId') == board_task_id:
                return r['id']
        return None
    except Exception as e:
        logger.warning('[Queue] board-kickoff dedup probe failed conv=%s '
                       'epic=%s (enqueuing anyway): %s',
                       conv_id[:8] if conv_id else '?', board_task_id, e)
        return None


def enqueue_message(conv_id: str, message_data: dict, config: dict,
                    kind: str = KIND_REAL) -> dict:
    """Add a turn source to the server-side queue for a conversation.

    Args:
        conv_id: Conversation ID.
        message_data: Dict with keys: text, images, pdfTexts, replyQuotes,
                      convRefs, convRefTexts, originalContent, timestamp.
                      For an ``autopilot`` sentinel this is an empty/marker
                      dict (the row is never dispatched as a task).
        config: The chat config to use when dispatching this message
                (model, searchMode, tools, etc.).
        kind: Turn source — ``KIND_REAL`` (default), ``KIND_WORKFLOW`` or
              ``KIND_AUTOPILOT``.  Determines the priority bucket.

    Returns:
        Dict with queueId, position, kind. On a COLLAPSED brain kickoff (a row
        for the same ``(conv_id, boardTaskId)`` already queued) the EXISTING
        row's id is returned with ``deduped: True`` — never a fresh uuid that
        no row carries, so a caller storing it cannot hold a dangling id.
    """
    _maybe_ensure_table()

    db = get_thread_db(DOMAIN_CHAT)

    # ── Structural de-dup floor: one queued kickoff per (conv, epic) ──
    # See _existing_board_kickoff. Applies ONLY to brain kickoffs carrying a
    # boardTaskId; human and peer turns are never collapsed.
    existing = _existing_board_kickoff(db, conv_id, message_data, kind)
    if existing:
        row = db.execute(
            'SELECT position FROM message_queue WHERE id=?', (existing,)).fetchone()
        position = int(row['position']) if row and row['position'] is not None else 1
        logger.info('[Queue] collapsed duplicate %s kickoff for conv=%s epic=%s '
                    '→ reusing queued row %s (position=%d); a second row would '
                    'inflate the visible queue depth and, on drain, cost a '
                    'billed task re-doing claimed work',
                    kind, conv_id[:8], message_data.get('boardTaskId'),
                    existing[:8], position)
        return {'queueId': existing, 'position': position, 'kind': kind,
                'deduped': True}

    queue_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    timestamp = message_data.get('timestamp', now_ms)
    priority = _priority_for_kind(kind)

    # Get current queue depth for position
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    position = (row[0] if row else 0) + 1

    payload = json.dumps(message_data, ensure_ascii=False)
    config_json = json.dumps(config, ensure_ascii=False)

    db_execute_with_retry(db, '''
        INSERT INTO message_queue (id, conv_id, payload, config, position, kind, priority, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (queue_id, conv_id, payload, config_json, position, kind, priority, timestamp))

    logger.info('[Queue] Enqueued %s source %s for conv=%s position=%d priority=%d text=%d chars',
                kind, queue_id[:8], conv_id[:8], position, priority,
                len(message_data.get('text', '')))

    # Owner-ratified preemption (2026-07-25): a human's REAL message must not
    # wait out an in-flight autopilot VU call — the deferral used to fire
    # only AFTER run_virtual_user completed (production incident: two full
    # VU rounds, 94s + 74s, of dead time). Abort the VU sub-task NOW; it
    # unwinds at the next abort checkpoint (the SSE loop checks per-chunk)
    # and the completion hook dispatches THIS row seconds later, not minutes.
    # KIND_REAL only: a peer/workflow row keeps the cheap wait-for-completion
    # deferral — its latency is not user-visible, so killing a paid VU call
    # for it would be waste.
    if kind == KIND_REAL:
        try:
            _preempt_vu_subtask_for_real_message(conv_id)
        except Exception as e:
            logger.warning('[Queue] VU preempt on real enqueue failed conv=%s: %s',
                           conv_id[:8], e)

    return {'queueId': queue_id, 'position': position, 'kind': kind}


def _preempt_vu_subtask_for_real_message(conv_id: str) -> bool:
    """Abort the conv's live autopilot VU sub-task so a just-enqueued REAL
    message starts generating at the next abort checkpoint instead of
    waiting out the whole VU LLM call.

    Mirrors the parent→sub-task abort-mirror pattern in
    ``lib/tasks_pkg/autopilot.run_virtual_user``: the orchestrator polls
    ``task['aborted']`` per round and the SSE stream loop checks its
    abort_check PER CHUNK (lib/llm/stream.py:163-166), so the VU unwinds
    within seconds. ``run_virtual_user`` then routes the deferral
    (AUTOPILOT_VU_CANCEL + completion-hook dispatch of the queued row).

    Best-effort: any probe failure logs and returns False (the row is
    already enqueued — the post-call deferral still applies, so the
    worst case is the OLD wait-for-completion behaviour, never a loss).

    Returns True iff a VU sub-task was preempted.
    """
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            vus = [t for t in tasks.values()
                   if t.get('convId') == conv_id
                   and t.get('_vu_subtask')
                   and t.get('status') in ('pending', 'running')
                   and not t.get('aborted')]
        if not vus:
            return False
        from lib.log import audit_log
        for t in vus:
            t['aborted'] = True
            t['_abort_timestamp'] = time.time()
            t['_abort_reason'] = 'real_message_preempts_vu'
            audit_log('vu_preempted_by_real_message', conv_id=conv_id,
                      vu_task_id=t.get('id', ''))
            logger.info('[Queue] Real message preempts autopilot VU sub-task %s '
                        'for conv=%s — the queued turn starts at the next abort '
                        'checkpoint instead of after the full VU call',
                        t.get('id', '?')[:8], conv_id[:8])
        return True
    except Exception as e:
        logger.warning('[Queue] VU preempt probe failed conv=%s: %s', conv_id[:8], e)
        return False


def arm_autopilot_marker(conv_id: str, config: dict) -> dict:
    """Enqueue (or reaffirm) the persistent autopilot armed-marker sentinel.

    Idempotent: at most one ``autopilot`` row exists per conversation.  When
    already armed, returns the existing row's id without inserting a second.
    The sentinel carries the resolved send ``config`` so the autopilot hook
    and any follow-up reuse the same model / tools the user had selected.

    Returns ``{queueId, armed}`` — ``armed`` True iff a NEW sentinel was added
    (False when one already existed).
    """
    _maybe_ensure_table()
    existing = _get_autopilot_marker(conv_id)
    if existing:
        return {'queueId': existing['queueId'], 'armed': False}
    res = enqueue_message(conv_id, {'_autopilotMarker': True}, config,
                          kind=KIND_AUTOPILOT)
    return {'queueId': res['queueId'], 'armed': True}


def _get_autopilot_marker(conv_id: str) -> dict | None:
    """Return ``{queueId}`` for the conv's autopilot sentinel, or None."""
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT id FROM message_queue WHERE conv_id=? AND kind=? LIMIT 1',
        (conv_id, KIND_AUTOPILOT)
    ).fetchone()
    return {'queueId': row['id']} if row else None


def get_autopilot_marker_config(conv_id: str) -> dict | None:
    """Return the send config stored on the conv's autopilot sentinel, or None.

    The armed-marker row carries the resolved send ``config`` (model / tools /
    searchMode …) captured at arm time.  Startup autopilot-resume reads it so a
    crash-recovered run re-kicks with the SAME config the user had selected,
    not a bare default.  Best-effort — returns None on any failure.
    """
    if not conv_id:
        return None
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT config FROM message_queue WHERE conv_id=? AND kind=? LIMIT 1',
            (conv_id, KIND_AUTOPILOT)
        ).fetchone()
        if not row:
            return None
        return json.loads(row['config'] or '{}')
    except Exception as e:
        logger.debug('[Queue] get_autopilot_marker_config failed: %s', e)
        return None


def list_armed_autopilot_convs() -> list[str]:
    """Return every conv_id that currently carries an autopilot armed-marker.

    The DURABLE source of truth for "which conversations are armed" — a marker
    survives restart (it is a DB row), so this is what startup autopilot-resume
    scans to re-kick every armed run, NOT the set of conversations that merely
    had an in-flight task at crash time. That distinction matters: a conv armed
    from idle (marker present, no task ever spawned, or the reply already
    finished before the crash) has an armed marker but was never a "recovered"
    task — scanning markers catches it; scanning recovered tasks would strand
    it. Best-effort — returns [] on any failure.
    """
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id FROM message_queue WHERE kind=?',
            (KIND_AUTOPILOT,)
        ).fetchall()
        return [r['conv_id'] for r in rows if r['conv_id']]
    except Exception as e:
        logger.warning('[Queue] list_armed_autopilot_convs failed: %s', e)
        return []


def list_orphaned_dispatchable_convs() -> list[str]:
    """Return every conv_id carrying a DISPATCHABLE queue row (real / peer /
    workflow_step — i.e. everything except the autopilot sentinel).

    This is the durable source of truth for "which conversations have a queued
    turn that no running task will ever drain". A queued human ``real`` row is
    written by ``/api/chat/send`` when a task is already running, and is drained
    ONLY by the post-task-completion hook / human-send / brain idle-drain — none
    of which fire after a server restart, because the task that would have
    triggered the completion hook died with the process. So on boot these rows
    are ORPHANED: shown in the queue bar (a DB row survives), never dispatched,
    no transcript trace = total loss. Startup re-dispatch
    (:func:`redispatch_orphaned_queue_on_startup`) scans this list to drain
    them, mirroring the autopilot armed-marker resume.

    Best-effort — returns [] on any failure.
    """
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id FROM message_queue WHERE kind!=?',
            (KIND_AUTOPILOT,)
        ).fetchall()
        return [r['conv_id'] for r in rows if r['conv_id']]
    except Exception as e:
        logger.warning('[Queue] list_orphaned_dispatchable_convs failed: %s', e)
        return []


def reap_expired_queue_leases(force_reclaim: bool = False) -> list[str]:
    """Reclaim stranded dispatch leases and re-dispatch them (maintenance tick).

    The dispatch lease (pt_4ab943fa) makes a queued row survive its dispatch;
    this reaper is the backstop that turns that durability into an automatic
    retry. Per leased row:

      • LIVE TASK — the lease's task is running in the registry: renew an
        expiring lease and leave the row alone (never a double-dispatch).
      • LOST-FINALIZE — the lease's task is TERMINAL (registry, or the durable
        ``task_results`` floor after registry eviction): the spawn succeeded
        but the deferred delete was lost. Finish the delete here — the row
        must never come back as a duplicate turn.
      • DEAD LEASE — expired lease whose task is missing/not running (crash
        or exception mid-dispatch): release the lease so the row drains again.

    Then, for every conv left with a dispatchable row and NO live task in the
    registry (the same per-conv guard the startup orphan scan uses), dispatch
    ONE row — the completion hook drains the rest in priority order. This is
    the steady-state safety net that converts "silent message loss" into
    "automatic retry on the next tick".

    ``force_reclaim=True`` (startup only): treat EVERY lease as dead. Correct
    on a fresh boot in the single-process topology — the registry is empty,
    so every lease's owner process is definitionally gone.

    Returns the list of task_ids spawned from reclaimed rows.
    """
    spawned: list[str] = []
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        rows = db.execute(
            'SELECT id, conv_id, leased_until, lease_task_id FROM message_queue '
            'WHERE kind!=? AND leased_until IS NOT NULL',
            (KIND_AUTOPILOT,),
        ).fetchall()
    except Exception as e:
        logger.warning('[Queue] lease-reaper scan failed: %s', e)
        return spawned

    for row in rows:
        qid = row['id']
        conv_id = row['conv_id']
        lease_tid = (row['lease_task_id'] or '')
        expired = force_reclaim or (row['leased_until'] or 0) < now_ms

        if lease_tid and not force_reclaim:
            status = None
            try:
                from lib.tasks_pkg.manager import tasks, tasks_lock
                with tasks_lock:
                    _t = tasks.get(lease_tid)
                if _t is not None:
                    status = 'aborted' if _t.get('aborted') else _t.get('status')
            except Exception as e:
                logger.debug('[Queue] lease-reaper registry probe failed for %s: %s',
                             qid[:8], e)
            if status is None:
                # Registry miss — check the durable floor before calling the
                # lease dead: a task that finished and was later evicted from
                # the registry still proves the spawn happened, so finishing
                # the delete (not a re-dispatch) is the correct repair.
                try:
                    _tr = db.execute(
                        'SELECT status FROM task_results WHERE task_id=?',
                        (lease_tid,),
                    ).fetchone()
                    if _tr is not None:
                        status = _tr['status'] or 'done'
                except Exception as e:
                    logger.debug('[Queue] lease-reaper task_results probe failed: %s', e)
            if status == 'running':
                if expired:
                    try:
                        db_execute_with_retry(
                            db,
                            'UPDATE message_queue SET leased_until=? WHERE id=?',
                            (now_ms + _QUEUE_LEASE_MS, qid))
                    except Exception as e:
                        logger.warning('[Queue] lease renew failed for %s: %s', qid[:8], e)
                continue
            if status in ('done', 'error', 'aborted'):
                try:
                    _finalize_queue_dispatch(db, conv_id, qid)
                    logger.warning('[Queue] lease-reaper finished lost delete for %s '
                                   '(task %s terminal=%s)', qid[:8], lease_tid[:8], status)
                except Exception as e:
                    logger.warning('[Queue] lease-reaper finalize failed for %s: %s',
                                   qid[:8], e)
                continue

        if not expired:
            # Fresh lease with no task id yet — an in-flight dispatch owns it.
            continue
        try:
            db_execute_with_retry(
                db,
                "UPDATE message_queue SET leased_until=NULL, lease_task_id='' WHERE id=?",
                (qid,))
            logger.warning('[Queue] lease-reaper reclaimed dead lease %s conv=%s '
                           '(lease_task=%s)', qid[:8], conv_id[:8],
                           lease_tid[:8] or '—')
        except Exception as e:
            logger.warning('[Queue] lease-reaper release failed for %s: %s', qid[:8], e)

    # Stranded drain: one dispatch per conv that has a dispatchable row and no
    # live task. Bounded lock wait — a wedged in-flight dispatch must never
    # wedge the maintenance tick that drives this reaper. OLDEST-ENQUEUED
    # first, capped per tick — a mass-stranding event (restart) must not slam
    # the LLM rate limit with N simultaneous spawns.
    try:
        stranded = db.execute(
            'SELECT conv_id, MIN(created_at) AS oldest FROM message_queue '
            'WHERE kind!=? AND (leased_until IS NULL OR leased_until < ?) '
            'GROUP BY conv_id ORDER BY oldest ASC',
            (KIND_AUTOPILOT, now_ms),
        ).fetchall()
    except Exception as e:
        logger.warning('[Queue] lease-reaper stranded scan failed: %s', e)
        return spawned

    max_dispatch = _reaper_max_dispatch_per_tick()
    attempts = 0
    for srow in stranded:
        if attempts >= max_dispatch:
            logger.info('[Queue] lease-reaper: per-tick dispatch cap %d reached — '
                        'remaining %d stranded conv(s) defer to the next tick',
                        max_dispatch, len(stranded) - attempts)
            break
        conv_id = srow['conv_id']
        if not conv_id or _conv_has_live_task(conv_id):
            continue
        attempts += 1
        try:
            tid = dispatch_next_queued(conv_id, _wait=5)
        except Exception as e:
            logger.warning('[Queue] lease-reaper dispatch failed for conv=%s: %s',
                           conv_id[:8], e, exc_info=True)
            continue
        if tid:
            spawned.append(tid)
            try:
                from lib.log import audit_log
                audit_log('queue_lease_reclaim', conv_id=conv_id, task_id=tid)
            except Exception as e:
                logger.debug('[Queue] audit_log failed: %s', e)
            logger.info('[Queue] lease-reaper: conv=%s → task %s', conv_id[:8], tid[:8])

    if spawned:
        logger.info('[Queue] lease-reaper spawned %d task(s) from reclaimed rows',
                    len(spawned))
    return spawned


def redispatch_orphaned_queue_on_startup() -> list[str]:
    """Re-dispatch every queued turn stranded by a server restart.

    A message enqueued while a task was running lives ONLY in ``message_queue``
    (never in ``conversations.messages`` — deliberate, so it doesn't render
    mid-stream). The queue row is durable, but the ONLY things that drain it are
    the post-task-completion hook, a human send, and the Project-Brain idle
    drain — NONE of which fire on a fresh boot for a conversation with no live
    task. So without this scan, a restart leaves the message shown in the queue
    bar but never processed, with no trace in the transcript = total loss (the
    ``KIND_REAL`` analogue of the autopilot armed-marker gap that
    :func:`~lib.tasks_pkg.autopilot.resume_armed_autopilot_after_crash` closes).

    For each conversation with a dispatchable row, we dispatch ONE task via the
    SAME :func:`dispatch_next_queued` seam every other caller uses — which pops
    the highest-priority queued row, appends its user message to
    ``conversations.messages`` (giving it a durable transcript home at last) and
    spawns the task. We deliberately start only ONE task per conv (not the whole
    queue) — the normal post-task-completion hook drains the remaining rows in
    priority order, exactly as in steady-state operation where a conversation
    only ever has one task running at a time.

    Ordering / safety:
      • Runs at startup AFTER ``recover_stale_tasks_on_startup`` has marked all
        crashed tasks ``interrupted`` and cleared dead ``activeTaskId`` pointers,
        so no conversation has a live in-memory task — draining cannot
        double-dispatch. A defensive live-task guard is applied per conv anyway.
      • ``dispatch_next_queued`` takes the non-reentrant ``_dispatch_lock``
        itself, so we must NOT hold it here.
      • Best-effort per conv: one failure never aborts the batch.

    Returns the list of task_ids spawned (one per conv that had a queued turn).
    """
    spawned: list[str] = []
    # Crash-durable leases (pt_4ab943fa): on a fresh boot the registry is
    # empty, so EVERY surviving lease is a dead-process artifact — reclaim
    # them all up front (this also re-dispatches one row per affected conv).
    try:
        spawned.extend(reap_expired_queue_leases(force_reclaim=True))
    except Exception as e:
        logger.warning('[Queue] startup lease reclaim failed: %s', e, exc_info=True)
    try:
        convs = list_orphaned_dispatchable_convs()
    except Exception as e:
        logger.warning('[Queue] redispatch-on-startup: scan failed: %s', e)
        return spawned

    if not convs:
        logger.debug('[Queue] redispatch-on-startup: no orphaned queued turns')
        return spawned

    logger.info('[Queue] redispatch-on-startup: %d conv(s) have orphaned queued '
                'turn(s): %s', len(convs), [c[:8] for c in convs])

    # Same herd guard as the steady-state reaper: a mass-stranding restart
    # dispatches oldest-first, K per boot — the maintenance tick drains the
    # rest, so recovery is throttled instead of an LLM rate-limit storm.
    max_boot = _reaper_max_dispatch_per_tick()
    boot_attempts = len(spawned)  # the lease reclaim above already spent some
    for conv_id in convs:
        if boot_attempts >= max_boot:
            logger.info('[Queue] redispatch-on-startup: dispatch cap %d reached — '
                        'remaining %d conv(s) drain on the maintenance tick',
                        max_boot, len(convs) - boot_attempts)
            break
        if not conv_id:
            continue
        # Defensive: never drain a conv that already has a live task (a task
        # spawned earlier in the same boot — e.g. by the lease reclaim above —
        # or a racing send).
        if _conv_has_live_task(conv_id):
            logger.info('[Queue] redispatch-on-startup: conv=%s already has a '
                        'live task — leaving its queue for the completion hook',
                        conv_id[:8])
            continue

        # Dispatch ONE task for this conv; its completion hook drains the rest
        # of the queue (single-task-per-conv, as in steady state).
        boot_attempts += 1
        try:
            tid = dispatch_next_queued(conv_id)
        except Exception as e:
            logger.warning('[Queue] redispatch-on-startup: dispatch failed for '
                           'conv=%s: %s', conv_id[:8], e, exc_info=True)
            continue
        if tid:
            spawned.append(tid)
            from lib.log import audit_log
            audit_log('queue_redispatch_after_restart', conv_id=conv_id, task_id=tid)
            logger.info('[Queue] redispatch-on-startup: conv=%s → task %s',
                        conv_id[:8], tid[:8])

    if spawned:
        logger.info('[Queue] redispatch-on-startup: spawned %d task(s) from '
                    'orphaned queue rows', len(spawned))
    return spawned


def list_convs_with_pending_peer_msg() -> list[str]:
    """Return every conv_id that currently holds a pending ``KIND_PEER_MSG`` row.

    The durable source of truth for "which conversations have a peer message
    that no running task will ever drain". A peer message (``project_message`` /
    ``project_intervene``) is written as a ``KIND_PEER_MSG`` row and drained by
    ``dispatch_next_queued`` — which fires ONLY on task-completion, a human
    send, startup orphan-redispatch, or the brain KIND_WORKFLOW idle-drain. The
    workflow idle-drain (``project_dispatch._reconcile_stranded_kickoffs`` /
    ``_has_queued_kickoff``) filters STRICTLY on ``KIND_WORKFLOW``, so a peer
    row landing in an IDLE, non-board conversation is drained by nothing — it
    sits in the queue widget forever until a restart or a human types. This scan
    is what the steady-state peer idle-drain consumes to close that gap.

    Best-effort — returns [] on any failure.
    """
    try:
        _maybe_ensure_table()
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id FROM message_queue WHERE kind=?',
            (KIND_PEER_MSG,)
        ).fetchall()
        return [r['conv_id'] for r in rows if r['conv_id']]
    except Exception as e:
        logger.warning('[Queue] list_convs_with_pending_peer_msg failed: %s', e)
        return []


def drain_idle_peer_messages() -> list[str]:
    """Steady-state idle-drain for peer messages — the brain heartbeat's peer
    analogue of :func:`redispatch_orphaned_queue_on_startup`.

    THE Symptom-A root fix. A ``KIND_PEER_MSG`` row that lands in an IDLE
    conversation (no live task, and not the owner of a board epic the workflow
    idle-drain would reconcile) is drained by nothing in steady state — so an
    advisory peer note to an idle sibling is shown ONLY as a pending item in the
    queue widget and never rendered as a turn. This pass, run on the existing
    brain 30s heartbeat (NO new thread/global), drains ONE such row per idle
    conv via the SAME ``dispatch_next_queued`` seam every other caller uses —
    which appends the peer turn to ``conversations.messages`` (giving it the
    ``.peer-msg-banner`` fresh-turn rendering) and spawns a task to answer it.

    This is a deliberate, backend-owned dispatch of a DURABLE, RATE-CAPPED,
    explicitly-sent signal — NOT the frontend age-heuristic auto-fire
    anti-pattern (Case-E). The per-(sender,target) send-time rate cap bounds how
    many peer rows can ever exist; the busy-guard + one-drain-per-conv-per-tick
    bound the work this pass starts.

    Safety (mirrors ``redispatch_orphaned_queue_on_startup``):
      • Skip a conv that has a live non-aborted task — a live drain-eligible
        turn already receives the fast-path inbox twin (delivered at its next
        round boundary), and its completion hook drains the durable row anyway;
        force-draining would double-dispatch. A conv with a live endpoint/VU
        task is likewise "busy" so it keeps queue-lane (cycle-end) delivery.
      • Skip a conv whose row is absent (a concurrent drain already popped it).
      • ``dispatch_next_queued`` takes the non-reentrant ``_dispatch_lock``
        itself, so we must NOT hold it here.
      • Best-effort per conv: one failure never aborts the batch.

    Returns the list of task_ids spawned (one per idle conv drained).
    """
    spawned: list[str] = []
    try:
        convs = list_convs_with_pending_peer_msg()
    except Exception as e:
        logger.warning('[Queue] peer idle-drain: scan failed: %s', e)
        return spawned
    if not convs:
        return spawned

    for conv_id in convs:
        if not conv_id:
            continue
        # Busy guard: never force-drain a conv that has a FAST-PATH-ELIGIBLE
        # live task — its inbox twin / round-boundary drain (or the completion
        # hook) owns delivery there, so force-draining would double-dispatch.
        # The predicate MUST mirror project_peer._live_drain_eligible_task
        # (running + not aborted, matched on convId OR _peer_drain_key): a VU
        # sub-task runs with convId='' and carries the parent conv in
        # _peer_drain_key, so a bare convId==conv_id check would MISS it and let
        # idle-drain wrongly pre-empt the VU loop's in-turn delivery. A conv
        # whose only live task is NOT eligible (aborted / non-running) falls
        # through and IS drained here — the intended strand-closing behaviour.
        try:
            from lib.tasks_pkg.manager import tasks, tasks_lock
            with tasks_lock:
                _live = any(
                    (t.get('convId') == conv_id
                     or t.get('_peer_drain_key') == conv_id)
                    and t.get('status') == 'running'
                    and not t.get('aborted')
                    for t in tasks.values()
                )
            if _live:
                continue
        except Exception as e:
            logger.debug('[Queue] peer idle-drain live-task probe failed '
                         'conv=%s (skipping): %s', conv_id[:8], e)
            continue
        try:
            tid = dispatch_next_queued(conv_id)
        except Exception as e:
            logger.warning('[Queue] peer idle-drain: dispatch failed for '
                           'conv=%s: %s', conv_id[:8], e, exc_info=True)
            continue
        if tid:
            spawned.append(tid)
            from lib.log import audit_log
            audit_log('peer_message_idle_drain', conv_id=conv_id, task_id=tid)
            logger.info('[Queue] peer idle-drain: woke idle conv=%s → task %s '
                        '(pending peer message delivered as a fresh turn)',
                        conv_id[:8], tid[:8])
    if spawned:
        logger.info('[Queue] peer idle-drain: woke %d idle conv(s) holding a '
                    'pending peer message', len(spawned))
    return spawned


def has_autopilot_marker(conv_id: str) -> bool:
    """True iff a persistent autopilot armed-marker exists for the conv."""
    if not conv_id:
        return False
    try:
        _maybe_ensure_table()
        return _get_autopilot_marker(conv_id) is not None
    except Exception as e:
        logger.debug('[Queue] has_autopilot_marker probe failed: %s', e)
        return False


def clear_autopilot_marker(conv_id: str) -> bool:
    """Remove the conv's autopilot sentinel (disarm). True if one was removed."""
    if not conv_id:
        return False
    db = get_thread_db(DOMAIN_CHAT)
    marker = _get_autopilot_marker(conv_id)
    if not marker:
        return False
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?',
                          (marker['queueId'],))
    _renumber_positions(db, conv_id)
    logger.info('[Queue] Cleared autopilot marker for conv=%s', conv_id[:8])
    return True


def get_queue(conv_id: str) -> list[dict]:
    """Get all queued messages for a conversation, ordered by position.

    Returns:
        List of dicts with keys: queueId, position, text (preview),
        hasImages, hasPdfs, hasRefs, hasQuotes, timestamp.
    """
    _maybe_ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT id, payload, position, kind, priority, created_at FROM message_queue '
        'WHERE conv_id=? ORDER BY priority ASC, position ASC',
        (conv_id,)
    ).fetchall()

    result = []
    for row in rows:
        try:
            data = json.loads(row['payload'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Queue] Failed to parse payload for queue_id=%s: %s', row['id'][:8], e)
            data = {}

        # A peer/operator turn (KIND_PEER_MSG) stores the model-facing framed
        # `text` (embeds "[Peer message … (conv X)]" + the sender's short id).
        # Prefer the clean, unframed `_peerText` the sender carried so the queue
        # bar shows the ORIGINAL message, and surface the sender + operator flag
        # so the frontend can render "from «title»" instead of the raw id.
        is_peer = bool(data.get('_peerMessage'))
        preview = (data.get('_peerText') if is_peer else None) or data.get('text', '') or ''
        entry = {
            'queueId': row['id'],
            'position': row['position'],
            'kind': row['kind'] or KIND_REAL,
            'priority': row['priority'],
            # No mid-word truncation (the user flagged cut-off previews). Cap at
            # a generous 2000 chars purely so a pathological payload can't bloat
            # the poll response; the frontend already wraps + scrolls the text.
            'text': preview[:2000],
            'hasImages': bool(data.get('images')),
            'hasPdfs': bool(data.get('pdfTexts')),
            'hasRefs': bool(data.get('convRefs')),
            'hasQuotes': bool(data.get('replyQuotes')),
            'timestamp': row['created_at'],
        }
        if is_peer:
            entry['isPeerMessage'] = True
            entry['fromConv'] = data.get('_fromConv', '')
            entry['isPeerHuman'] = bool(data.get('_peerHuman'))
        result.append(entry)

    return result


def remove_from_queue(conv_id: str, queue_id: str) -> bool:
    """Remove a specific message from the queue.

    Returns:
        True if removed, False if not found.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT id FROM message_queue WHERE id=? AND conv_id=?',
        (queue_id, conv_id)
    ).fetchone()
    if not row:
        return False

    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)

    logger.info('[Queue] Removed message %s from conv=%s', queue_id[:8], conv_id[:8])
    return True


def dedup_peer_durable_rows(conv_id: str, queue_ids) -> int:
    """Delete peer-message durable rows by ``queueId`` (the FORWARD-race de-dup).

    The Pillar #6 peer-message FORWARD-race twin of
    :func:`lib.agent_inbox.consume_peer`. A live-target peer message is written
    to BOTH a durable ``message_queue`` row AND a fast-path agent_inbox item
    tagged with that row's ``queueId``. When the orchestrator's round-boundary
    drain hook injects the inbox item (delivery), it calls THIS to delete the
    matching durable row(s) so ``dispatch_next_queued`` can never later pop them
    as a redundant fresh turn = zero double-delivery. The REVERSE race (durable
    row dispatched first) is closed symmetrically by ``consume_peer``.

    Best-effort — a delete failure logs and is skipped (the reverse-race guard
    still protects against a double delivery). Returns the number removed.
    """
    ids = [q for q in (queue_ids or []) if q]
    if not conv_id or not ids:
        return 0
    removed = 0
    for qid in ids:
        try:
            if remove_from_queue(conv_id, qid):
                removed += 1
        except Exception as e:
            logger.warning('[Queue] peer durable-row de-dup failed conv=%s '
                           'queueId=%s: %s — the row may re-dispatch as a '
                           'duplicate', conv_id[:8], str(qid)[:8], e)
    if removed:
        logger.info('[Queue] forward de-dup removed %d peer durable row(s) for '
                    'conv=%s (delivered via the fast-path inbox)',
                    removed, conv_id[:8])
    return removed


def clear_queue(conv_id: str) -> int:
    """Clear all queued messages for a conversation.

    Returns:
        Number of messages removed.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    count = row[0] if row else 0

    if count > 0:
        db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
        logger.info('[Queue] Cleared %d messages from conv=%s', count, conv_id[:8])

    return count



def _renumber_positions(db, conv_id: str):
    """Re-number position column after a deletion to keep them contiguous."""
    rows = db.execute(
        'SELECT id FROM message_queue WHERE conv_id=? ORDER BY position ASC',
        (conv_id,)
    ).fetchall()
    for i, row in enumerate(rows, 1):
        db.execute(
            'UPDATE message_queue SET position=? WHERE id=?',
            (i, row['id'])
        )
    db.commit()


def dequeue_next(conv_id: str) -> dict | None:
    """Pop the next message from the queue (lowest position).

    Returns:
        Full message dict (payload + config) or None if queue is empty.
    """
    db = get_thread_db(DOMAIN_CHAT)

    # Only dispatchable sources (real / workflow_step) are popped as tasks.
    # The autopilot sentinel is consulted by the end-of-turn hook, never
    # dequeued here.
    # Lease-aware: a row carrying an UNEXPIRED lease belongs to an in-flight
    # dispatch — never hand it to a second drainer. An EXPIRED lease is a
    # crash/failure artifact and the row is fair game again (self-heal even
    # before the reaper runs).
    now_ms = int(time.time() * 1000)
    row = db.execute(
        'SELECT id, payload, config FROM message_queue '
        'WHERE conv_id=? AND kind!=? '
        'AND (leased_until IS NULL OR leased_until < ?) '
        'ORDER BY priority ASC, position ASC LIMIT 1',
        (conv_id, KIND_AUTOPILOT, now_ms)
    ).fetchone()

    if not row:
        return None

    queue_id = row['id']
    try:
        payload = json.loads(row['payload'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse payload for dequeue queue_id=%s: %s', queue_id[:8], e)
        payload = {}

    try:
        config = json.loads(row['config'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse config for dequeue queue_id=%s: %s', queue_id[:8], e)
        config = {}

    # LEASE, don't delete (pt_4ab943fa). The row stays durable until
    # spawn_task succeeds (the delete moved to _finalize_queue_dispatch), so
    # any failure/crash between here and the spawn leaves the message
    # reclaimable instead of silently lost. lease_task_id='' means "dispatch
    # in flight, task not yet created".
    db_execute_with_retry(
        db,
        "UPDATE message_queue SET leased_until=?, lease_task_id='' WHERE id=?",
        (now_ms + _QUEUE_LEASE_MS, queue_id),
    )

    logger.info('[Queue] Leased queued message %s for dispatch conv=%s, text=%d chars',
                queue_id[:8], conv_id[:8], len(payload.get('text', '')))

    return {
        'queueId': queue_id,
        'payload': payload,
        'config': config,
    }


def _release_queue_lease(db, queue_id: str) -> None:
    """Release a dispatch lease immediately (used by every failure path).

    Best-effort — a failure here only delays re-dispatch until lease expiry;
    it can never lose the row (the row is only deleted on spawn success).
    """
    try:
        db_execute_with_retry(
            db,
            "UPDATE message_queue SET leased_until=NULL, lease_task_id='' WHERE id=?",
            (queue_id,),
        )
    except Exception as e:
        logger.warning('[Queue] lease release failed for %s: %s', queue_id[:8], e)


def _finalize_queue_dispatch(db, conv_id: str, queue_id: str) -> None:
    """Delete a successfully-dispatched row + renumber (the deferred delete).

    This is the ONLY delete on the dispatch path now — it runs AFTER
    spawn_task succeeded, so the durable copy outlives every failure window.
    """
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)


def _conv_has_live_task(conv_id: str) -> bool:
    """True if the in-memory registry holds a running, non-aborted task for
    the conv. Shared by the startup orphan scan and the lease reaper (the
    per-conv guard that prevents double-dispatch). Best-effort False on error.
    """
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            return any(
                t.get('convId') == conv_id
                and t.get('status') == 'running'
                and not t.get('aborted')
                for t in tasks.values()
            )
    except Exception as e:
        logger.debug('[Queue] live-task probe failed for conv=%s: %s', conv_id[:8], e)
        return False


def _append_user_msg_with_cas(db, conv_id: str, user_msg: dict) -> bool:
    """Append ``user_msg`` to a conversation's messages under an optimistic
    lock, retrying on a CAS miss.

    The old code did a bare read-modify-write of the whole ``messages`` blob
    (``SELECT messages`` → append → unconditional ``UPDATE``). ``_dispatch_lock``
    serializes dispatches WITHIN one process, but it does NOT guard against a
    concurrent frontend / other-writer UPDATE landing between our SELECT and
    our UPDATE — a last-writer-wins clobber that silently drops the other
    write (e.g. a settings PATCH or a partial-stream checkpoint). We now re-read
    the row and CAS on ``updated_at`` (mirroring
    ``manager._sync_partial_to_conversation``), so a racing write forces a
    retry against the fresh tail rather than being overwritten.

    Returns True on success, False if the conversation row is missing.
    """
    from lib.chat import append_user_msg_idempotent
    from lib.database import json_dumps_pg

    _MAX_CAS = 4
    for attempt in range(_MAX_CAS):
        row = db.execute(
            'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[Queue] Conversation %s not found for dispatch', conv_id[:8])
            return False
        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Queue] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            messages = []
        row['updated_at']
        cur_rev = row['rev']  # Phase 4 W3: CAS on rev (loop re-reads each attempt)

        # Idempotent append (dedupes if a prior attempt already wrote it).
        append_user_msg_idempotent(messages, user_msg)

        now_ms = int(time.time() * 1000)
        cur = db.execute(
            'UPDATE conversations SET messages=?, updated_at=?, msg_count=? '
            'WHERE id=? AND user_id=1 AND rev=?',
            (json_dumps_pg(messages), now_ms, len(messages), conv_id, cur_rev)
        )
        db.commit()
        if getattr(cur, 'rowcount', None) != 0:
            # Phase 5 dual-write (flag-gated, inert when off): tail append.
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, messages, now_ms=now_ms)
            return True
        # CAS miss — a concurrent writer bumped updated_at. Re-read + retry.
        logger.debug('[Queue] append CAS miss conv=%s attempt %d/%d — re-reading',
                     conv_id[:8], attempt + 1, _MAX_CAS)
        time.sleep(0.02 * (attempt + 1))

    # Exhausted retries. The old code fell back to an UNCONDITIONAL write here,
    # justified as "correctness > the rare lost-concurrent-write". Measurement
    # disproved that trade (conv ms3sfyrmn31omb, 2026-07-28): what an
    # unconditional whole-blob write loses is not a rare metadata tweak, it is
    # whatever row another writer appended in the meantime — five completed
    # autopilot turns, 1665–3252 chars each, gone with no error and no red test.
    # Dropping a queued turn is visible and recoverable; erasing a committed
    # turn is neither. So we keep re-reading instead, with a wider budget.
    logger.warning('[Queue] append CAS contended for conv=%s — widening the '
                   'retry budget rather than overwriting a concurrent writer',
                   conv_id[:8])
    for attempt in range(_MAX_CAS, _MAX_CAS * 3):
        row = db.execute(
            'SELECT messages, rev FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return False
        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Queue] messages JSON parse failed, using fallback: %s', e)
            messages = []
        cur_rev = row['rev']
        append_user_msg_idempotent(messages, user_msg)
        now_ms = int(time.time() * 1000)
        cur = db.execute(
            'UPDATE conversations SET messages=?, updated_at=?, msg_count=? '
            'WHERE id=? AND user_id=1 AND rev=?',
            (json_dumps_pg(messages), now_ms, len(messages), conv_id, cur_rev)
        )
        db.commit()
        if getattr(cur, 'rowcount', None) != 0:
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, messages, now_ms=now_ms)
            return True
        time.sleep(0.05 * (attempt - _MAX_CAS + 1))
    logger.error('[Queue] append could not win the rev CAS for conv=%s after '
                 '%d attempts — the queued turn was NOT appended (it stays '
                 'queued for the next drain rather than clobbering the row)',
                 conv_id[:8], _MAX_CAS * 3)
    return False


def _brain_kickoff_still_wanted(project_path: str | None, board_task_id: str,
                                conv_id: str) -> bool:
    """True iff a brain kickoff for ``board_task_id`` is still worth spawning.

    Consume-time re-check for the produce/consume gap (pt_1613ab83b1934884).
    A kickoff is dropped when its epic is no longer waiting for work:

      • the epic row is GONE (deleted board entry), or
      • its effective status is ``done`` (finished while the kickoff queued —
        THE incident: done at 21:01:55, drained at 21:03:07), or
      • it is effectively ``claimed`` by a DIFFERENT conversation (a sibling
        legitimately took it over; spawning here would duplicate the work).

    Fails OPEN: any lookup error returns True, so an unrelated DB hiccup can
    never silently swallow a legitimate kickoff — the failure mode we accept is
    "a stale kickoff occasionally slips through" (recoverable, costs one task),
    never "brain dispatch stops working" (invisible, stalls the whole project).
    """
    if not project_path or not board_task_id:
        return True
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        epic = next((t for t in board.get('tasks', [])
                     if t.get('id') == board_task_id), None)
        if epic is None:
            logger.info('[Queue] discarding brain kickoff conv=%s epic=%s — '
                        'board row is gone', conv_id[:8], board_task_id)
            return False
        status = epic.get('status') or ''
        if status == 'done':
            logger.info('[Queue] discarding brain kickoff conv=%s epic=%s — '
                        'epic already DONE (finished while the kickoff sat in '
                        'the queue; spawning would re-verify finished work)',
                        conv_id[:8], board_task_id)
            return False
        owner = epic.get('owner_conv_id') or ''
        if status == 'claimed' and owner and owner != conv_id:
            logger.info('[Queue] discarding brain kickoff conv=%s epic=%s — '
                        'now live-claimed by conv=%s', conv_id[:8],
                        board_task_id, owner[:8])
            return False
        return True
    except Exception as e:
        logger.warning('[Queue] brain-kickoff board re-check failed conv=%s '
                       'epic=%s (dispatching anyway): %s',
                       conv_id[:8], board_task_id, e)
        return True


def dispatch_next_queued(conv_id: str, *, _wait: float | None = None) -> str | None:
    """Dispatch the next queued message for a conversation as a new task.

    Called after a task completes.  If there are queued messages, the first
    one is dequeued, its user message is appended to the conversation in the
    DB, and a new task is started.

    ``_wait`` bounds the dispatch-lock wait in seconds; None (default) waits
    forever — every steady-state caller. The lease reaper passes a small bound
    so a wedged in-flight dispatch can never wedge the maintenance tick.

    Returns:
        The new task_id if dispatched, None if queue was empty.
    """
    if _wait is None:
        _dispatch_lock.acquire()
    elif not _dispatch_lock.acquire(timeout=_wait):
        logger.info('[Queue] dispatch lock busy (>%ss) conv=%s — tick skips',
                    _wait, conv_id[:8])
        return None
    try:
        item = dequeue_next(conv_id)
        if not item:
            return None

        payload = item['payload']
        config = item['config']
        text = payload.get('text', '')

        # ── Stale brain-kickoff discard (pt_1613ab83b1934884) ──
        # A brain kickoff is PRODUCED when the board says an epic is dispatchable,
        # but it is CONSUMED here — possibly much later. In the 2026-07-27
        # incident the epic was marked done at 21:01:55 and this drain ran at
        # 21:03:07, spawning an Opus-5 task that re-verified finished work
        # (¥26, conv ms34yw0k74o2lq task 2ef5fcaa). Worse, that kickoff was
        # itself a re-dispatch of an epic whose 30-min claim lease had expired
        # under an 88-min task, so the board read it as open.
        #
        # The invariant that fixes ALL of those shapes at once: never trust the
        # produce-time decision — re-check at consume time. It holds regardless
        # of lease semantics, which is why lease renewal was ruled out as the
        # fix (it would only shrink the window, not close it).
        #
        # Only brain-dispatched rows are gated. A human turn has no boardTaskId
        # and must NEVER be discardable.
        #
        # ★ The filter itself lives in ``_row_is_dispatchable`` — the SINGLE
        #   consume-time predicate this function shares with the autopilot
        #   hook's yield gate. Do NOT re-inline a filter here: a filter that
        #   only one of the two readers applies is exactly what let a queued
        #   kickoff read as "a turn is waiting" to autopilot and as "discard
        #   me" to this dispatcher, destroying a finished VU turn and spawning
        #   nothing (conv ms3s8s0kjlvq18, 2026-07-28).
        if not _row_is_dispatchable(get_thread_db(DOMAIN_CHAT), conv_id,
                                    payload, config):
            _finalize_queue_dispatch(get_thread_db(DOMAIN_CHAT), conv_id,
                                     item['queueId'])
            return None

        # ── Pillar #6 REVERSE-race de-dup ──
        # A live-target peer message is written to BOTH this durable row AND a
        # fast-path agent_inbox twin tagged with this row's queueId. If the
        # target's live turn ended BEFORE its next round-boundary drain, we pop
        # the durable row HERE and dispatch it as a fresh turn — so the still-
        # pending inbox twin must be dropped, or it would be re-injected on that
        # fresh turn = double delivery. (The forward race — inbox drains first —
        # is closed symmetrically in the orchestrator drain hook, which deletes
        # this row by queueId.) The inbox is conv-keyed (swarm_key_for=convId).
        if payload.get('_peerMessage') and item.get('queueId'):
            try:
                from lib.agent_inbox import consume_peer
                consume_peer(conv_id, [item['queueId']])
            except Exception as e:
                logger.debug('[Queue] peer inbox-twin de-dup skipped conv=%s: %s',
                             conv_id[:8], e)
        # ★ _user_msg: pre-built (and already translated) user message dict
        #   from /api/chat/send.  If present, skip translation and use directly.
        pre_built_user_msg = payload.get('_user_msg')

        logger.info('[Queue] Dispatching queued message for conv=%s text=%d chars pre_built=%s',
                    conv_id[:8], len(text), bool(pre_built_user_msg))

        db = get_thread_db(DOMAIN_CHAT)

        if pre_built_user_msg:
            # ★ New path: /api/chat/send already built + translated the user message.
            #   Append it to the conversation DB under an optimistic lock so a
            #   concurrent writer can't clobber the append (see helper).
            if not _append_user_msg_with_cas(db, conv_id, pre_built_user_msg):
                _release_queue_lease(db, item['queueId'])
                return None
            remaining = _get_queue_depth(db, conv_id)
            logger.info('[Queue] Appended pre-built user msg to conv=%s (CAS)', conv_id[:8])

        else:
            # Legacy path: message was enqueued via /api/chat/queue (old API).
            # Need to translate and append to conversation ourselves.
            import re
            from lib.conv_config import resolve_auto_translate
            auto_translate = resolve_auto_translate(config)
            has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text)) if text else False
            translated_text = text
            _translate_model = None
            if auto_translate and has_chinese:
                try:
                    from lib.translate import _build_translate_prompt, _translate_freetext
                    system_prompt = _build_translate_prompt('English', 'Chinese')
                    result, _usage = _translate_freetext(
                        text, system_prompt, chunk_label=':queue',
                        source='Chinese', target='English',
                    )
                    if result and result.strip():
                        translated_text = result.strip()
                        if isinstance(_usage, dict):
                            _disp = _usage.get('_dispatch', {})
                            _translate_model = _disp.get('model', _usage.get('model'))
                        logger.info('[Queue] Auto-translated queued message for conv=%s: %d→%d chars model=%s',
                                    conv_id[:8], len(text), len(translated_text), _translate_model)
                    else:
                        translated_text = text
                except Exception as e:
                    logger.warning('[Queue] Auto-translate failed for conv=%s: %s', conv_id[:8], e)
                    translated_text = text

            # Build user message
            user_msg = {
                'role': 'user',
                'content': translated_text if auto_translate and has_chinese else text,
                'timestamp': payload.get('timestamp', int(time.time() * 1000)),
            }
            # ★ Carry the client-generated stable _msgId through verbatim
            #   (mirrors lib/chat/turn_builder.build_user_msg_from_payload).
            #   The QUEUED lane is the send-while-a-task-is-running path: the
            #   user sends on a slow network, it hangs, they send again → the
            #   turn is enqueued and later persisted HERE, not by the immediate
            #   /api/chat/send persist. Without preserving _msgId, a queued-then-
            #   rescued message duplicates exactly like the immediate path did
            #   before the fix — the client's rescue-PUT rebase (keyed on _msgId)
            #   wouldn't recognise this server copy. Preserve it so server and
            #   client agree on one turn identity.
            if payload.get('_msgId'):
                user_msg['_msgId'] = payload['_msgId']
            else:
                # Engine-built turns (brain kickoff / peer inject) arrive with
                # no client-minted id — stamp one here so the persisted turn
                # carries the SAME stable identity every reader keys on
                # (_msgId anchors the windowed-tail verify anchor and the
                # frontend's injected-turn dedup). Without it the turn was
                # invisible to the id-keyed reconciliation lanes (conv
                # msebjymx5b4a25, 2026-08-05).
                user_msg['_msgId'] = str(uuid.uuid4())
            if auto_translate and has_chinese and translated_text != text:
                user_msg['originalContent'] = text
                user_msg['_translateDone'] = True
                if _translate_model:
                    user_msg['_translateModel'] = _translate_model
            if payload.get('images'):
                user_msg['images'] = payload['images']
            if payload.get('pdfTexts'):
                user_msg['pdfTexts'] = payload['pdfTexts']
            if payload.get('replyQuotes'):
                user_msg['replyQuotes'] = payload['replyQuotes']
            if payload.get('convRefs'):
                user_msg['convRefs'] = payload['convRefs']
            # ── Peer-message attribution: a KIND_PEER_MSG turn is content-wise
            #    prefixed so the AGENT sees "[Peer message … (conv X)]", but the
            #    structured markers were being dropped here — the persisted turn
            #    then looked byte-identical to real user input, so the frontend
            #    could not visually distinguish/attribute it. Propagate the
            #    markers onto the message so the arrival is observable + the
            #    sender is attributable (renderer keys on `_peerMessage`). ──
            # ── Authoritative initiator stamp: a KIND_PEER_MSG / KIND_WORKFLOW
            #    turn is injected without a human typing, so the persisted turn
            #    must carry the ONE _initiator field every reader resolves
            #    through — not just the legacy per-path booleans (which we keep
            #    as read-aliases). Stamped here at the single dispatch seam. ──
            from lib.conversations.turn_initiation import (INITIATOR_BRAIN,
                                                           INITIATOR_OPERATOR,
                                                           INITIATOR_PEER,
                                                           stamp_initiator)
            if payload.get('_peerMessage'):
                user_msg['_peerMessage'] = True
                user_msg['_fromConv'] = payload.get('_fromConv', '')
            # Authoritative initiator stamp — kept as its OWN sibling guard (NOT
            # nested in the marker block above) so that block stays byte-identical
            # to the peer-marker negative-control anchor and can be neutered
            # independently without orphaning this call.
            if payload.get('_peerMessage'):
                stamp_initiator(
                    user_msg,
                    INITIATOR_OPERATOR if payload.get('_peerHuman') else INITIATOR_PEER)
            # A human operator nudge (sent from the Team panel) is stamped so the
            # receiving banner attributes it to the operator, not to an agent
            # peer. Distinct provenance, same KIND_PEER_MSG lane. Kept as its own
            # guard (not nested in the block above) so the peer-marker block can
            # be neutered independently by its own negative-control test.
            if payload.get('_peerMessage') and payload.get('_peerHuman'):
                user_msg['_peerHuman'] = True
            # ── Brain-dispatch attribution: a Project-Brain autonomous kickoff
            #    (KIND_WORKFLOW, marked _brainDispatch by dispatch_epic) must be
            #    distinguishable from human input downstream — the frontend
            #    shows it started autonomously, and the task itself should not
            #    be mistaken for a user turn. Propagate the markers onto the
            #    persisted user turn (mirrors the _peerMessage block). ──
            if payload.get('_brainDispatch'):
                user_msg['_brainDispatch'] = True
                stamp_initiator(user_msg, INITIATOR_BRAIN)
                if payload.get('boardTaskId'):
                    user_msg['_boardTaskId'] = payload.get('boardTaskId')
                # Provenance card data (creator conv/title, dispatch seam,
                # routing reason) — display-only, stamped by dispatch_epic.
                if payload.get('_brainEpic'):
                    user_msg['_brainEpic'] = payload['_brainEpic']
            conv_ref_texts = payload.get('convRefTexts')
            if not conv_ref_texts and payload.get('convRefs'):
                try:
                    from lib.chat import resolve_conv_refs
                    conv_ref_texts = resolve_conv_refs(payload['convRefs'])
                except Exception as e:
                    logger.warning('[Queue] Failed to resolve conv refs for conv=%s: %s',
                                   conv_id[:8], e)
            if conv_ref_texts:
                user_msg['convRefTexts'] = conv_ref_texts

            # Append user message to the conversation under an optimistic lock
            # (see _append_user_msg_with_cas — re-reads + CAS internally).
            if not _append_user_msg_with_cas(db, conv_id, user_msg):
                _release_queue_lease(db, item['queueId'])
                return None
            remaining = _get_queue_depth(db, conv_id)
        # (Legacy _msg_persisted path removed — no longer used)

        # 3. Build API messages and create task
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        api_messages = build_api_messages_from_db(conv_id, config)
        if not api_messages:
            logger.warning('[Queue] No API messages after building for conv=%s', conv_id[:8])
            _release_queue_lease(db, item['queueId'])
            return None

        from lib.tasks_pkg import create_task

        task = create_task(conv_id, api_messages, config)
        task_id = task['id']

        # Stamp the lease with the real task id (and renew it) so the reaper's
        # registry-liveness check can tell "spawned, delete pending" from
        # "died before create_task". Renewing also covers the reaper having
        # cleared this row's lease a moment ago during a slow dispatch.
        try:
            db_execute_with_retry(
                db,
                'UPDATE message_queue SET lease_task_id=?, leased_until=? WHERE id=?',
                (task_id, int(time.time() * 1000) + _QUEUE_LEASE_MS, item['queueId']),
            )
        except Exception as e:
            logger.debug('[Queue] lease task-stamp failed for %s: %s',
                         item['queueId'][:8], e)

        # Update conversation settings with the new activeTaskId. Serialized
        # read-merge-write (settings_store) so it doesn't clobber a concurrent
        # tool-state / autopilot settings write on the same row (reuses `db`).
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: this path emits its own notify_conv_changed after
            # spawn (no double push); the gate still invalidates the cache.
            set_conversation_settings(conv_id, {'activeTaskId': task_id}, db=db,
                                      notify=False)
        except Exception as e:
            logger.warning('[Queue] Failed to update activeTaskId for conv=%s: %s',
                           conv_id[:8], e, exc_info=True)

        # 4. Start the task in a background thread
        _cfg_model = config.get('model', '?')
        logger.info('[Queue] Starting dispatched task %s for conv=%s model=%s remaining=%d',
                    task_id[:8], conv_id[:8], _cfg_model, remaining)

        try:
            from lib.tasks_pkg import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Queue] Failed to start thread for dispatched task %s', task_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start queued task thread.',
                model=config.get('model', ''),
                context='queue-dispatch',
                source='message-queue',
                raw=str(_spawn_err),
            )
            _release_queue_lease(db, item['queueId'])
            return None

        # Spawn succeeded — NOW the durable row goes away (the deferred delete,
        # moved here from dequeue time for crash durability, pt_4ab943fa).
        try:
            _finalize_queue_dispatch(db, conv_id, item['queueId'])
        except Exception as e:
            # Non-fatal: the reaper finishes the delete once the task goes
            # terminal (and extends the lease while it runs) — the message can
            # never be re-dispatched as a duplicate by this path.
            logger.warning('[Queue] deferred delete failed for %s: %s',
                           item['queueId'][:8], e)

        # Notify clients so open tabs reflect the newly-dispatched turn
        # without a manual refresh. The append above changed the messages
        # column, so the DB trigger already bumped the content rev — carry it.
        # rev=None (metadata-only) was the old choice, and it is why an
        # engine-injected turn stayed INVISIBLE on an open tab for the whole
        # task: the rev-gate never saw a content change, so no body verify
        # ever ran (the push frame has no replay) — only the busy-signal
        # attach fired, painting "an Agent generating out of nowhere" (conv
        # msebjymx5b4a25, 2026-08-05). The stream-active adoption lane on the
        # client makes carrying the real rev safe during a live stream.
        try:
            from lib.conversations import notify_conv_changed
            from lib.tasks_pkg.manager._registry import task_user_id
            _rev_row = db.execute(
                'SELECT rev FROM conversations WHERE id=?', (conv_id,)).fetchone()
            _new_rev = _rev_row['rev'] if _rev_row else None
            notify_conv_changed(conv_id, rev=_new_rev, user_id=task_user_id(task))
        except Exception as e:
            logger.debug('[Queue] conv-changed notify failed: %s', e)

        return task_id
    finally:
        _dispatch_lock.release()


def _get_queue_depth(db, conv_id: str) -> int:
    """Number of DISPATCHABLE rows in queue (real / workflow_step only).

    Excludes the autopilot sentinel — it is never dispatched as a task, so
    callers gating "is there pending work to start" must not see it.  This is
    the lynchpin of human-over-autopilot priority: the autopilot hook calls
    this (via ``get_queue_depth``) and defers whenever it is > 0.
    """
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=? AND kind!=?',
        (conv_id, KIND_AUTOPILOT)
    ).fetchone()
    return row[0] if row else 0


def get_queue_depth(conv_id: str) -> int:
    """Public version: dispatchable queue depth with its own DB connection.

    ⚠️ This is a COUNT with a WEAK filter (kind only) — it deliberately does
    NOT apply the consume-time filters that decide whether a row will really
    become a turn. Use it for badges/telemetry, NEVER to answer "is a turn
    about to take over?" — for that ask :func:`has_pending_human_turn` /
    :func:`next_dispatchable_turn`, which route through the single
    ``_row_is_dispatchable`` predicate.
    """
    db = get_thread_db(DOMAIN_CHAT)
    return _get_queue_depth(db, conv_id)


# ── THE single consume-time dispatchability predicate ────────────────
#
# Both readers of this queue MUST route through here:
#   • ``dispatch_next_queued`` — decides whether a leased row becomes a task;
#   • ``next_dispatchable_turn`` / ``has_pending_human_turn`` — let the
#     autopilot hook ask "will a turn really take over from me?".
#
# WHY THE SEAM EXISTS (conv ms3s8s0kjlvq18, 2026-07-28): the dispatch side
# applied the board re-check while the autopilot side counted rows with a
# WEAKER filter (kind only). A brain kickoff whose epic had finished while it
# sat queued therefore read as "a human is waiting" to autopilot (which threw
# away a completed 24-round VU turn) and as "discard me" to the dispatcher
# (which spawned nothing). Two correct-looking gates, opposite verdicts on the
# SAME row, and the conversation died with no signal.
#
# Narrowing the kind check alone would have fixed that ONE instance and left
# the cause: every future filter would again land on only one side. Adding a
# filter HERE moves both readers at once — that is the entire point.

def _row_is_dispatchable(db, conv_id: str, payload: dict,
                         config: dict) -> bool:
    """True iff this queued row would really be dispatched as a turn.

    Args:
        db: Open chat-domain DB handle (filters may need to read state).
        conv_id: Owning conversation id.
        payload: The row's decoded payload dict.
        config: The row's decoded config dict.

    Returns:
        ``False`` only when a consume-time filter rejects the row. Fails OPEN
        (see ``_brain_kickoff_still_wanted``): an unrelated lookup error must
        never silently swallow a legitimate turn.
    """
    board_task_id = (payload or {}).get('boardTaskId')
    if board_task_id and not _brain_kickoff_still_wanted(
            (config or {}).get('projectPath'), board_task_id, conv_id):
        return False
    return True


def _dispatchable_rows(db, conv_id: str) -> list[dict]:
    """Queued rows that would REALLY be dispatched, in dispatch order.

    Mirrors ``dequeue_next``'s row selection (non-autopilot kinds, lease-aware,
    ``priority ASC, position ASC``) and then applies ``_row_is_dispatchable``
    to each — but takes NO lease and mutates nothing, so it is safe to ask
    from a decision gate.

    Returns a list of ``{'queueId', 'kind', 'isHuman'}``.
    """
    now_ms = int(time.time() * 1000)
    rows = db.execute(
        'SELECT id, kind, payload, config FROM message_queue '
        'WHERE conv_id=? AND kind!=? '
        'AND (leased_until IS NULL OR leased_until < ?) '
        'ORDER BY priority ASC, position ASC',
        (conv_id, KIND_AUTOPILOT, now_ms)
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row['payload'] or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Queue] peek payload parse failed id=%s: %s',
                         str(row['id'])[:8], e)
            payload = {}
        try:
            config = json.loads(row['config'] or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Queue] peek config parse failed id=%s: %s',
                         str(row['id'])[:8], e)
            config = {}
        if not _row_is_dispatchable(db, conv_id, payload, config):
            continue
        kind = row['kind'] or KIND_REAL
        out.append({'queueId': row['id'], 'kind': kind,
                    'isHuman': kind == KIND_REAL})
    return out


def next_dispatchable_turn(conv_id: str) -> dict | None:
    """The next queued turn that would REALLY be dispatched, or ``None``.

    Returns ``{'queueId', 'kind', 'isHuman'}`` for the head of the dispatchable
    queue. ``None`` means nothing here will become a turn — so a caller that
    stands down for it would be standing down for nobody.
    """
    if not conv_id:
        return None
    rows = _dispatchable_rows(get_thread_db(DOMAIN_CHAT), conv_id)
    return rows[0] if rows else None


def has_pending_human_turn(conv_id: str) -> bool:
    """True iff a real HUMAN turn is queued and would really be dispatched.

    The autopilot yield gate. The judgement is "is there a person waiting on
    this conversation" — NOT "is there a non-autopilot row". Machine work items
    (``KIND_WORKFLOW`` brain kickoffs, ``KIND_PEER_MSG`` sibling messages) do
    NOT preempt a run that is actively working: they are picked up by the
    existing idle drain once the run ends. Only a human outranks the loop.

    Scans ALL dispatchable rows rather than just the head, so the answer cannot
    depend on ``KIND_REAL``'s priority happening to sort first.

    Fails OPEN (``False``) on a probe error, matching the prior posture: a DB
    hiccup must not wedge a healthy loop, and the follow-up spawn is still
    guarded by the final supersede recheck.
    """
    if not conv_id:
        return False
    try:
        rows = _dispatchable_rows(get_thread_db(DOMAIN_CHAT), conv_id)
        return any(r['isHuman'] for r in rows)
    except Exception as e:
        logger.debug('[Queue] human-turn probe failed conv=%s (non-fatal): %s',
                     conv_id[:8], e)
        return False

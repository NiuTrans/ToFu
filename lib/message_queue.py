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

Dispatch durability: dequeue LEASES a row (``leased_until``) instead of
deleting it. The delete lands only after the authoritative turn pair and
attempt exist; every retryable failure releases the lease; a
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

from lib.log import get_logger

logger = get_logger(__name__)

# Lock for dispatch coordination (prevent double-dispatch races)
_dispatch_lock = threading.Lock()

# ── Turn-source kinds + their default priorities (lower = higher) ──
KIND_REAL = "real"
KIND_PEER_MSG = "peer_msg"
KIND_WORKFLOW = "workflow_step"
KIND_AUTOPILOT = "autopilot"

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


# ── Dispatch lease () ──
# How long a dequeued-but-not-yet-spawned row stays invisible to other drains.
# Must comfortably exceed the slowest in-dispatch step (auto-translate of a
# long queued message is an LLM call). Only a true process crash mid-dispatch
# ever waits out the full TTL — every failure path releases the lease
# immediately, and the success path deletes the row outright.
_QUEUE_LEASE_MS = 120 * 1000


def _queue_client(*, write: bool = False):
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _reaper_max_dispatch_per_tick() -> int:
    """Max stranded-drain dispatches per reaper tick (default 4).

    A crash/restart can strand MANY conversations at once (each holding a
    queued human message). Draining them all in a single tick would spawn N
    tasks simultaneously and slam the LLM rate limit — the steady-state tick
    drains oldest-first, K per tick; the rest retry on the next tick.
    """
    try:
        return max(
            1, int(os.environ.get("TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK", "") or "4")
        )
    except (ValueError, TypeError) as e:
        logger.debug(
            "[Queue] TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK parse failed: %s", e
        )
        return 4


def enqueue_message(
    conv_id: str,
    message_data: dict,
    config: dict,
    kind: str = KIND_REAL,
    *,
    user_id: int,
) -> dict:
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
    queue_id = str(uuid.uuid4())
    result = _queue_client(write=True).command(
        "queue.enqueue",
        {
            "user_id": int(user_id),
            "conv_id": conv_id,
            "queue_id": queue_id,
            "message": message_data,
            "config": config,
            "kind": kind,
            "priority": _priority_for_kind(kind),
            "created_at_ms": int(time.time() * 1000),
        },
        command_id=queue_id,
    )
    if kind == KIND_REAL:
        try:
            _preempt_vu_subtask_for_real_message(conv_id, user_id=int(user_id))
        except Exception as e:
            logger.warning(
                "[Queue] VU preempt on enqueue failed conv=%s: %s", conv_id[:8], e
            )
    return result


def _preempt_vu_subtask_for_real_message(
    conv_id: str,
    *,
    user_id: int,
) -> bool:
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
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        vus = [
            task
            for task in chat_task_runtime.snapshot_owned(user_id=int(user_id))
            if task.get("convId") == conv_id
            and task.get("_vu_subtask")
            and task.get("status") in ("pending", "running")
            and not task.get("aborted")
        ]
        if not vus:
            return False
        from lib.log import audit_log

        for t in vus:
            task_id = str(t.get("id") or "")
            if not chat_task_runtime.abort_owned(task_id, user_id=int(user_id)):
                continue
            chat_task_runtime.update_fields(
                task_id,
                fields={
                    "aborted": True,
                    "_abort_timestamp": time.time(),
                    "_abort_reason": "real_message_preempts_vu",
                },
                only_if_status=("pending", "running"),
            )
            audit_log(
                "vu_preempted_by_real_message",
                conv_id=conv_id,
                vu_task_id=t.get("id", ""),
            )
            logger.info(
                "[Queue] Real message preempts autopilot VU sub-task %s "
                "for conv=%s — the queued turn starts at the next abort "
                "checkpoint instead of after the full VU call",
                t.get("id", "?")[:8],
                conv_id[:8],
            )
        return True
    except Exception as e:
        logger.warning("[Queue] VU preempt probe failed conv=%s: %s", conv_id[:8], e)
        return False


def arm_autopilot_marker(
    conv_id: str,
    config: dict,
    *,
    user_id: int,
) -> dict:
    """Enqueue (or reaffirm) the persistent autopilot armed-marker sentinel.

    Idempotent: at most one ``autopilot`` row exists per conversation.  When
    already armed, returns the existing row's id without inserting a second.
    The sentinel carries the resolved send ``config`` so the autopilot hook
    and any follow-up reuse the same model / tools the user had selected.

    Returns ``{queueId, armed}`` — ``armed`` True iff a NEW sentinel was added
    (False when one already existed).
    """
    existing = _queue_client().query(
        "queue.autopilot.get", {"conv_id": conv_id, "user_id": int(user_id)}
    )
    if existing:
        return {"queueId": existing["queueId"], "armed": False}
    result = _queue_client(write=True).command(
        "queue.autopilot.arm",
        {
            "conv_id": conv_id,
            "user_id": int(user_id),
            "queue_id": str(uuid.uuid4()),
            "config": config,
        },
        command_id=None,
    )
    return {
        "queueId": result["queueId"],
        "armed": bool(result.get("armed")),
    }


def _get_autopilot_marker(
    conv_id: str,
    *,
    user_id: int,
) -> dict | None:
    """Return ``{queueId}`` for the conv's autopilot sentinel, or None."""
    row = _queue_client().query(
        "queue.autopilot.get", {"conv_id": conv_id, "user_id": int(user_id)}
    )
    return {"queueId": row["queueId"]} if row else None


def list_orphaned_dispatchable_conversations() -> list[dict]:
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
        return _queue_client().query("queue.conversations.list_all", {})
    except Exception as e:
        logger.warning("[Queue] list_orphaned_dispatchable_conversations failed: %s", e)
        return []


def reap_expired_queue_leases(force_reclaim: bool = False) -> list[str]:
    """Repair leases, then drain every currently orphaned queue conversation.

    ``queue.reap`` atomically releases expired leases (or every lease during
    startup recovery).  The following read deliberately scans *all* durable
    dispatchable rows, not only rows whose lease was just repaired.  That
    distinction closes the submit-failure window: a failed submit releases
    its lease immediately, so it would never appear in an expired-lease-only
    result even though no completion hook exists to retry it.

    ``dispatch_next_queued`` remains the single consumer.  Its live-task guard
    and lease-taking operation prevent double dispatch; this maintenance pass
    merely discovers conversations that still need a consumer.  Work is
    bounded and ordered by the oldest queued source so a large recovery does
    not create an LLM request herd.

    ``force_reclaim=True`` is startup-only.  On a fresh process every retained
    lease belongs to a dead predecessor and is safe to release.

    Returns the public attempt ids accepted during this tick.
    """
    spawned: list[str] = []
    now_ms = int(time.time() * 1000)
    reclaim_mode = "force" if force_reclaim else "normal"
    _queue_client(write=True).command(
        "queue.reap",
        {
            "now_ms": now_ms,
            "force_reclaim": bool(force_reclaim),
        },
        command_id=f"queue-reap:{now_ms}:{reclaim_mode}",
    ) or {}
    conversations = list_orphaned_dispatchable_conversations()
    limit = _reaper_max_dispatch_per_tick()
    attempts = 0
    for conversation in conversations:
        if attempts >= limit:
            break
        conv_id = str(conversation["convId"])
        user_id = int(conversation["userId"])
        if _conv_has_live_task(conv_id, user_id=user_id):
            continue
        attempts += 1
        try:
            task_id = dispatch_next_queued(conv_id, user_id=user_id, _wait=5)
        except Exception as e:
            logger.warning("[Queue] stranded drain failed conv=%s: %s", conv_id[:8], e)
            continue
        if task_id:
            spawned.append(task_id)
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
    The authoritative queue row survives restart, so it can be resumed without
    inferring work from conversation messages.

    For each conversation with a dispatchable row, we dispatch ONE task via the
    SAME :func:`dispatch_next_queued` seam every other caller uses — which pops
    the highest-priority queued row, appends its user message to
    ``conversations.messages`` (giving it a durable transcript home at last) and
    spawns the task. We deliberately start only ONE task per conv (not the whole
    queue) — the normal post-task-completion hook drains the remaining rows in
    priority order, exactly as in steady-state operation where a conversation
    only ever has one task running at a time.

    Ordering / safety:
      • Runs after turn/task recovery has settled prior-process work. A
        defensive live-task guard is still applied per conversation.
      • ``dispatch_next_queued`` takes the non-reentrant ``_dispatch_lock``
        itself, so we must NOT hold it here.
      • Best-effort per conv: one failure never aborts the batch.

    Returns the list of task_ids spawned (one per conv that had a queued turn).
    """
    spawned: list[str] = []
    # Crash-durable leases (): on a fresh boot the registry is
    # empty, so EVERY surviving lease is a dead-process artifact — reclaim
    # them all up front (this also re-dispatches one row per affected conv).
    try:
        spawned.extend(reap_expired_queue_leases(force_reclaim=True))
    except Exception as e:
        logger.warning("[Queue] startup lease reclaim failed: %s", e, exc_info=True)
    try:
        conversations = list_orphaned_dispatchable_conversations()
    except Exception as e:
        logger.warning("[Queue] redispatch-on-startup: scan failed: %s", e)
        return spawned

    if not conversations:
        logger.debug("[Queue] redispatch-on-startup: no orphaned queued turns")
        return spawned

    logger.info(
        "[Queue] redispatch-on-startup: %d conv(s) have orphaned queued turn(s): %s",
        len(conversations),
        [str(c["convId"])[:8] for c in conversations],
    )

    # Same herd guard as the steady-state reaper: a mass-stranding restart
    # dispatches oldest-first, K per boot — the maintenance tick drains the
    # rest, so recovery is throttled instead of an LLM rate-limit storm.
    max_boot = _reaper_max_dispatch_per_tick()
    boot_attempts = len(spawned)  # the lease reclaim above already spent some
    for conversation in conversations:
        conv_id = str(conversation["convId"])
        user_id = int(conversation["userId"])
        if boot_attempts >= max_boot:
            logger.info(
                "[Queue] redispatch-on-startup: dispatch cap %d reached — "
                "remaining %d conv(s) drain on the maintenance tick",
                max_boot,
                len(conversations) - boot_attempts,
            )
            break
        if not conv_id:
            continue
        # Defensive: never drain a conv that already has a live task (a task
        # spawned earlier in the same boot — e.g. by the lease reclaim above —
        # or a racing send).
        if _conv_has_live_task(conv_id, user_id=user_id):
            logger.info(
                "[Queue] redispatch-on-startup: conv=%s already has a "
                "live task — leaving its queue for the completion hook",
                conv_id[:8],
            )
            continue

        # Dispatch ONE task for this conv; its completion hook drains the rest
        # of the queue (single-task-per-conv, as in steady state).
        boot_attempts += 1
        try:
            tid = dispatch_next_queued(conv_id, user_id=user_id)
        except Exception as e:
            logger.warning(
                "[Queue] redispatch-on-startup: dispatch failed for conv=%s: %s",
                conv_id[:8],
                e,
                exc_info=True,
            )
            continue
        if tid:
            spawned.append(tid)
            from lib.log import audit_log

            audit_log("queue_redispatch_after_restart", conv_id=conv_id, task_id=tid)
            logger.info(
                "[Queue] redispatch-on-startup: conv=%s → task %s", conv_id[:8], tid[:8]
            )

    if spawned:
        logger.info(
            "[Queue] redispatch-on-startup: spawned %d task(s) from "
            "orphaned queue rows",
            len(spawned),
        )
    return spawned


def list_conversations_with_pending_peer_messages() -> list[dict]:
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
        return _queue_client().query(
            "queue.conversations.list_all", {"kind": KIND_PEER_MSG}
        )
    except Exception as e:
        logger.warning(
            "[Queue] list_conversations_with_pending_peer_messages failed: %s",
            e,
        )
        return []


def _conv_has_wake_peer_row(conv_id: str, *, user_id: int) -> bool:
    """True iff the conv holds a pending ``KIND_PEER_MSG`` row allowed to WAKE
    an idle conversation.

    Codex ``trigger_turn`` port: a peer note sent with ``wake=False`` carries
    ``_peerNoWake`` in its payload — mailbox-only delivery, it must NEVER spin
    up a fresh turn on an idle target (it rides the target's next NATURAL
    turn instead). A conv whose pending peer rows are ALL no-wake is skipped
    by the heartbeat idle-drain; one wake-capable row is enough to justify
    the turn (the no-wake rows then ride along in queue order — free mailbox
    batching).

    Fails OPEN (True) on a probe error: a storage hiccup must not strand a
    wake-intended message forever — the pre-flag behaviour (always drain) is
    the safe fallback.
    """
    try:
        rows = (
            _queue_client().query(
                "queue.list", {"conv_id": conv_id, "user_id": int(user_id)}
            )
            or []
        )
        payloads = [r.get("payload") for r in rows if r.get("kind") == KIND_PEER_MSG]
    except Exception as e:
        logger.warning(
            "[Queue] wake-peer-row probe failed conv=%s (fail-open: "
            "treating as wake): %s",
            conv_id[:8],
            e,
        )
        return True
    for raw in payloads:
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(
                "[Queue] wake probe payload parse failed conv=%s: %s", conv_id[:8], e
            )
            data = {}
        if not data.get("_peerNoWake"):
            return True
    return False


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
        conversations = list_conversations_with_pending_peer_messages()
    except Exception as e:
        logger.warning("[Queue] peer idle-drain: scan failed: %s", e)
        return spawned
    if not conversations:
        return spawned

    for conversation in conversations:
        conv_id = str(conversation["convId"])
        user_id = int(conversation["userId"])
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
            from lib.tasks_pkg.manager.runtime import chat_task_runtime

            _live = any(
                    (t.get("convId") == conv_id or t.get("_peer_drain_key") == conv_id)
                    and t.get("status") == "running"
                    and not t.get("aborted")
                    for t in chat_task_runtime.snapshot_owned(user_id=user_id)
                )
            if _live:
                continue
        except Exception as e:
            logger.debug(
                "[Queue] peer idle-drain live-task probe failed conv=%s (skipping): %s",
                conv_id[:8],
                e,
            )
            continue
        # wake=False (mailbox-only) rows never justify waking an idle conv —
        # skip when EVERY pending peer row is no-wake (Codex trigger_turn
        # semantics: send_message delivers mail, it does not start a turn).
        if not _conv_has_wake_peer_row(conv_id, user_id=user_id):
            logger.debug(
                "[Queue] peer idle-drain: conv=%s holds only no-wake "
                "peer row(s) — leaving for its next natural turn",
                conv_id[:8],
            )
            continue
        try:
            tid = dispatch_next_queued(conv_id, user_id=user_id)
        except Exception as e:
            logger.warning(
                "[Queue] peer idle-drain: dispatch failed for conv=%s: %s",
                conv_id[:8],
                e,
                exc_info=True,
            )
            continue
        if tid:
            spawned.append(tid)
            from lib.log import audit_log

            audit_log("peer_message_idle_drain", conv_id=conv_id, task_id=tid)
            logger.info(
                "[Queue] peer idle-drain: woke idle conv=%s → task %s "
                "(pending peer message delivered as a fresh turn)",
                conv_id[:8],
                tid[:8],
            )
    if spawned:
        logger.info(
            "[Queue] peer idle-drain: woke %d idle conv(s) holding a "
            "pending peer message",
            len(spawned),
        )
    return spawned


def has_autopilot_marker(conv_id: str, *, user_id: int) -> bool:
    """True iff a persistent autopilot armed-marker exists for the conv."""
    if not conv_id:
        return False
    try:
        return _get_autopilot_marker(conv_id, user_id=user_id) is not None
    except Exception as e:
        logger.debug("[Queue] has_autopilot_marker probe failed: %s", e)
        return False


def clear_autopilot_marker(conv_id: str, *, user_id: int) -> bool:
    """Remove the conv's autopilot sentinel (disarm). True if one was removed."""
    if not conv_id:
        return False
    existing = _queue_client().query(
        "queue.autopilot.get", {"conv_id": conv_id, "user_id": int(user_id)}
    )
    if not existing:
        return False
    result = _queue_client(write=True).command(
        "queue.autopilot.clear",
        {"conv_id": conv_id, "user_id": int(user_id)},
        command_id=f"queue-autopilot-clear:{conv_id}:{existing['queueId']}",
    )
    return bool(result.get("cleared"))


# The documented get_queue preview contract — the HTTP poll surface
# (routes/chat_queue.py). Peer attribution keys ride along conditionally.
_QUEUE_PREVIEW_KEYS = (
    "queueId",
    "position",
    "kind",
    "priority",
    "text",
    "sourceMessageId",
    "hasImages",
    "hasPdfs",
    "hasRefs",
    "hasQuotes",
    "timestamp",
    "isPeerMessage",
    "fromConv",
    "isPeerHuman",
)


def get_queue(conv_id: str, *, user_id: int) -> list[dict]:
    """Get all queued messages for a conversation, ordered by position.

    Returns:
        List of dicts with keys: queueId, position, text (preview),
        hasImages, hasPdfs, hasRefs, hasQuotes, timestamp.
    """
    rows = (
        _queue_client().query(
            "queue.list", {"conv_id": conv_id, "user_id": int(user_id)}
        )
        or []
    )
    return [{k: row[k] for k in _QUEUE_PREVIEW_KEYS if k in row} for row in rows]


def remove_from_queue(
    conv_id: str,
    queue_id: str,
    *,
    user_id: int,
) -> bool:
    """Remove a specific message from the queue.

    Returns:
        True if removed, False if not found.
    """
    result = _queue_client(write=True).command(
        "queue.remove",
        {
            "conv_id": conv_id,
            "queue_id": queue_id,
            "user_id": int(user_id),
        },
        command_id=f"queue-remove:{conv_id}:{queue_id}",
    )
    return bool(result.get("removed"))


def dedup_peer_durable_rows(
    conv_id: str,
    queue_ids,
    *,
    user_id: int,
) -> int:
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
            if remove_from_queue(conv_id, qid, user_id=user_id):
                removed += 1
        except Exception as e:
            logger.warning(
                "[Queue] peer durable-row de-dup failed conv=%s "
                "queueId=%s: %s — the row may re-dispatch as a "
                "duplicate",
                conv_id[:8],
                str(qid)[:8],
                e,
            )
    if removed:
        logger.info(
            "[Queue] forward de-dup removed %d peer durable row(s) for "
            "conv=%s (delivered via the fast-path inbox)",
            removed,
            conv_id[:8],
        )
    return removed


def clear_queue(conv_id: str, *, user_id: int) -> int:
    """Clear all queued messages for a conversation.

    Returns:
        Number of messages removed.
    """
    queued = _queue_client().query(
        "queue.list", {"conv_id": conv_id, "user_id": int(user_id)}
    )
    if not queued:
        return 0
    result = _queue_client(write=True).command(
        "queue.clear",
        {"conv_id": conv_id, "user_id": int(user_id)},
        command_id="queue-clear:%s:%s"
        % (conv_id, ",".join(row["queueId"] for row in queued)),
    )
    return int(result.get("cleared", 0))


def dequeue_next(conv_id: str, *, user_id: int) -> dict | None:
    """Pop the next message from the queue (lowest position).

    Returns:
        Full message dict (payload + config) or None if queue is empty.
    """
    return _queue_client(write=True).command(
        "queue.dequeue",
        {
            "conv_id": conv_id,
            "user_id": int(user_id),
            "now_ms": int(time.time() * 1000),
            "lease_ms": _QUEUE_LEASE_MS,
        },
        command_id=None,
    )


def _release_queue_lease(queue_id: str, *, user_id: int) -> None:
    """Release a dispatch lease immediately (used by every failure path).

    Best-effort — a failure here only delays re-dispatch until lease expiry;
    it can never lose the row (the row is only deleted on spawn success).
    """
    try:
        _queue_client(write=True).command(
            "queue.lease.release",
            {"queue_id": queue_id, "user_id": int(user_id)},
            command_id=f"queue-release:{queue_id}",
        )
    except Exception as e:
        logger.warning("[Queue] lease release failed for %s: %s", queue_id[:8], e)


def _finalize_queue_dispatch(
    conv_id: str,
    queue_id: str,
    *,
    user_id: int,
) -> None:
    """Delete a successfully-dispatched row + renumber (the deferred delete).

    This is the only success-path delete. It runs after command acceptance, so
    the queue row or its idempotent turn command owns the input at every point.
    """
    _queue_client(write=True).command(
        "queue.finalize",
        {
            "conv_id": conv_id,
            "queue_id": queue_id,
            "user_id": int(user_id),
        },
        command_id=f"queue-finalize:{conv_id}:{queue_id}",
    )


def _conv_has_live_task(conv_id: str, *, user_id: int) -> bool:
    """True if the in-memory registry holds a running, non-aborted task for
    the conv. Shared by the startup orphan scan and the lease reaper (the
    per-conv guard that prevents double-dispatch). Best-effort False on error.
    """
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        return any(
                t.get("convId") == conv_id
                and t.get("status") == "running"
                and not t.get("aborted")
                for t in chat_task_runtime.snapshot_owned(user_id=int(user_id))
            )
    except Exception as e:
        logger.debug("[Queue] live-task probe failed for conv=%s: %s", conv_id[:8], e)
        return False


def _stamp_queued_turn_initiator(user_msg: dict, payload: dict) -> None:
    """Project one queue payload onto the canonical turn-initiator field.

    Keeping attribution at the queue-to-turn boundary prevents an
    engine-injected brain/peer turn from becoming human-authored input.
    """
    from lib.turn_initiation import (
        INITIATOR_BRAIN,
        INITIATOR_OPERATOR,
        INITIATOR_PEER,
        stamp_initiator,
    )

    if payload.get("_peerMessage"):
        stamp_initiator(
            user_msg,
            INITIATOR_OPERATOR if payload.get("_peerHuman") else INITIATOR_PEER,
        )
    if payload.get("_brainDispatch"):
        # Preserve the historical precedence if a malformed payload carries
        # both markers: the workflow/brain identity is the more specific lane.
        stamp_initiator(user_msg, INITIATOR_BRAIN)


def _submit_queued_turn_command(
    conv_id: str,
    user_id,
    command_body: dict,
) -> dict:
    """Submit through the shared application service and return its value.

    This is the queue's single outbound port. Keeping it small makes lease and
    ordering contracts independently testable without replacing task-manager
    internals or starting model workers.
    """
    from lib.conversation_sync.runtime import conversation_turn_commands

    return conversation_turn_commands.create_turn(
        conv_id,
        user_id,
        command_body,
        request_started_at=time.time(),
    ).value


def _dispatch_queued_turn(
    conv_id: str,
    item: dict,
    payload: dict,
    config: dict,
    pre_built_user_msg,
    *,
    user_id: int,
) -> str | None:
    """Dispatch a dequeued row as a fresh turn pair on the main lane.

    The row is already dequeued (leased) by the caller. On success the pair
    is created and its first attempt is started through the SAME application
    command service as the HTTP routes (claim/bind/executor flags included),
    then the queue row is finalized and clients are notified. On a lane-busy
    race the lease is released so the occupying attempt's settlement hook
    re-drains later.
    """
    from lib.conversation_sync.command_service import AttemptStartFailure
    from lib.turn_lifecycle import LifecycleConflict, LifecycleNotFound

    if pre_built_user_msg:
        user_msg = dict(pre_built_user_msg)
    else:
        # Engine-built rows (brain kickoff / peer inject) carry no _user_msg.
        # The manual-enqueue API that once needed dispatch-time translation
        # was deleted 2026-05-29, so every row arriving here has final text.
        user_msg = {
            "role": "user",
            "content": payload.get("text", "") or "",
            "timestamp": payload.get("timestamp", int(time.time() * 1000)),
        }
        for key in (
            "images",
            "pdfTexts",
            "replyQuotes",
            "convRefs",
            "convRefTexts",
            "originalContent",
            "_peerMessage",
            "_fromConv",
            "_peerHuman",
            "_brainDispatch",
            "_boardTaskId",
            "_brainEpic",
            "_msgId",
        ):
            if payload.get(key):
                user_msg[key] = payload[key]
        # The durable queue/board contract uses ``boardTaskId`` while the
        # canonical conversation-turn projection intentionally uses the
        # private attribution key consumed by the UI and recovery lanes.
        if payload.get("boardTaskId"):
            user_msg["_boardTaskId"] = payload["boardTaskId"]
    _stamp_queued_turn_initiator(user_msg, payload)

    command_body = {
        # The queue row id keys idempotency: a crash between pair-create and
        # finalize replays to the SAME pair instead of duplicating the input.
        "commandId": f"queue:{item['queueId']}",
        "inputTurn": user_msg,
        "config": config,
        "laneId": "main",
        "kind": "reply",
        "actor": "assistant",
    }
    start_error = None
    try:
        result = _submit_queued_turn_command(conv_id, user_id, command_body)
        if result.get("aborted"):
            # A Stop won the internal command's start window before a pair was
            # allocated. The durable row still owns the input; release it for
            # a later explicit drain instead of deleting user intent.
            _release_queue_lease(item["queueId"], user_id=user_id)
            return None
        attempt_id = result["attempt"]["attemptId"]
    except AttemptStartFailure as exc:
        # The command service already settled the durable attempt and attached
        # recovery options to the output turn. Its public HTTP adapter raises
        # to return 500; the queue has a different duty: finalize this row so
        # it cannot replay forever. Callers only truth-test the returned id, so
        # the durable output turn is an honest fallback identity here.
        start_error = exc
        latest_turn = exc.latest_turn or {}
        attempt_id = str(
            latest_turn.get("activeAttemptId")
            or latest_turn.get("turnId")
            or item["queueId"]
        )
    except LifecycleConflict as exc:
        # The lane filled between the settlement hook and this drain (a
        # human submit wins, an autopilot/continuation successor claimed it,
        # …). Release the row; the occupying attempt's own settlement hook
        # re-drains when IT concludes.
        logger.info(
            "[Queue] lane busy at drain time conv=%s (%s) — row stays queued",
            conv_id[:8],
            exc.code,
        )
        _release_queue_lease(item["queueId"], user_id=user_id)
        return None
    except LifecycleNotFound:
        # Delete may commit after this worker leased the row. The active
        # conversation is gone, so the leased payload can never become a turn.
        _finalize_queue_dispatch(conv_id, item["queueId"], user_id=user_id)
        logger.warning(
            "[Queue] Dropped queued row %s for deleted conversation %s",
            item["queueId"][:8],
            conv_id[:8],
        )
        return None
    except Exception as exc:
        # The queue-id command key makes retry safe even if the command
        # committed and only its acknowledgement was lost.
        _release_queue_lease(item["queueId"], user_id=user_id)
        logger.error(
            "[Queue] queued command failed conv=%s row=%s: %s",
            conv_id[:8],
            item["queueId"][:8],
            exc,
            exc_info=True,
        )
        return None

    if start_error is not None:
        # The pair exists but the executor refused the start. The command
        # service settled it and the turn carries resume options; do not
        # requeue or the same idempotent pair would replay forever.
        logger.warning(
            "[Queue] queued turn created but executor start "
            "failed conv=%s attempt=%s — row finalized, resume "
            "options ride the turn",
            conv_id[:8],
            attempt_id[:8],
        )

    # No lease task-stamp: the public attempt shape deliberately never exposes
    # the executor taskId, and the attempt lifecycle owns recovery (resume-options +
    # killed-task recovery) for the died-before/during-start windows the
    # stamp exists to detect.

    try:
        _finalize_queue_dispatch(conv_id, item["queueId"], user_id=user_id)
    except Exception as e:
        logger.warning(
            "[Queue] deferred delete failed for %s: %s", item["queueId"][:8], e
        )

    logger.info(
        "[Queue] dispatched queued message → attempt %s for conv=%s",
        attempt_id[:8],
        conv_id[:8],
    )

    # Notify clients so open tabs hydrate + attach the fresh attempt (the
    # conversation-change handler re-reads the authoritative snapshot).
    try:
        from lib.conversations import notify_conv_changed
        from lib.turn_lifecycle import get_conversation_revision

        notify_conv_changed(
            conv_id,
            rev=get_conversation_revision(conv_id, user_id=user_id),
            user_id=user_id,
        )
    except Exception as e:
        logger.debug("[Queue] conv-changed notify failed: %s", e)

    # The dispatch contract's return value is only ever logged/truth-tested
    # by callers — the attempt id is the honest public identity of the
    # dispatched work (no public task id exists).
    return attempt_id


def _brain_kickoff_still_wanted(
    project_path: str | None, board_task_id: str, conv_id: str, *, user_id: int
) -> bool:
    """True iff a brain kickoff for ``board_task_id`` is still worth spawning.

    Consume-time re-check for the produce/consume gap ().
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

        board = read_board(project_path, user_id=user_id)
        epic = next(
            (t for t in board.get("tasks", []) if t.get("id") == board_task_id), None
        )
        if epic is None:
            logger.info(
                "[Queue] discarding brain kickoff conv=%s epic=%s — board row is gone",
                conv_id[:8],
                board_task_id,
            )
            return False
        status = epic.get("status") or ""
        if status == "done":
            logger.info(
                "[Queue] discarding brain kickoff conv=%s epic=%s — "
                "epic already DONE (finished while the kickoff sat in "
                "the queue; spawning would re-verify finished work)",
                conv_id[:8],
                board_task_id,
            )
            return False
        owner = epic.get("owner_conv_id") or ""
        if status == "claimed" and owner and owner != conv_id:
            logger.info(
                "[Queue] discarding brain kickoff conv=%s epic=%s — "
                "now live-claimed by conv=%s",
                conv_id[:8],
                board_task_id,
                owner[:8],
            )
            return False
        return True
    except Exception as e:
        logger.warning(
            "[Queue] brain-kickoff board re-check failed conv=%s "
            "epic=%s (dispatching anyway): %s",
            conv_id[:8],
            board_task_id,
            e,
        )
        return True


def dispatch_next_queued(
    conv_id: str,
    *,
    user_id: int,
    _wait: float | None = None,
) -> str | None:
    """Dispatch the next queued source as an authoritative turn attempt.

    Called when a conversation becomes idle. The first dispatchable row is
    leased, converted to a normalized input/output pair, and started through
    the same command service as HTTP submissions.

    ``_wait`` bounds the dispatch-lock wait in seconds; None (default) waits
    forever — every steady-state caller. The lease reaper passes a small bound
    so a wedged in-flight dispatch can never wedge the maintenance tick.

    Returns:
        The public attempt id if dispatched, otherwise ``None``.
    """
    if _wait is None:
        _dispatch_lock.acquire()
    elif not _dispatch_lock.acquire(timeout=_wait):
        logger.info(
            "[Queue] dispatch lock busy (>%ss) conv=%s — tick skips", _wait, conv_id[:8]
        )
        return None
    try:
        # ── Per-conv double-dispatch guard ──
        # A dispatched task keeps running ASYNC after this function returns,
        # so a second dispatch entering the lock milliseconds later would
        # dequeue + append + spawn AGAIN. Measured 2026-08-04 (conv
        # msco7vqmkf8yb2): two kickoffs appended 463 ms apart, tasks
        # 17582690/cebd5669 overlapping ~5 s — the first ended empty-'done',
        # leaving the persisted user,user adjacency behind the llm_sanitize
        # merge-warning storm. Every legitimate caller drains only when no
        # task is live (the completing task is already terminal by the time
        # its completion hook runs this), so a live task here always means a
        # premature caller — leave the row queued for the completion hook.
        if _conv_has_live_task(conv_id, user_id=user_id):
            logger.info(
                "[Queue] conv=%s already has a live task — dispatch "
                "refused; the completion hook will drain",
                conv_id[:8],
            )
            return None
        item = dequeue_next(conv_id, user_id=user_id)
        if not item:
            return None

        payload = item["payload"]
        config = item["config"]
        text = payload.get("text", "")

        # ── Stale brain-kickoff discard () ──
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
        # The filter itself lives in ``_row_is_dispatchable`` — the SINGLE
        #   consume-time predicate this function shares with the autopilot
        #   hook's yield gate. Do NOT re-inline a filter here: a filter that
        #   only one of the two readers applies is exactly what let a queued
        #   kickoff read as "a turn is waiting" to autopilot and as "discard
        #   me" to this dispatcher, destroying a finished VU turn and spawning
        #   nothing (conv ms3s8s0kjlvq18, 2026-07-28).
        if not _row_is_dispatchable(conv_id, payload, config, user_id=user_id):
            _finalize_queue_dispatch(conv_id, item["queueId"], user_id=user_id)
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
        if payload.get("_peerMessage") and item.get("queueId"):
            try:
                from lib.agent_inbox import consume_peer

                consume_peer(conv_id, [item["queueId"]])
            except Exception as e:
                logger.debug(
                    "[Queue] peer inbox-twin de-dup skipped conv=%s: %s", conv_id[:8], e
                )
        # _user_msg: pre-built (and already translated) user message dict
        #   from /api/chat/send.  If present, skip translation and use directly.
        pre_built_user_msg = payload.get("_user_msg")

        logger.info(
            "[Queue] Dispatching queued message for conv=%s text=%d chars pre_built=%s",
            conv_id[:8],
            len(text),
            bool(pre_built_user_msg),
        )

        # Every queued source becomes a normalized input/output turn pair.
        # There is no archive-writing or raw-task fallback: retry safety comes
        # from the queue-id command key and the attempt lifecycle.
        return _dispatch_queued_turn(
            conv_id, item, payload, config, pre_built_user_msg, user_id=user_id
        )
    finally:
        _dispatch_lock.release()


def get_queue_depth(conv_id: str, *, user_id: int) -> int:
    """Return the authority's count of non-marker queue rows.

    ⚠️ This is a COUNT with a WEAK filter (kind only) — it deliberately does
    NOT apply the consume-time filters that decide whether a row will really
    become a turn. Use it for badges/telemetry, NEVER to answer "is a turn
    about to take over?" — for that ask :func:`has_pending_human_turn` /
    :func:`next_dispatchable_turn`, which route through the single
    ``_row_is_dispatchable`` predicate.
    """
    result = _queue_client().query(
        "queue.depth", {"conv_id": conv_id, "user_id": int(user_id)}
    )
    return int(result.get("depth", 0))


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


def _row_is_dispatchable(
    conv_id: str, payload: dict, config: dict, *, user_id: int
) -> bool:
    """True iff this queued row would really be dispatched as a turn.

    Args:
        conv_id: Owning conversation id.
        payload: The row's decoded payload dict.
        config: The row's decoded config dict.

    Returns:
        ``False`` only when a consume-time filter rejects the row. Fails OPEN
        (see ``_brain_kickoff_still_wanted``): an unrelated lookup error must
        never silently swallow a legitimate turn.
    """
    board_task_id = (payload or {}).get("boardTaskId")
    if board_task_id and not _brain_kickoff_still_wanted(
        (config or {}).get("projectPath"), board_task_id, conv_id, user_id=user_id
    ):
        return False
    return True


def _dispatchable_rows(conv_id: str, *, user_id: int) -> list[dict]:
    """Queued rows that would REALLY be dispatched, in dispatch order.

    Mirrors ``dequeue_next``'s row selection (non-autopilot kinds, lease-aware,
    ``priority ASC, position ASC``) and then applies ``_row_is_dispatchable``
    to each — but takes NO lease and mutates nothing, so it is safe to ask
    from a decision gate.

    Returns a list of ``{'queueId', 'kind', 'isHuman'}``.
    """
    rows = (
        _queue_client().query(
            "queue.list", {"conv_id": conv_id, "user_id": int(user_id)}
        )
        or []
    )
    out: list[dict] = []
    for row in rows:
        kind = row.get("kind") or KIND_REAL
        if kind == KIND_AUTOPILOT:
            continue
        payload = row.get("payload") or {}
        config = row.get("config") or {}
        if not _row_is_dispatchable(conv_id, payload, config, user_id=user_id):
            continue
        out.append(
            {"queueId": row["queueId"], "kind": kind, "isHuman": kind == KIND_REAL}
        )
    return out


def next_dispatchable_turn(conv_id: str, *, user_id: int) -> dict | None:
    """The next queued turn that would REALLY be dispatched, or ``None``.

    Returns ``{'queueId', 'kind', 'isHuman'}`` for the head of the dispatchable
    queue. ``None`` means nothing here will become a turn — so a caller that
    stands down for it would be standing down for nobody.
    """
    if not conv_id:
        return None
    rows = _dispatchable_rows(conv_id, user_id=user_id)
    return rows[0] if rows else None


def has_pending_human_turn(conv_id: str, *, user_id: int) -> bool:
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
        rows = _dispatchable_rows(conv_id, user_id=user_id)
        return any(r["isHuman"] for r in rows)
    except Exception as e:
        logger.debug(
            "[Queue] human-turn probe failed conv=%s (non-fatal): %s", conv_id[:8], e
        )
        return False

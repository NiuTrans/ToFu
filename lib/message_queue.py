"""lib/message_queue.py — Unified priority turn-source queue for conversations.

The queue holds the *sources* of upcoming conversation turns, ordered by
priority.  Five kinds of source share one table:

  • ``real``          — a human message (highest priority).
  • ``goal_continuation`` — one explicit, cancellable Goal Mode command.
  • ``peer_msg``      — a turn sent by another conversation.
  • ``workflow_step`` — a turn injected by an orchestration workflow
                        (medium priority; reserved for the workflow engine).
  • ``autopilot``     — a legacy armed-marker sentinel (lowest priority).

Every row except the compatibility-only ``autopilot`` sentinel is
dispatchable. Goal Mode does not use that sentinel: arming behind a live turn
queues one ``goal_continuation`` command, and disarming can remove precisely
that intent without deleting queued human or workflow work.

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
 The one
exception: permanently undispatchable rows — a materialized pair whose
attempt settled while queue-bound, or whose pair turns were deleted — are
retired on the next drain instead of being re-leased into an infinite
conflict loop.
"""

import json
import os
import threading
import time
import uuid
import weakref
from collections.abc import Mapping

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.turn_source_queue_contract import (
    KIND_AUTOPILOT,
    KIND_BACKGROUND_RECEIPT,
    KIND_GOAL_CONTINUATION,
    KIND_PEER_MSG,
    KIND_REAL,
    KIND_WORKFLOW as KIND_WORKFLOW,
    DEFAULT_BACKGROUND_RECEIPT_PAGE_ROWS,
    QUEUE_REAP_PROBE_CONTRACT,
    QUEUE_REAP_PROBE_CONVERSATIONS_FIELD,
    QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD,
    QUEUE_REAP_PROBE_REQUEST_FIELD,
    QUEUE_REAP_PROBE_RESPONSE_FIELD,
    turn_source_priority,
)

logger = get_logger(__name__)

# Lock for dispatch coordination (prevent double-dispatch races)
_dispatch_lock = threading.Lock()
_background_batch_locks_guard = threading.Lock()
_background_batch_locks = weakref.WeakValueDictionary()

_BACKGROUND_BATCH_PROMPT = (
    'The background commands for the previous task have finished. Review '
    'the attached results together, complete any remaining work, and give '
    'the user one consolidated update.'
)


def _background_batch_lock_for(conv_id: str, user_id: int):
    """Serialize one conversation's wake without retaining idle lock entries."""
    key = (int(user_id), conv_id)
    with _background_batch_locks_guard:
        lock = _background_batch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _background_batch_locks[key] = lock
        return lock

def _priority_for_kind(kind: str) -> int:
    return turn_source_priority(kind)


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


def _json_safe(value):
    """Recursively coerce a value into something the strict JSON codec accepts.

    The queue row is written through storage.v2's strict JSON codec. Callers
    can attach process-private objects — a ``MappingProxyType`` from a live
    config bag, a tuple, a set — that the old SQLite path silently tolerated
    but the codec now rejects, dropping the durable completion (2026-09-24
    background-command delivery loss). Sanitize at the enqueue boundary so
    every caller is protected, not only the one path that first tripped:
    mappings become dicts, sequences/sets become lists, and any scalar the
    codec cannot represent is dropped (returns ``_UNDROP`` so the caller
    omits the key rather than corrupt the payload).
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            safe = _json_safe(item)
            if safe is not _UNDROP:
                cleaned[str(key)] = safe
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        cleaned = []
        for item in value:
            safe = _json_safe(item)
            if safe is not _UNDROP:
                cleaned.append(safe)
        return cleaned
    return _UNDROP


class _Undrop:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '_UNDROP'


_UNDROP = _Undrop()


def freeze_queue_payload(value: dict) -> dict:
    """Return an owned, strict-JSON-safe queue payload before retry identity freezes."""
    return _json_safe(value) or {}


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
        message_data: Dict with keys: text, images, attachments, legacy
                      pdfTexts/videos, replyQuotes,
                      convRefs, convRefTexts, originalContent, timestamp.
                      For an ``autopilot`` sentinel this is an empty/marker
                      dict (the row is never dispatched as a task).
        config: The chat config to use when dispatching this message
                (model, searchMode, tools, etc.).
        kind: Turn source — ``KIND_REAL`` (default),
              ``KIND_GOAL_CONTINUATION``, ``KIND_PEER_MSG``,
              ``KIND_WORKFLOW``, ``KIND_AUTOPILOT`` or passive
              ``KIND_BACKGROUND_RECEIPT``. Determines the priority bucket.

    Returns:
        Dict with queueId, position, and kind.
    """
    # Durable boundary: the row is JSON-serialized by storage.v2. Sanitize
    # both dicts so a process-private object in a caller's config/message
    # cannot abort the durable write and lose the turn (mappingproxy loss).
    message_data = _json_safe(message_data) or {}
    config = _json_safe(config) or {}
    completion_id = message_data.get('_backgroundCommand')
    queue_id = (
        str(uuid.uuid5(uuid.NAMESPACE_URL,
                       f'tofu:background:{int(user_id)}:{conv_id}:{completion_id}'))
        if (kind in (KIND_WORKFLOW, KIND_BACKGROUND_RECEIPT)
            and isinstance(completion_id, str) and completion_id)
        else str(uuid.uuid4())
    )
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
            "created_at_ms": (
                int(message_data.get('timestamp') or 0) if completion_id
                else int(time.time() * 1000)
            ),
        },
        command_id=queue_id,
    )
    if kind == KIND_REAL:
        try:
            _preempt_autonomous_work_for_real_message(
                conv_id, user_id=int(user_id))
        except Exception as e:
            logger.warning(
                "[Queue] Goal preempt on enqueue failed conv=%s: %s",
                conv_id[:8], e,
            )
    return result


def _preempt_autonomous_work_for_real_message(
    conv_id: str,
    *,
    user_id: int,
) -> bool:
    """Abort live Goal work superseded by a newly durable human message.

    New Goal Mode has one Flow-managed root task; compatibility mode may still
    have a standalone VU sub-task. Both honor cooperative abort checkpoints.
    A normal worker or a user-selected non-Goal Flow is never touched.

    Best-effort failure cannot lose the already committed human queue row. A
    Goal continuation also performs a durable preflight before execution, so
    the registration race cannot restore a stale objective.
    """
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        candidates = []
        for task in chat_task_runtime.snapshot_owned(user_id=int(user_id)):
            config = task.get("config")
            config = config if isinstance(config, dict) else {}
            is_goal_root = bool(task.get("_goalRunId")) or bool(
                task.get("_flow_managed")
                and task.get("flow_mode")
                and config.get("autopilot") is True
            )
            is_compatibility_vu = bool(task.get("_vu_subtask"))
            if (
                task.get("convId") == conv_id
                and task.get("status") in ("pending", "running")
                and not task.get("aborted")
                and (is_goal_root or is_compatibility_vu)
            ):
                candidates.append((task, is_goal_root))
        if not candidates:
            return False
        from lib.log import audit_log

        preempted = False
        for task, is_goal_root in candidates:
            task_id = str(task.get("id") or "")
            if not chat_task_runtime.abort_owned(task_id, user_id=int(user_id)):
                continue
            reason = (
                "superseded_by_human"
                if is_goal_root else "real_message_preempts_vu"
            )
            chat_task_runtime.update_fields(
                task_id,
                fields={
                    "aborted": True,
                    "_abort_timestamp": time.time(),
                    "_abort_reason": reason,
                },
                only_if_status=("pending", "running"),
            )
            preempted = True
            audit_log(
                (
                    "goal_run_preempted_by_real_message"
                    if is_goal_root else "vu_preempted_by_real_message"
                ),
                conv_id=conv_id,
                task_id=task_id,
            )
            logger.info(
                "[Queue] Real message supersedes %s task %s for conv=%s; "
                "the queued human turn owns the next lane",
                "GoalRun" if is_goal_root else "compatibility VU",
                task_id[:8],
                conv_id[:8],
            )
        return preempted
    except Exception as e:
        logger.warning(
            "[Queue] Goal preempt probe failed conv=%s: %s", conv_id[:8], e)
        return False


def list_orphaned_dispatchable_conversations() -> list[dict]:
    """Return conversations with active queue rows for startup recovery.

    Passive completion receipts are excluded. Old workflow completion rows
    can still appear here; dispatch_next_queued checks the exact dispatchable
    predicate before leasing one, so they never start a synthetic turn.

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


def _validated_reap_probe(response) -> tuple[bool, list[dict]] | None:
    """Decode only the exact additive Sidecar capability response."""
    if (not isinstance(response, dict)
            or response.get(QUEUE_REAP_PROBE_RESPONSE_FIELD)
            != QUEUE_REAP_PROBE_CONTRACT
            or not isinstance(
                response.get(QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD), bool,
            )):
        return None
    raw_conversations = response.get(QUEUE_REAP_PROBE_CONVERSATIONS_FIELD)
    if not isinstance(raw_conversations, list):
        return None
    conversations: list[dict] = []
    for raw in raw_conversations:
        if not isinstance(raw, dict):
            return None
        conv_id = raw.get("convId")
        user_id = raw.get("userId")
        if (not isinstance(conv_id, str)
                or not conv_id
                or len(conv_id) > 256
                or isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1):
            return None
        conversations.append({"convId": conv_id, "userId": user_id})
    return (
        response[QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD], conversations,
    )


def _reap_queue_leases(*, now_ms: int, force_reclaim: bool) -> None:
    reclaim_mode = "force" if force_reclaim else "normal"
    _queue_client(write=True).command(
        "queue.reap",
        {
            "now_ms": now_ms,
            "force_reclaim": bool(force_reclaim),
        },
        command_id=f"queue-reap:{now_ms}:{reclaim_mode}",
    ) or {}


def reap_expired_queue_leases(force_reclaim: bool = False) -> list[str]:
    """Probe/repair leases, then drain every orphaned queue conversation.

    Normal maintenance gets its ordered dispatch list and exact
    ``hasExpiredLeases`` bit from one read-pool query. ``queue.reap`` enters the
    writer only when that exact capability echo proves repair is useful;
    startup still force-reclaims every predecessor lease. The list deliberately
    includes *all* durable dispatchable rows, not only rows whose lease needs
    repair. That closes the submit-failure window: an immediately released row
    still needs a consumer even though it never becomes expired.

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
    conversations = None
    probe_response = None
    if not force_reclaim:
        probe_response = _queue_client().query(
            "queue.conversations.list_all",
            {
                QUEUE_REAP_PROBE_REQUEST_FIELD: QUEUE_REAP_PROBE_CONTRACT,
                "now_ms": now_ms,
            },
        )
        reap_probe = _validated_reap_probe(probe_response)
        if reap_probe is not None:
            has_expired_leases, conversations = reap_probe
            if has_expired_leases:
                _reap_queue_leases(
                    now_ms=now_ms, force_reclaim=False,
                )
    if conversations is None:
        # Old Sidecars ignore the additive selector and return the legacy bare
        # list. Keep their writer repair, but reuse that already-read list so
        # rolling compatibility still costs only the former two RPCs.
        _reap_queue_leases(
            now_ms=now_ms, force_reclaim=force_reclaim,
        )
        conversations = (
            probe_response
            if isinstance(probe_response, list)
            else list_orphaned_dispatchable_conversations()
        )
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
                "queue.list", {"conv_id": conv_id, "user_id": int(user_id),
                               "mode": "dispatchable"}
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
    "hasAttachments",
    "hasRefs",
    "hasQuotes",
    "timestamp",
    "isPeerMessage",
    "fromConv",
    "isPeerHuman",
    "inputTurnId",
    "outputTurnId",
    "attemptId",
    # Present (True) when a steer ("insert now") degraded to the durable
    # queue — the queue bar must be able to explain why this row waits
    # for the next turn instead of injecting mid-reply.
    "steerFallback",
    "backgroundCommand",
)


def get_queue(conv_id: str, *, user_id: int) -> list[dict]:
    """Get all queued messages for a conversation, ordered by position.

    Returns:
        List of dicts with keys: queueId, position, text (preview),
        hasImages, hasPdfs, hasRefs, hasQuotes, timestamp.
    """
    rows = (
        _queue_client().query(
            "queue.list", {"conv_id": conv_id, "user_id": int(user_id),
                           "mode": "preview"}
        )
        or []
    )
    return [{k: row[k] for k in _QUEUE_PREVIEW_KEYS if k in row} for row in rows]


def background_receipt_page(conv_id: str, *, user_id: int, page_size: int = DEFAULT_BACKGROUND_RECEIPT_PAGE_ROWS,
                            cursor: dict | None = None) -> dict:
    """Read complete receipts under the storage response byte budget."""
    return _queue_client().query('queue.list', {
        'conv_id': conv_id, 'user_id': int(user_id), 'mode': 'background_page',
        'page_size': page_size, 'cursor': cursor,
    }) or {'items': [], 'nextCursor': None}


def _background_receipts(conv_id: str, *, user_id: int, max_items: int | None = None) -> list[dict]:
    result = []
    cursor = None
    page_rows = DEFAULT_BACKGROUND_RECEIPT_PAGE_ROWS
    while max_items is None or len(result) < max_items:
        requested_rows = page_rows if max_items is None else min(page_rows, max_items - len(result))
        page = background_receipt_page(conv_id, user_id=user_id,
            page_size=requested_rows, cursor=cursor)
        result.extend(page['items'])
        next_cursor = page.get('nextCursor')
        if next_cursor is None:
            break
        if next_cursor == cursor or not page['items']:
            raise StorageError('database_protocol_error', 'Background receipt page did not advance')
        cursor = next_cursor
        if len(page['items']) == requested_rows:
            page_rows *= 2  # Full windows grow; response bytes still split complete items.
    return sorted(result, key=lambda row: row.get('position', 0))


def pending_background_command_injections(
    conv_id: str, *, user_id: int,
    max_items: int | None = None,
) -> list[dict]:
    """Rebuild complete inbox twins from durable receipts for a new turn.

    This is also the upgrade path for old workflow completion rows. A receipt
    stays in queue authority until the provider-consumption ACK removes it.
    """
    rows = _background_receipts(conv_id, user_id=user_id, max_items=max_items)
    items = []
    for row in rows:
        if max_items is not None and len(items) >= max_items:
            break
        if row.get('kind') not in (KIND_BACKGROUND_RECEIPT, KIND_WORKFLOW):
            continue
        payload = row.get('payload') or {}
        command_id = payload.get('_backgroundCommand')
        queue_id = row.get('queueId')
        if (not isinstance(command_id, str) or not command_id
                or not isinstance(queue_id, str) or not queue_id
                or row.get('attemptId')):
            continue
        value = payload.get('text')
        if not isinstance(value, str) or not value:
            continue
        items.append({
            'queueId': queue_id,
            'commandId': command_id,
            'command': (row.get('backgroundCommand') or {}).get('command', ''),
            'backgroundCommand': row.get('backgroundCommand'),
            'value': value,
        })
    return items


def dispatch_completed_background_batch(conv_id: str, *, user_id: int) -> str | None:
    """Start one completion turn after every known command has a durable result.

    Passive receipts are the data; one workflow intent is the wake signal.
    Existing queued work takes precedence and consumes the receipts itself.
    The short lock coalesces simultaneous final command publications; the
    ordinary dispatch lock still owns task creation and lease safety.
    """
    if not conv_id:
        return None
    with _background_batch_lock_for(conv_id, user_id):
        from lib.tasks_pkg.background_commands import conversation_results_complete
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        if (not conversation_results_complete(conv_id=conv_id, user_id=user_id)
                or any(task.get('convId') == conv_id
                       and task.get('status') in ('pending', 'running')
                       and not task.get('aborted')
                       for task in chat_task_runtime.snapshot_owned(user_id=user_id))):
            return None
        receipts = _background_receipts(conv_id, user_id=user_id, max_items=None)
        if not receipts:
            return None
        if next_dispatchable_turn(conv_id, user_id=user_id) is None:
            from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator

            receipt_ids = sorted(str(row['queueId']) for row in receipts)
            queue_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f'tofu:background-batch:{user_id}:{conv_id}:{":".join(receipt_ids)}',
            ))
            created_at_ms = max(int(row.get('timestamp') or 1)
                                for row in receipts)
            user_message = stamp_initiator({
                'role': 'user', 'content': _BACKGROUND_BATCH_PROMPT,
                'timestamp': created_at_ms,
            }, INITIATOR_PROACTIVE)
            from lib.background_command_delivery import completion_delivery_config
            # Normalize even already-durable receipts from before this fix;
            # their frozen publication identity and bytes remain untouched.
            config = completion_delivery_config(receipts[0].get('config'))
            _queue_client(write=True).command('queue.enqueue', {
                'conv_id': conv_id, 'user_id': int(user_id),
                'queue_id': queue_id, 'kind': KIND_WORKFLOW,
                'priority': _priority_for_kind(KIND_WORKFLOW),
                'message': {
                    'text': _BACKGROUND_BATCH_PROMPT,
                    '_user_msg': user_message,
                    '_backgroundBatchWake': receipt_ids,
                },
                'config': config, 'created_at_ms': created_at_ms,
            }, command_id=queue_id)
    # Best-effort wake, never a blocking dispatch: this seam is reachable
    # while the caller TRANSITIVELY holds the non-reentrant
    # ``_dispatch_lock`` — ``dispatch_next_queued`` → ``_dispatch_queued_turn``
    # → ``create_turn`` → ``_throttled_cleanup_old_tasks`` →
    # ``cleanup_old_tasks`` → ``drain_idle_background_batches`` → here. An
    # unbounded acquire in that shape self-deadlocks the owning thread
    # (2026-09-28 boot wedge: startup background-command recovery froze the
    # readiness gate at 'Storage recovery' for 15+ minutes). A zero bound
    # turns the nested entry into the documented "lock busy — tick skips"
    # outcome; the in-flight dispatch's completion hook (or the next
    # maintenance tick) drains the wake instead.
    return dispatch_next_queued(conv_id, user_id=user_id, _wait=0)


def drain_idle_background_batches() -> list[str]:
    """Rebuild collective wakes after restart and on the maintenance tick."""
    conversations: dict[tuple[int, str], dict] = {}
    for kind in (KIND_BACKGROUND_RECEIPT, KIND_WORKFLOW):
        for row in _queue_client().query(
                'queue.conversations.list_all', {'kind': kind}) or []:
            owner = int(row['userId'])
            conv_id = str(row['convId'])
            conversations[(owner, conv_id)] = row
    spawned = []
    for owner, conv_id in conversations:
        if len(spawned) >= _reaper_max_dispatch_per_tick():
            break
        try:
            attempt_id = dispatch_completed_background_batch(
                conv_id, user_id=owner)
            if attempt_id:
                spawned.append(attempt_id)
        except Exception as exc:
            logger.warning('[Queue] background batch drain failed conv=%s: %s',
                           conv_id[:8], exc)
    return spawned


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
    queued = _queue_client().query(
        "queue.list", {"conv_id": conv_id, "user_id": int(user_id),
                       "mode": "preview"}
    ) or []
    item = next((row for row in queued if row.get("queueId") == queue_id), None)
    if not item:
        return False
    if item.get("attemptId"):
        # Conversation Sync v3 accepts a real input/output Turn pair before
        # placing it in the lane.  Its cancellation boundary must remove that
        # pair and the queue row in one transaction; deleting only the legacy
        # source row would strand an unclaimable pending Attempt forever.
        from lib.turn_lifecycle import cancel_queued_turn_pair

        result = cancel_queued_turn_pair(
            conv_id, queue_id, user_id=int(user_id),
        )
        return bool(result.get("cancelled"))
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


def dedup_inbox_durable_rows(
    conv_id: str,
    queue_ids,
    *,
    user_id: int,
) -> int:
    """Delete injection-lane durable rows by ``queueId`` (FORWARD-race de-dup).

    Shared by every dual-written injection lane (peer message, background-
    command completion): the live-target payload is written to BOTH a durable
    ``message_queue`` row AND a fast-path agent_inbox twin tagged with that
    row's ``queueId``. When the orchestrator's round-boundary drain injects
    the twin and the post-LLM flush confirms consumption, it calls THIS to
    delete the matching durable row(s) so ``dispatch_next_queued`` can never
    later pop acknowledged rows as redundant fresh turns. The
    REVERSE race (durable row dispatched first) is closed symmetrically by
    :func:`lib.agent_inbox.consume_peer` at dispatch time.

    Best-effort: a delete failure logs and preserves rows for redelivery.
    Model consumption and this storage transaction are not atomic.
    Returns the number removed.
    """
    if not conv_id:
        return 0
    try:
        batch = list(queue_ids or [])
        ids = sorted(set(q for q in batch if q))
        if not ids:
            return 0
        result = _queue_client(write=True).command(
            "queue.injection.ack",
            {"conv_id": conv_id, "user_id": int(user_id), "queue_ids": ids},
            command_id=f"queue-injection-ack:{uuid.uuid4()}",
        )
        removed = int(result.get("removed", 0))
    except Exception as e:
        logger.warning(
            "[Queue] injection durable-row de-dup failed conv=%s: %s "
            "— the rows may re-dispatch as duplicates", conv_id[:8], e,
        )
        return 0
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
        "queue.list", {"conv_id": conv_id, "user_id": int(user_id),
                       "mode": "preview"}
    )
    if not queued:
        return 0
    linked = [row for row in queued if row.get("attemptId")]
    cancelled = sum(
        1 for row in linked
        if remove_from_queue(conv_id, row["queueId"], user_id=int(user_id))
    )
    queued = [row for row in queued if not row.get("attemptId")]
    if not queued:
        return cancelled
    result = _queue_client(write=True).command(
        "queue.clear",
        {"conv_id": conv_id, "user_id": int(user_id)},
        command_id="queue-clear:%s:%s"
        % (conv_id, ",".join(row["queueId"] for row in queued)),
    )
    return cancelled + int(result.get("cleared", 0))


def clear_queue_kind(conv_id: str, kind: str, *, user_id: int) -> int:
    """Clear one source kind while preserving every other queued intent."""
    queued = [
        row for row in get_queue(conv_id, user_id=int(user_id))
        if row.get("kind") == kind
    ]
    if not queued:
        return 0
    linked = [row for row in queued if row.get("attemptId")]
    cancelled = sum(
        1 for row in linked
        if remove_from_queue(conv_id, row["queueId"], user_id=int(user_id))
    )
    queued = [row for row in queued if not row.get("attemptId")]
    if not queued:
        return cancelled
    result = _queue_client(write=True).command(
        "queue.kind.clear",
        {
            "conv_id": conv_id,
            "kind": kind,
            "user_id": int(user_id),
        },
        command_id="queue-kind-clear:%s:%s:%s" % (
            conv_id,
            kind,
            ",".join(row["queueId"] for row in queued),
        ),
    )
    return cancelled + int(result.get("cleared", 0))


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
            # Each failed dispatch owns a new lease on the same durable row.
            # A row-lifetime receipt would replay the first release forever
            # and leave every later lease held until expiry.
            command_id=f"queue-release:{queue_id}:{uuid.uuid4().hex}",
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
    engine-injected peer turn from becoming human-authored input.
    """
    from lib.turn_initiation import (
        INITIATOR_OPERATOR,
        INITIATOR_PEER,
        stamp_initiator,
    )

    if payload.get("_peerMessage"):
        stamp_initiator(
            user_msg,
            INITIATOR_OPERATOR if payload.get("_peerHuman") else INITIATOR_PEER,
        )


def _submit_queued_turn_command(
    conv_id: str,
    user_id,
    command_body: dict,
    *,
    trusted_goal_objective: str | None = None,
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
        trusted_goal_objective=trusted_goal_objective,
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
        # Engine-built peer rows carry no _user_msg.
        # The manual-enqueue API that once needed dispatch-time translation
        # was deleted 2026-05-29, so every row arriving here has final text.
        user_msg = {
            "role": "user",
            "content": payload.get("text", "") or "",
            "timestamp": payload.get("timestamp", int(time.time() * 1000)),
        }
        for key in (
            "images",
            "attachments",
            "videos",
            "pdfTexts",
            "replyQuotes",
            "convRefs",
            "convRefTexts",
            "originalContent",
            "_peerMessage",
            "_fromConv",
            "_peerHuman",
            "_msgId",
        ):
            if payload.get(key):
                user_msg[key] = payload[key]
    _stamp_queued_turn_initiator(user_msg, payload)
    if item.get('kind') == KIND_WORKFLOW and payload.get('_backgroundBatchWake'):
        from lib.background_command_delivery import completion_delivery_config
        config = completion_delivery_config(config)

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
    linked_turn_pair = bool(item.get("attemptId"))
    try:
        if linked_turn_pair:
            from lib.conversation_sync.runtime import conversation_turn_commands

            result = conversation_turn_commands.activate_queued_turn(
                conv_id,
                user_id,
                str(item["queueId"]),
                config=config,
                request_data=command_body,
            ).value
        elif item.get("kind") == KIND_GOAL_CONTINUATION:
            result = _submit_queued_turn_command(
                conv_id,
                user_id,
                command_body,
                trusted_goal_objective=str(
                    config.get("_goalObjective") or ""
                ).strip() or None,
            )
        else:
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
        if (
            item.get("kind") == KIND_GOAL_CONTINUATION
            and exc.code == "superseded_by_human"
        ):
            # Permanent intent supersession: retrying this leased synthetic
            # command could only restore its stale stamped objective.
            _finalize_queue_dispatch(
                conv_id, item["queueId"], user_id=user_id)
            logger.info(
                "[Queue] retired Goal continuation %s superseded by a "
                "newer human turn for conv=%s",
                str(item["queueId"])[:8], conv_id[:8],
            )
            return None
        if linked_turn_pair and exc.code != "lane_busy":
            # The materialized pair can never activate again: its attempt
            # settled while queue-bound (a Stop raced the drain). That is
            # permanent, not a lane race — retire the row, or every drain
            # replays this conflict and queue.depth (the composer badge)
            # stays pinned at 1.
            _finalize_queue_dispatch(
                conv_id, item["queueId"], user_id=user_id)
            logger.warning(
                "[Queue] retired undispatchable queued pair %s conv=%s (%s)",
                str(item["queueId"])[:8], conv_id[:8], exc.code,
            )
            return None
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
        if (
            linked_turn_pair
            and isinstance(exc, StorageError)
            and exc.code == "database_integrity"
        ):
            # The pair turns are gone (e.g. the user deleted the queued
            # turns) while the queue row survived: it can never activate, so
            # retire it instead of releasing the lease into an infinite
            # conflict loop.
            _finalize_queue_dispatch(
                conv_id, item["queueId"], user_id=user_id)
            logger.warning(
                "[Queue] retired queued row %s conv=%s: pair turns missing",
                item["queueId"][:8], conv_id[:8],
            )
            return None
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

    if not linked_turn_pair:
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
        # Older releases stored command completions as dispatchable workflow
        # rows. Keep them visible for the next genuine turn, never convert
        # them into a synthetic user/assistant pair during startup recovery.
        if not next_dispatchable_turn(conv_id, user_id=user_id):
            return None
        item = dequeue_next(conv_id, user_id=user_id)
        if not item:
            return None

        payload = item["payload"]
        config = item["config"]
        text = payload.get("text", "")

        if (
            item.get("kind") == KIND_GOAL_CONTINUATION
            and not str(config.get("_goalObjective") or "").strip()
        ):
            # Never turn a corrupted continuation into a new objective whose
            # text merely says "continue". The durable queue row is invalid
            # and cannot become correct through retry, so retire it once.
            _finalize_queue_dispatch(
                conv_id, item["queueId"], user_id=user_id)
            logger.error(
                "[Queue] discarded Goal Mode continuation without an "
                "authoritative objective conv=%s row=%s",
                conv_id[:8], str(item["queueId"])[:8],
            )
            return None

        # ── Injection-lane REVERSE-race de-dup (peer / background-command) ──
        # A dual-written injection payload lives as BOTH this durable row AND a
        # fast-path agent_inbox twin tagged with this row's queueId. If the
        # target's live turn ended BEFORE its next round-boundary drain, we pop
        # the durable row HERE and dispatch it as a fresh turn — so the still-
        # pending inbox twin must be dropped, or it would be re-injected on that
        # fresh turn = double delivery. (The forward race — inbox drains first —
        # is closed symmetrically by the post-LLM deferred flush, which deletes
        # this row by queueId.) The inbox is conv-keyed (swarm_key_for=convId).
        if (payload.get("_peerMessage") or payload.get("_backgroundCommand")) \
                and item.get("queueId"):
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
    """Return the authority's count of active queue rows.

    Passive receipts and autopilot markers are excluded. This is still a
    COUNT with a WEAK filter (kind only) — it deliberately does
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


def _dispatchable_rows(conv_id: str, *, user_id: int) -> list[dict]:
    """Queued rows that would REALLY be dispatched, in dispatch order.

    Mirrors ``dequeue_next``'s non-autopilot selection without taking a lease,
    so it is safe to ask from a decision gate.

    Returns a list of ``{'queueId', 'kind', 'isHuman'}``.
    """
    rows = (
        _queue_client().query(
            "queue.list", {"conv_id": conv_id, "user_id": int(user_id),
                           "mode": "preview"}
        )
        or []
    )
    out: list[dict] = []
    for row in rows:
        kind = row.get("kind") or KIND_REAL
        if (kind in (KIND_AUTOPILOT, KIND_BACKGROUND_RECEIPT)
                or row.get('backgroundCommand')):
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
    (``KIND_WORKFLOW`` jobs and ``KIND_PEER_MSG`` notifications) do
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

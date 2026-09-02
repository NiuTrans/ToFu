"""lib.conversations.project_dispatch — brain-driven dispatch (Pillar #5).

The last step that closes "无需人手": the Board lets conversations coordinate
when a human is driving them, but nothing on the board ever *starts* work. An
open epic with all dependencies met sits forever unless a human opens a tab.
This module is the spine that makes the project autonomous — it SELECTS
genuinely-pickable epics and KICKS them off into a conversation via the
existing ``message_queue`` (NOT a second turn-source), claiming each on
dispatch so siblings (and a re-dispatch pass) immediately avoid it.

Locked design (owner, 2026-06-30):

  • **Reuse the board's at-read-time lease eval.** ``select_dispatchable`` is
    built on ``read_board`` (whose ``_effective_status`` already reclaims an
    expired claim to ``open``) — there is exactly ONE deadlock path, not two.
  • **Dispatchable = open AND every dependency done AND no live claim.** An
    epic with an unfinished ``depends_on`` or a live (unexpired) claim is
    NEVER a candidate.
  • **Claim-on-dispatch = idempotency guard.** ``dispatch_epic`` claims the
    epic under the target conv BEFORE/with enqueuing the kickoff, so a second
    dispatch pass sees it as ``claimed`` and won't re-select it (no concurrent
    double-dispatch). The claim is the same soft lease — advisory, TTL-expiring.
  • **Trigger needs no new global / thread.** ``on_epic_completed`` is called
    from ``complete_task`` (a completion may unblock a dependent); it reuses
    the existing post-task ``dispatch_next_queued`` machinery to actually start
    the enqueued kickoff. No background poller is added here.
  • **Trigger needs no new global / thread.** ``on_epic_completed`` is called
    from ``complete_task`` (a completion may unblock a dependent); it reuses
    the existing post-task ``dispatch_next_queued`` machinery to actually start
    the enqueued kickoff. No background poller is added here.
  • **Event channel (2026-07-27).** The 30 s sweep is the crash/lease/strand
    SAFETY NET, not the starter: common flows dispatch AT THE EVENT —
    ``on_epic_posted`` (post time, idle existing target), ``on_conv_idle``
    (a task completes with an empty queue), ``on_epic_completed`` /
    ``on_epic_answered`` (dependency done / human answered).
  • **Per ``project_path``, never a process-global.**
"""

from __future__ import annotations

import time
import uuid

from lib.conversations.project_board_policy import DEFAULT_LEASE_TTL_MS
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client

logger = get_logger(__name__)


def _resolve_dispatch_config(target_conv_id: str, *, user_id: int) -> dict:
    """Build a runnable task config from the owned target conversation."""
    if not target_conv_id:
        return {}
    try:
        document = get_storage_client().query(
            "conversation.get",
            {"conv_id": target_conv_id, "user_id": int(user_id)},
        )
        settings = ((document or {}).get("metadata") or {}).get("settings") or {}
        from lib.scheduler._shared import build_task_config

        return build_task_config({}, settings)
    except Exception as error:
        logger.debug(
            "[Dispatch] config resolve failed conv=%s: %s", target_conv_id[:8], error
        )
        return {}


def _drain_idle_target(target_conv_id: str, *, user_id: int) -> str | None:
    """Start one queued kickoff when the owned target lane is idle."""
    if not target_conv_id or _conv_has_live_task(target_conv_id, user_id=user_id):
        return None
    try:
        document = get_storage_client().query(
            "conversation.get",
            {"conv_id": target_conv_id, "user_id": int(user_id)},
        )
        if not document:
            logger.debug(
                "[Dispatch] idle drain skipped missing conv=%s", target_conv_id[:8]
            )
            return None
    except Exception as error:
        logger.debug(
            "[Dispatch] idle-drain ownership probe failed conv=%s: %s",
            target_conv_id[:8],
            error,
        )
        return None

    try:
        from lib.message_queue import dispatch_next_queued

        task_id = dispatch_next_queued(target_conv_id, user_id=int(user_id))
        if task_id:
            logger.info(
                "[Dispatch] drained idle conv=%s -> task=%s",
                target_conv_id[:8],
                task_id[:8],
            )
        else:
            logger.warning(
                "[Dispatch] idle drain produced no task conv=%s; "
                "the durable kickoff remains recoverable",
                target_conv_id[:8],
            )
        return task_id
    except Exception as error:
        logger.error(
            "[Dispatch] idle drain failed conv=%s: %s",
            target_conv_id[:8],
            error,
            exc_info=True,
        )
        return None


# A brain-dispatched kickoff carries this marker in its queue payload so the
# turn is recognisable as engine-injected (NOT a human turn) downstream.
BRAIN_DISPATCH_MARKER = "_brainDispatch"

# Which dispatch SEAM fired the kickoff — stamped by each event seam on the
# epic dict it hands to dispatch_epic (``_via``), surfaced verbatim in the
# message's ``_brainEpic`` provenance card so the frontend can say HOW this
# conversation was picked. 'heartbeat' is the default (the 30 s sweep, the
# most common path — sweep_dispatch deliberately does not stamp).
DISPATCH_VIAS = frozenset(
    {
        "heartbeat",  # 30 s sweep_dispatch picked a genuinely-pickable epic
        "dependency_done",  # on_epic_completed — a dependency just finished
        "answered",  # on_epic_answered — the human answered the gate
        "posted",  # on_epic_posted — startable at post time
        "conv_idle",  # on_conv_idle — the conv just went idle
    }
)


def _resolve_conv_title(conv_id: str, *, user_id: int) -> str:
    """Resolve the owned conversation title for dispatch provenance."""
    conv_id = (conv_id or "").strip()
    if not conv_id:
        return ""
    try:
        document = get_storage_client().query(
            "conversation.get",
            {"conv_id": conv_id, "user_id": int(user_id)},
        )
        metadata = (document or {}).get("metadata") or {}
        return str(metadata.get("title") or "")
    except Exception as error:
        logger.debug("[Dispatch] title resolve failed conv=%s: %s", conv_id[:8], error)
        return ""


def _brain_meta(epic: dict, target_conv_id: str, *, user_id: int) -> dict:
    """The display-only provenance record stamped onto the kickoff payload as
    ``_brainEpic`` — WHO created the epic (title + conv id, for a clickable
    chip), HOW it was dispatched (which seam fired), and WHY this conversation
    was picked (its creator / migrated from an idle-stranded target / the
    completing-conv fallback). Display-only: conv_message_builder reads only
    ``content`` for the wire, so this never reaches the model.

    ``route`` derivation keys on the board's OWN provenance fields, not the
    seam: ``dispatch_target`` is the mutable routing override (idle-sibling
    migration sets it), ``created_by_conv`` the immutable authorship."""
    origin = (epic.get("created_by_conv") or "").strip()
    override = (epic.get("dispatch_target") or "").strip()
    target = (target_conv_id or "").strip()
    if override and target == override and origin and target != origin:
        route = "migrated"
    elif origin and target == origin:
        route = "creator"
    else:
        route = "fallback"
    via = str(epic.get("_via") or "heartbeat")
    if via not in DISPATCH_VIAS:
        via = "heartbeat"
    return {
        "epicId": epic.get("id") or "",
        # Display cap only — the kickoff text carries the full title.
        "epicTitle": (epic.get("title") or "")[:300],
        "originatorConv": origin,
        "originatorTitle": _resolve_conv_title(origin, user_id=user_id),
        "method": via,
        "route": route,
        "answered": bool((epic.get("human_answer") or "").strip()),
    }


def select_dispatchable(project_path: str, *, user_id: int) -> list[dict]:
    """Return board epics that are GENUINELY pickable right now.

    An epic qualifies iff (read via ``read_board``, so expired claims already
    read as open):
      • its effective status is ``open`` (NOT ``claimed`` with a live lease,
        NOT ``done``); AND
      • every id in its ``depends_on`` refers to an epic that is ``done``.

    Pure + side-effect-free — the testable core. Returns [] on no project.
    """
    if not project_path:
        return []
    import time as _time

    from lib.conversations.project_board import read_board

    board = read_board(project_path, user_id=user_id)
    tasks = board["tasks"]
    now_ms = int(_time.time() * 1000)
    # Dependencies are satisfied only by epics whose EFFECTIVE status is done.
    done_ids = {t["id"] for t in tasks if t["status"] == "done"}

    candidates = []
    for t in tasks:
        # ── kind filter: a 'lease' row is a durational resource/path
        #    RESERVATION, never a work-item. It MUST NOT be auto-dispatched —
        #    without this skip, an EXPIRED lease reclaims claimed→open (via
        #    _effective_status), passes the status=='open' check below, and the
        #    sweep + _drain_idle_target would spawn a spurious BILLED kickoff at
        #    TTL expiry. DENYLIST (not an allowlist on 'epic') so a
        #    pre-migration None/'' kind still reads as a dispatchable epic. ──
        if t.get("kind") == "lease":
            continue
        # ── live-claim filter: only OPEN epics are pickable. A claimed epic
        #    with an unexpired lease (effective status 'claimed') is excluded
        #    — never double-dispatch live-claimed work. ──
        if t["status"] != "open":
            continue
        # ── block-cooldown filter: an epic that hit a genuine external gate was
        #    stamped blocked_until = now + an escalating cooldown by block_task.
        #    While that window is live, SKIP it — this is what stops the ~30-min
        #    lease-expiry re-dispatch churn (a billed agent turn each cycle to
        #    re-discover the same unmet dep). At-READ-time expiry: once the
        #    window lapses the epic is pickable again (a resolved dep IS
        #    retried), with NO reaper and NO human un-block gate. ──
        if int(t.get("blocked_until") or 0) > now_ms:
            continue
        # ── pending-question filter: an epic blocked WITH a structured human
        #    question waits for the ANSWER, not for time — re-dispatching
        #    before the human answers can only re-discover the same gate (the
        #    billed-turn loop this redesign exists to kill). answer_task
        #    clears the question + cooldown, so an ANSWERED epic falls through
        #    to normal pick-up (and carries its answer into the kickoff). ──
        if t.get("block_question") and not (t.get("human_answer") or "").strip():
            continue
        # ── dependency filter: every dependency must be DONE. An epic with an
        #    unfinished (or unknown) dependency is NOT yet pickable. ──
        deps = t.get("depends_on") or []
        if any(d not in done_ids for d in deps):
            continue
        candidates.append(t)

    # ── Write-set partitioning: prefer a candidate
    #    whose declared write_set is DISJOINT from every LIVE-CLAIMED epic's
    #    write_set, so two conversations aren't handed epics that will fight
    #    over the same files. This is a SOFT preference (stable reorder,
    #    disjoint-first) NOT a hard filter — a conflicting epic is still
    #    dispatchable (last), and an epic with an empty/undeclared write_set is
    #    "unknown footprint" → treated as non-conflicting → never demoted. ──
    claimed_write_sets = [
        _write_set_of(t)
        for t in tasks
        if t.get("status") == "claimed" and _write_set_of(t)
    ]
    if len(candidates) > 1:

        def _demote_key(c):
            # A wait-on-path conflict (isolation-demoted above) OR a declared
            # write_set that overlaps a live-claimed epic's → hand out LAST.
            # Both are SOFT: a conflicting epic is still dispatchable, just
            # after every disjoint one, so no colliding pair is handed out
            # concurrently while independent work exists. Stable sort keeps
            # relative order within each bucket.
            if c.get("_conflict_demote"):
                return 1
            if claimed_write_sets and _write_set_conflicts(
                _write_set_of(c), claimed_write_sets
            ):
                return 1
            return 0

        candidates.sort(key=_demote_key)
    return candidates


def _write_set_of(task: dict) -> list:
    """The epic's declared write_set as a clean list of strings (empty when
    undeclared → unknown footprint → treated as non-conflicting)."""
    ws = task.get("write_set") or []
    return [str(s) for s in ws if isinstance(ws, list) and str(s).strip()]


def _paths_intersect(a: str, b: str) -> bool:
    """True iff two write-set entries name overlapping targets. Handles a
    plain-prefix / directory-containment relationship in EITHER direction
    (``lib/`` vs ``lib/x.py``) and exact match; a trailing-``*`` glob is
    treated as its directory prefix. Deliberately conservative — a false
    "overlap" only demotes an epic in the ordering (safe), never drops it."""
    a = (a or "").rstrip("/*")
    b = (b or "").rstrip("/*")
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _write_set_conflicts(ws: list, others: list) -> bool:
    """True iff ``ws`` shares any target with ANY of the ``others`` write-sets.
    An empty ``ws`` (unknown footprint) never conflicts (fail-open)."""
    if not ws:
        return False
    for other in others:
        for x in ws:
            for y in other:
                if _paths_intersect(x, y):
                    return True


def dispatch_epic(
    project_path: str,
    epic: dict,
    target_conv_id: str,
    *,
    user_id: int,
    config: dict | None = None,
) -> dict:
    """Atomically claim one epic and enqueue its autonomous kickoff."""
    if not project_path or not epic or not target_conv_id:
        return {"ok": False, "error": "missing project/epic/conv"}
    task_id = str(epic.get("id") or "")
    title = str(epic.get("title") or "").strip()
    if not task_id:
        return {"ok": False, "error": "epic has no id"}

    kickoff = (
        "[Project Brain — autonomous dispatch] Pick up this open project "
        f'epic: "{title}". Read the project board and charter, complete the '
        "work. If an external gate cannot "
        "be cleared, call project_board_block with a reason prefixed "
        "'[human-gated]' or '[sibling]'. For a human decision, include a "
        "structured question and options; do not silently no-op."
    )
    try:
        from lib.integration_control import peek_workspace_for_epic

        isolation = peek_workspace_for_epic(
            project_path, task_id, user_id=int(user_id))
    except Exception as error:
        logger.debug("[Dispatch] isolation probe failed epic=%s: %s", task_id, error)
        isolation = None
    if isolation and isolation.get("workspace_path"):
        kickoff += (
            "\n\nISOLATED WORKSPACE: do all file work under "
            f"{isolation['workspace_path']}. The canonical checkout is "
            "observation-only. Submit the result with integration_submit "
            f'for task_id "{task_id}"; use integration_status if it is '
            "already queued. Do not call project_board_complete: the board "
            "epic completes automatically only after the checkpoint passes "
            "its gate and moves candidate."
        )
    else:
        kickoff += "\n\nWhen the work is complete, call project_board_complete."

    answer = str(epic.get("human_answer") or "").strip()
    if answer:
        kickoff += (
            "\n\nThe human answered the earlier gate: "
            f'"{answer}". Proceed on that basis and do not re-ask it.'
        )

    dispatch_config = (
        config
        if config
        else _resolve_dispatch_config(target_conv_id, user_id=int(user_id))
    )
    queue_id = str(uuid.uuid4())
    message = {
        "text": kickoff,
        BRAIN_DISPATCH_MARKER: True,
        "boardTaskId": task_id,
        "_brainEpic": _brain_meta(epic, target_conv_id, user_id=int(user_id)),
    }
    try:
        result = get_storage_client(write=True).command(
            "board.dispatch",
            {
                "project_path": project_path,
                "task_id": task_id,
                "conv_id": target_conv_id,
                "user_id": int(user_id),
                "ttl_ms": DEFAULT_LEASE_TTL_MS,
                "queue_id": queue_id,
                "message": message,
                "config": dispatch_config,
                "priority": 50,
                "created_at_ms": int(time.time() * 1000),
            },
            queue_id,
        )
    except Exception as error:
        logger.error(
            "[Dispatch] atomic dispatch failed proj=%.40r epic=%s: %s",
            project_path,
            task_id,
            error,
            exc_info=True,
        )
        return {"ok": False, "error": str(error)}
    if not result.get("ok"):
        return result

    if result.get("transitioned", True):
        from lib.conversations.project_board import _emit

        _emit(
            "claimed",
            project_path,
            target_conv_id,
            f"Claimed: {result.get('title') or title}",
            user_id=int(user_id),
            payload={"taskId": task_id},
        )
        audit_log(
            "board_claim",
            project_path=project_path,
            task_id=task_id,
            conv_id=target_conv_id,
        )
    audit_log(
        "brain_dispatch",
        project_path=project_path,
        task_id=task_id,
        conv_id=target_conv_id,
        user_id=int(user_id),
    )
    logger.info(
        "[Dispatch] atomic epic dispatch %s -> conv=%s queue=%s",
        task_id,
        target_conv_id[:8],
        str(result.get("queueId") or "")[:8],
    )
    return {
        "ok": True,
        "queueId": result.get("queueId"),
        "deduped": bool(result.get("deduped")),
    }


def _conv_has_live_task(conv_id: str, *, user_id: int) -> bool:
    """Return whether this owner has a live task on the conversation lane."""
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime

        return any(
                task.get("convId") == conv_id
                and task.get("status") == "running"
                and not task.get("aborted")
                for task in chat_task_runtime.snapshot_owned(user_id=int(user_id))
            )
    except Exception as error:
        logger.debug(
            "[Dispatch] live-task probe failed conv=%s; assuming busy: %s",
            conv_id[:8],
            error,
        )
        return True


def _workflow_queue_rows(conv_id: str, *, user_id: int) -> list[dict]:
    """Read the owner's pending workflow kickoffs for one conversation."""
    if not conv_id:
        return []
    rows = (
        get_storage_client().query(
            "queue.list",
            {"conv_id": conv_id, "user_id": int(user_id)},
        )
        or []
    )
    return [row for row in rows if row.get("kind") == "workflow_step"]


def _has_queued_kickoff(conv_id: str, *, user_id: int) -> bool:
    """Return whether any workflow kickoff is queued for the conversation."""
    try:
        return bool(_workflow_queue_rows(conv_id, user_id=user_id))
    except Exception as error:
        logger.debug(
            "[Dispatch] kickoff probe failed conv=%s: %s",
            conv_id[:8] if conv_id else "?",
            error,
        )
        return False


def _convs_holding_undrained_kickoffs(
    project_path: str,
    board: dict,
    *,
    user_id: int,
) -> set[str]:
    """Find owned conversations with a live claim or durable project kickoff."""
    conv_ids = {
        task["owner_conv_id"]
        for task in board.get("tasks", [])
        if task.get("status") == "claimed" and task.get("owner_conv_id")
    }
    try:
        from lib.conversations.project_feed import normalize_project_path

        wanted_path = normalize_project_path(project_path)
        conversations = (
            get_storage_client().query(
                "queue.conversations.list_all",
                {"kind": "workflow_step"},
            )
            or []
        )
        for conversation in conversations:
            if int(conversation.get("userId") or 0) != int(user_id):
                continue
            conv_id = str(conversation.get("convId") or "")
            if not conv_id or conv_id in conv_ids:
                continue
            for row in _workflow_queue_rows(conv_id, user_id=user_id):
                config = row.get("config") or {}
                row_path = str(config.get("projectPath") or "")
                if row_path and normalize_project_path(row_path) == wanted_path:
                    conv_ids.add(conv_id)
                    break
    except Exception as error:
        logger.debug(
            "[Dispatch] project kickoff scan failed proj=%.40r: %s", project_path, error
        )
    return conv_ids


def _reconcile_stranded_kickoffs(project_path: str, *, user_id: int) -> int:
    """Drain one durable kickoff for each owned, idle conversation."""
    if not project_path:
        return 0
    drained = 0
    try:
        from lib.conversations.project_board import read_board

        board = read_board(project_path, user_id=user_id)
        conv_ids = _convs_holding_undrained_kickoffs(
            project_path, board, user_id=user_id
        )
        for conv_id in conv_ids:
            if _conv_has_live_task(conv_id, user_id=user_id):
                continue
            if not _has_queued_kickoff(conv_id, user_id=user_id):
                continue
            if _drain_idle_target(conv_id, user_id=user_id):
                drained += 1
    except Exception as error:
        logger.warning(
            "[Dispatch] kickoff reconcile failed proj=%.40r: %s", project_path, error
        )
    return drained


def _epic_already_queued(
    conv_id: str,
    board_task_id: str,
    *,
    user_id: int,
) -> bool:
    """Fail-safe duplicate probe for one epic's durable kickoff."""
    if not conv_id or not board_task_id:
        return False
    try:
        return any(
            (row.get("payload") or {}).get("boardTaskId") == board_task_id
            for row in _workflow_queue_rows(conv_id, user_id=user_id)
        )
    except Exception as error:
        logger.debug("[Dispatch] epic kickoff probe failed; assuming queued: %s", error)
        return True


# ═══════════════════════════════════════════════════════════════════
#  Idle-sibling migration (Pillar #5) — route a stuck epic to an idle peer
#  WITHOUT overwriting authorship. See docs/modules/conversations_project_brain.md.
# ═══════════════════════════════════════════════════════════════════


# "Originator stuck" threshold = one soft-lease window. A healthy idle conv
# drains its kickoff within a 30s sweep; a kickoff still queued after a FULL
# lease TTL means the drain has failed across ~60 sweeps AND the claim would
# have expired + re-dispatched + re-failed — unambiguously stuck, never a
# transient. Reuses the lease clock (owner: no new timer).
def _migration_stuck_ms() -> int:
    return DEFAULT_LEASE_TTL_MS


MIGRATION_STUCK_MS = DEFAULT_LEASE_TTL_MS


def _dispatch_target(epic: dict) -> str:
    """Who should RUN this epic next: the mutable ``dispatch_target`` routing
    override if set, else the immutable ``created_by_conv`` (authorship). This
    is the ONE routing seam every dispatch path consults — provenance is never
    consulted for routing directly."""
    return (epic.get("dispatch_target") or "").strip() or (
        epic.get("created_by_conv") or ""
    ).strip()


def _kickoff_age_ms(
    conv_id: str,
    board_task_id: str,
    now_ms: int,
    *,
    user_id: int,
) -> int | None:
    """Return the age of the oldest durable kickoff for one epic."""
    if not conv_id or not board_task_id:
        return None
    try:
        timestamps = [
            int(row.get("timestamp") or 0)
            for row in _workflow_queue_rows(conv_id, user_id=user_id)
            if (row.get("payload") or {}).get("boardTaskId") == board_task_id
        ]
        return None if not timestamps else max(0, now_ms - min(timestamps))
    except Exception as error:
        logger.debug(
            "[Dispatch] kickoff-age probe failed conv=%s: %s", conv_id[:8], error
        )
        return None


def _paths_waited_but_held(epic: dict, board_tasks: list) -> list:
    """The subset of the epic's ``wait_paths`` currently under a LIVE path
    lease held by a DIFFERENT conversation than the epic's dispatch target.

    The inverse read of the kind='lease' board rows
    (docs/modules/conversations_project_brain.md): the lease claim says "conv X is
    actively touching path Y — hold off"; wait-on-path reads the same rows
    from the epic's side ("hold my epic while Y is held by someone else").
    A lease the epic's OWN target holds is not a hold — that conv is the one
    supposed to run the work.

    Fail-open by construction (design invariant 3): an empty/unparseable
    ``wait_paths``, or a path nobody leases, resolves to [] (not held) so a
    stale entry can never strand an epic. Matching reuses the write-set
    ``_paths_intersect`` semantics (exact or containment either direction) —
    conservative: a false overlap only HOLDS an epic (safe), never migrates
    one. Returns the held subset (empty = not waiting).
    """
    paths = epic.get("wait_paths") or []
    if not isinstance(paths, list) or not paths:
        return []
    target = _dispatch_target(epic)
    live_foreign_leases = [
        t
        for t in (board_tasks or [])
        if isinstance(t, dict)
        and t.get("kind") == "lease"
        and t.get("status") == "claimed"  # effective: lease unexpired
        and (t.get("owner_conv_id") or "") != target
    ]
    if not live_foreign_leases:
        return []
    held = []
    for p in paths:
        ps = str(p).strip()
        if not ps:
            continue
        for lease in live_foreign_leases:
            if _paths_intersect(ps, lease.get("title") or ""):
                held.append(ps)
                break
    return held


def _originator_stuck(
    project_path: str,
    epic: dict,
    board_tasks: list,
    now_ms: int,
    *,
    user_id: int,
) -> bool:
    """True iff the epic's current dispatch target is GENUINELY unable to run
    it — the precise, self-correcting migration trigger (owner-defined).

    ALL must hold (else NOT stuck — never migrate a merely-busy or
    correctly-held epic):
      1. its kickoff has been queued on the target LONGER than one lease TTL
         (``_kickoff_age_ms`` > ``MIGRATION_STUCK_MS``) — a healthy idle conv
         drains within a sweep, so this is unambiguous, no new timer; AND
      2. the target has NO live task (a busy conv is WORKING, not stuck); AND
      3. the epic is NOT on a live block-cooldown AND NOT on a live
         wait-on-path (those mean it is correctly HELD — compose, don't
         override).
    Best-effort; on any error report NOT stuck (never migrate on uncertainty).
    """
    try:
        target = _dispatch_target(epic)
        if not target:
            return False
        # 3 — correctly held (cooldown) is NOT stuck.
        if int(epic.get("blocked_until") or 0) > now_ms:
            return False
        # 3b — correctly held (wait-on-path: a listed path is under a LIVE
        # lease owned by a DIFFERENT conversation) is NOT stuck. Migrating
        # would override the hold the epic declared, and the hold self-expires
        # with the lease (never a deadlock, so never a migration trigger).
        if _paths_waited_but_held(epic, board_tasks):
            return False
        # 2 — a busy target is working, not stuck.
        if _conv_has_live_task(target, user_id=user_id):
            return False
        # 1 — kickoff undrained past a full lease window.
        age_ms = _kickoff_age_ms(target, epic.get("id", ""), now_ms, user_id=user_id)
        if age_ms is None:
            return False  # nothing queued → nothing to migrate
        if age_ms < MIGRATION_STUCK_MS:
            return False
        return True
    except Exception as e:
        logger.debug(
            "[Dispatch] originator-stuck probe failed epic=%s: %s",
            epic.get("id", "?"),
            e,
        )
        return False


def _pick_migration_target(
    project_path: str,
    exclude_conv: str,
    *,
    user_id: int,
) -> str:
    """Pick the most recently active owned sibling with an idle lane."""
    if not project_path:
        return ""
    try:
        from lib.conversations.repository import list_conversations

        rows = list_conversations(
            user_id=int(user_id),
            project_path=project_path,
            order_by="updated_at_desc",
            limit=1000,
            include_messages=False,
            settings_keys=["projectPath"],
        )
        candidates = []
        for row in rows:
            settings = row.get("settings") or {}
            if settings.get("projectPath") != project_path:
                continue
            candidates.append(
                (
                    int(row.get("updated_at") or 0),
                    str(row.get("id") or ""),
                )
            )
        for _, conv_id in sorted(candidates, reverse=True):
            if not conv_id or conv_id == exclude_conv:
                continue
            if _conv_has_live_task(conv_id, user_id=user_id):
                continue
            if _has_queued_kickoff(conv_id, user_id=user_id):
                continue
            return conv_id
    except Exception as error:
        logger.debug(
            "[Dispatch] migration target query failed proj=%.40r: %s",
            project_path,
            error,
        )
    return ""


def _drop_epic_kickoffs(
    conv_id: str,
    board_task_id: str,
    *,
    user_id: int,
) -> int:
    """Remove stale queued kickoffs for one epic from its former target."""
    if not conv_id or not board_task_id:
        return 0
    try:
        client = get_storage_client(write=True)
        removed = 0
        for row in _workflow_queue_rows(conv_id, user_id=user_id):
            if (row.get("payload") or {}).get("boardTaskId") != board_task_id:
                continue
            result = (
                client.command(
                    "queue.remove",
                    {
                        "conv_id": conv_id,
                        "queue_id": row["queueId"],
                        "user_id": int(user_id),
                    },
                    f"queue-drop:{conv_id}:{row['queueId']}",
                )
                or {}
            )
            removed += int(bool(result.get("removed")))
        return removed
    except Exception as error:
        logger.warning(
            "[Dispatch] drop kickoff failed conv=%s epic=%s: %s",
            conv_id[:8],
            board_task_id,
            error,
        )
        return 0


def migrate_epic(
    project_path: str,
    epic: dict,
    new_target: str,
    *,
    user_id: int,
) -> dict:
    """Atomically reopen a stuck epic onto an owned idle sibling."""
    if not project_path or not epic or not new_target:
        return {"ok": False, "error": "missing project/epic/target"}
    task_id = str(epic.get("id") or "")
    origin = str(epic.get("created_by_conv") or "").strip()
    if not task_id:
        return {"ok": False, "error": "epic has no id"}
    try:
        from lib.conversations.project_feed import normalize_project_path

        normalized_path = normalize_project_path(project_path)
        result = get_storage_client(write=True).command(
            "board.mutate",
            {
                "action": "migrate",
                "project_path": normalized_path,
                "task_id": task_id,
                "dispatch_target": new_target,
                "user_id": int(user_id),
            },
            f"board-migrate:{task_id}:{new_target}:{uuid.uuid4().hex[:16]}",
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "error": result.get("error", "migration_conflict"),
            }
        if origin:
            _drop_epic_kickoffs(origin, task_id, user_id=user_id)
    except Exception as error:
        logger.error(
            "[Dispatch] migrate failed proj=%.40r epic=%s: %s",
            project_path,
            task_id,
            error,
            exc_info=True,
        )
        return {"ok": False, "error": str(error)}

    try:
        from lib.conversations.project_feed import emit_project_event

        emit_project_event(
            project_path,
            new_target,
            "note",
            f"Migrated epic to {new_target[:8]} "
            f"(originator {origin[:8] or '?'} was idle-stranded)",
            user_id=int(user_id),
            payload={
                "taskId": task_id,
                "migratedFrom": origin,
                "migratedTo": new_target,
            },
        )
    except Exception as error:
        logger.debug("[Dispatch] migration feed skipped: %s", error)
    audit_log(
        "brain_migrate",
        project_path=project_path,
        task_id=task_id,
        from_conv=origin,
        to_conv=new_target,
        user_id=int(user_id),
    )
    return {"ok": True, "from": origin, "to": new_target}


def _migrate_stranded_epics(project_path: str, *, user_id: int) -> int:
    """Migrate epics whose dispatch target is idle-stranded to a genuinely-idle
    sibling — the bounded (1/sweep) idle-sibling migration pass.

    For each dispatchable epic, if ``_originator_stuck`` (kickoff undrained past
    one lease TTL + target has no live task + NOT held by cooldown/wait) AND an
    idle sibling exists, ``migrate_epic`` re-routes it (sets ``dispatch_target``,
    drops the stale kickoff, reopens the claim). Runs AFTER the reconcile pass
    and BEFORE the dispatch loop, so a just-migrated epic is picked up in the
    SAME sweep and routed to its new target. Bounded to ONE migration per sweep
    (the next sweep handles any further strands) so a mass-stranded board can't
    thrash. Best-effort; never raises into the sweep.

    Returns the number of epics migrated (0 or 1).
    """
    if not project_path:
        return 0
    try:
        import time as _time

        now_ms = int(_time.time() * 1000)
        from lib.conversations.project_board import read_board

        board = read_board(project_path, user_id=user_id)
        tasks = board["tasks"]
        for epic in tasks:
            if epic.get("kind") == "lease" or epic.get("status") != "open":
                continue
            if not _originator_stuck(
                project_path, epic, tasks, now_ms, user_id=user_id
            ):
                continue
            target = _pick_migration_target(
                project_path,
                _dispatch_target(epic),
                user_id=user_id,
            )
            if not target:
                continue  # no idle sibling → leave it with the originator
            res = migrate_epic(project_path, epic, target, user_id=user_id)
            if res.get("ok"):
                return 1  # bounded: one migration per sweep
    except Exception as e:
        logger.warning(
            "[Dispatch] migrate-stranded pass failed proj=%.40r: %s", project_path, e
        )
    return 0


def sweep_dispatch(
    project_path: str,
    *,
    user_id: int,
    max_per_sweep: int = 3,
) -> int:
    """Reconcile, migrate, and dispatch a bounded project batch."""
    if not project_path:
        return 0
    dispatched = 0
    _reconcile_stranded_kickoffs(project_path, user_id=user_id)
    _migrate_stranded_epics(project_path, user_id=user_id)
    try:
        for epic in select_dispatchable(project_path, user_id=user_id):
            if dispatched >= max(1, max_per_sweep):
                break
            target = _dispatch_target(epic)
            if not target:
                continue
            if _conv_has_live_task(target, user_id=user_id):
                continue
            if _epic_already_queued(target, str(epic.get("id") or ""), user_id=user_id):
                continue
            result = dispatch_epic(project_path, epic, target, user_id=user_id)
            if not result.get("ok"):
                continue
            dispatched += 1
            _drain_idle_target(target, user_id=user_id)
    except Exception as error:
        logger.warning("[Dispatch] sweep failed proj=%.40r: %s", project_path, error)
    return dispatched


def sweep_all_active_projects(
    *,
    user_id: int,
    max_projects: int = 20,
    max_per_sweep: int = 3,
) -> int:
    """Run the heartbeat over a bounded set of recent projects for one owner."""
    try:
        from lib.project_mod import get_recent_projects

        projects = get_recent_projects(user_id=user_id) or []
    except Exception as error:
        logger.debug("[Dispatch] project enumeration failed: %s", error)
        return 0

    total = 0
    selected = projects[: max(1, max_projects)]
    for project in selected:
        path = str(project.get("path") if isinstance(project, dict) else "")
        if not path:
            continue
        total += sweep_dispatch(
            path,
            user_id=user_id,
            max_per_sweep=max_per_sweep,
        )
    if total:
        logger.info(
            "[Dispatch] heartbeat dispatched %d epic(s) across %d project(s)",
            total,
            len(selected),
        )
    return total


def on_epic_completed(
    project_path: str,
    completed_conv_id: str = "",
    *,
    user_id: int,
) -> int:
    """Immediately enqueue epics whose dependencies just became satisfied."""
    if not project_path:
        return 0
    dispatched = 0
    try:
        for epic in select_dispatchable(project_path, user_id=user_id):
            target = _dispatch_target(epic) or completed_conv_id
            if not target:
                continue
            if _epic_already_queued(target, str(epic.get("id") or ""), user_id=user_id):
                continue
            result = dispatch_epic(
                project_path,
                {**epic, "_via": "dependency_done"},
                target,
                user_id=user_id,
            )
            if result.get("ok"):
                dispatched += 1
                _drain_idle_target(target, user_id=user_id)
    except Exception as error:
        logger.warning(
            "[Dispatch] completion trigger failed proj=%.40r: %s", project_path, error
        )
    return dispatched


def on_epic_answered(
    project_path: str,
    task_id: str,
    *,
    user_id: int,
) -> int:
    """Immediately resume one answered human-gated epic when safe."""
    if not project_path or not task_id:
        return 0
    try:
        from lib.conversations.project_board import read_board

        board = read_board(project_path, user_id=user_id)
        epic = next(
            (task for task in board["tasks"] if task["id"] == task_id),
            None,
        )
        if not epic or epic.get("status") != "open":
            return 0
        if not str(epic.get("human_answer") or "").strip():
            return 0
        done_ids = {
            task["id"] for task in board["tasks"] if task.get("status") == "done"
        }
        if any(
            dependency not in done_ids for dependency in epic.get("depends_on") or []
        ):
            return 0
        target = _dispatch_target(epic)
        if not target:
            return 0
        if _conv_has_live_task(target, user_id=user_id):
            return 0
        if _epic_already_queued(target, task_id, user_id=user_id):
            return 0
        result = dispatch_epic(
            project_path,
            {**epic, "_via": "answered"},
            target,
            user_id=user_id,
        )
        if not result.get("ok"):
            return 0
        _drain_idle_target(target, user_id=user_id)
        return 1
    except Exception as error:
        logger.warning(
            "[Dispatch] answer trigger failed proj=%.40r task=%s: %s",
            project_path,
            task_id,
            error,
        )
        return 0


def on_epic_posted(
    project_path: str,
    task_id: str,
    *,
    user_id: int,
) -> int:
    """Start a newly posted epic immediately when its owned target is idle."""
    if not project_path or not task_id:
        return 0
    try:
        from lib.conversations.project_board import read_board

        board = read_board(project_path, user_id=user_id)
        epic = next(
            (task for task in board["tasks"] if task["id"] == task_id),
            None,
        )
        if not epic or epic.get("status") != "open":
            return 0
        done_ids = {
            task["id"] for task in board["tasks"] if task.get("status") == "done"
        }
        if any(
            dependency not in done_ids for dependency in epic.get("depends_on") or []
        ):
            return 0
        target = _dispatch_target(epic)
        if not target:
            return 0
        if _conv_has_live_task(target, user_id=user_id):
            return 0
        if _epic_already_queued(target, task_id, user_id=user_id):
            return 0
        if not get_storage_client().query(
            "conversation.get",
            {"conv_id": target, "user_id": int(user_id)},
        ):
            return 0
        result = dispatch_epic(
            project_path,
            {**epic, "_via": "posted"},
            target,
            user_id=user_id,
        )
        if not result.get("ok"):
            return 0
        _drain_idle_target(target, user_id=user_id)
        return 1
    except Exception as error:
        logger.warning(
            "[Dispatch] post trigger failed proj=%.40r task=%s: %s",
            project_path,
            task_id,
            error,
        )
        return 0


def on_conv_idle(
    project_path: str,
    conv_id: str,
    *,
    user_id: int,
) -> int:
    """Start one epic routed to a conversation that just became idle."""
    if not project_path or not conv_id:
        return 0
    try:
        # Human-authored queued turns always outrank autonomous Board work.
        # Without this gate, settlement could race the queue drain and launch
        # an epic into the lane just before the user's waiting message.
        from lib.message_queue import get_queue_depth

        if get_queue_depth(conv_id, user_id=user_id) > 0:
            return 0
        if _conv_has_live_task(conv_id, user_id=user_id):
            return 0
        for epic in select_dispatchable(project_path, user_id=user_id):
            if _dispatch_target(epic) != conv_id:
                continue
            result = dispatch_epic(
                project_path,
                {**epic, "_via": "conv_idle"},
                conv_id,
                user_id=user_id,
            )
            if not result.get("ok"):
                return 0
            _drain_idle_target(conv_id, user_id=user_id)
            return 1
    except Exception as error:
        logger.warning(
            "[Dispatch] idle trigger failed proj=%.40r conv=%s: %s",
            project_path,
            conv_id[:8],
            error,
        )
    return 0


__all__ = [
    "select_dispatchable",
    "dispatch_epic",
    "on_epic_completed",
    "on_epic_answered",
    "on_epic_posted",
    "on_conv_idle",
    "sweep_dispatch",
    "sweep_all_active_projects",
    "BRAIN_DISPATCH_MARKER",
]

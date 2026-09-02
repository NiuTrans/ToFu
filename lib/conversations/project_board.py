"""lib.conversations.project_board — the coordination BOARD (Pillar #3).

This is the piece that turns PERCEPTION (the Activity Feed) and shared INTENT
(the Charter) into actual AUTO-COORDINATION: a per-project board of coarse,
human-meaningful epics that conversations POST, CLAIM, and COMPLETE — so two
conversations of the same project stop colliding / duplicating work.

Locked design (owner, 2026-06-30):

  • **Soft, TTL-expiring lease — advisory, never a hard lock.** ``claim_task``
    sets ``owner_conv_id`` + ``lease_expires_at = now + TTL``. The lease is
    NOT enforced by a write-lock; it's a HINT injected into every sibling's
    prompt ("X is being worked by conversation …, avoid duplicating"). A
    crashed/abandoned conversation can NEVER deadlock the board because the
    lease expiry is evaluated AT READ TIME — an expired claim reads as
    ``open`` with no background reaper, no global cleaner thread.
  • **Per ``project_path``, never a process-global.** Every call addresses its
    project explicitly (the read/write-badge thrash guard).
  • **Coarse granularity.** Epics only — fine agent sub-steps belong to the
    Activity Feed, not the board.
  • **Feed-coupled.** post→(no feed; quiet), claim→``claimed``,
    complete→``completed``, block→``blocked`` (the last dead kind finally
    gets a producer here).

``status`` is the STORED column (open/claimed/done); ``effective_status`` is
what a reader sees after the at-read-time lease check (a stored ``claimed``
whose lease has expired is reported ``open``).
"""

from __future__ import annotations

import hashlib
import json

from lib.conversations.project_board_policy import (
    BLOCK_COOLDOWN_BASE_MS,
    BLOCK_COOLDOWN_MAX_MS,
    DEFAULT_LEASE_TTL_MS,
    SIBLING_BLOCK_COOLDOWN_MS,
    SIBLING_BLOCK_TAG as _SIBLING_TAG,
    block_cooldown_ms as _block_cooldown_ms,
    effective_board_status as _effective_status,
)
from lib.log import audit_log, get_logger
from lib.ids import short_id
from lib.storage import get_storage_client
from lib.timeutil import now_ms

logger = get_logger(__name__)


def _invocation_cmd_id(operation: str, *identity_parts: object) -> str:
    """Mint a bounded receipt ID for one logical mutation invocation."""
    identity = "\x1f".join(str(part) for part in identity_parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{operation}:{digest}:{short_id(n=16)}"


# Structured human question on a [human-gated] block (Pillar #3). Capped so a
# pathological payload can't bloat the row; mirrors ask_human's option shape.
_QUESTION_MAX_CHARS = 600
_OPTION_LABEL_MAX = 120
_OPTION_DESC_MAX = 300
_OPTION_MAX = 6

# Prose patterns that ASSERT a structured question card already exists. A
# ``block_reason`` matching one of these while ``question`` is empty is the
# measured 2026-07-31 defect (): two epics sat parked
# ~19 h behind a "4-option question card" that was never created, because the
# author wrote the question into the free-text reason instead of passing
# ``question=``. That state is invisible BOTH ways — ``project_attention``
# builds its "Needs you" card from the ``block_question`` column, and
# ``select_dispatchable`` only honours that same column — so the epic neither
# reached the human nor genuinely stopped.
#
# Matched against a CASEFOLDED reason. Deliberately narrow: each phrase names
# an *interactive control the owner is expected to operate*, which is exactly
# the claim only ``question=`` can make true. A plain "[human-gated] needs infra
# sign-off" asserts no such control and stays legal without a question.
#
# NOT included, on measured evidence: a bare "awaiting owner". The over-firing
# complement in tests caught it firing on the legitimate
# ``[sibling] path=lib/x.py awaiting the owner of that file`` — there "owner"
# means the owner of a FILE, not a human decision-maker. A phrase that also
# describes ordinary sibling coordination cannot carry this refusal.
_QUESTION_CLAIM_PHRASES = (
    "question card",
    "one-click",
    "one click",
    "awaiting your answer",
    "answer the question",
)


def _claims_a_question_card(reason: str) -> bool:
    """True iff ``reason`` asserts that a structured human question is pending.

    Used to refuse a block whose prose promises a card the caller never
    registered. Pure + side-effect-free so the refusal can be evaluated BEFORE
    any mutation.
    """
    low = (reason or "").casefold()
    # A [sibling] block auto-resolves on a peer's commit — it has no human
    # question by construction, so it can never be making this claim.
    if _SIBLING_TAG in low:
        return False
    return any(p in low for p in _QUESTION_CLAIM_PHRASES)


def _clean_block_question(question: str, options) -> str:
    """Sanitize the optional structured human question → canonical JSON (''
    when no question was given). Shape::

        {"q": str, "options": [{"label": str, "description"?: str}]}

    An empty options list means the human answers with free text. Never
    raises; malformed entries are dropped, not repaired.
    """
    q = (question or "").strip()[:_QUESTION_MAX_CHARS]
    if not q:
        return ""
    clean_opts = []
    raw_opts = options if isinstance(options, (list, tuple)) else []
    for o in raw_opts:
        if len(clean_opts) >= _OPTION_MAX:
            break  # cap VALID options — malformed entries never consume a slot
        if isinstance(o, str):
            label, desc = o.strip()[:_OPTION_LABEL_MAX], ""
        elif isinstance(o, dict):
            label = str(o.get("label") or "").strip()[:_OPTION_LABEL_MAX]
            desc = str(o.get("description") or "").strip()[:_OPTION_DESC_MAX]
        else:
            continue
        if not label:
            continue
        item = {"label": label}
        if desc:
            item["description"] = desc
        clean_opts.append(item)
    return json.dumps({"q": q, "options": clean_opts}, ensure_ascii=False)


_TITLE_MAX_CHARS = 2000  # epics carry multi-sentence design descriptions; a
# tight cap silently clipped titles mid-word (both in
# the board panel and the injected prompt block)
# Admission guard against runaway posting. This caps only the ACTIVE epics
# (stored status != 'done') — the working set a reader actually has to reason
# about. Completed epics are history: they must NEVER count toward admission
# (otherwise a long-lived project accretes 200 finished epics and the board is
# PERMANENTLY "full", unable to accept a single new epic — the reported bug).
_MAX_ACTIVE_TASKS = 200
# Completed epics are retained for the "Recently done" lane, but capped so the
# table can't grow without bound over a project's life. When a post pushes the
# done-row count past this, the OLDEST done rows are pruned (best-effort, in the
# same connection). The panel/prompt only ever surface the last ~8 done epics.
_MAX_DONE_RETAINED = 100
_now_ms = now_ms


def claims_by_conv(board_tasks: list) -> dict:
    """Map ``owner_conv_id`` → claimed-epic title, for epics whose EFFECTIVE
    status is ``claimed`` (i.e. a live, unexpired lease).

    This is the SINGLE source of the "which conversation is advancing which
    epic" join. ``read_board`` already reclaimed expired leases to ``open``, so
    every entry here is a live claim — never a deadlocked one. Both
    ``build_brain_summary`` (collab bar) and ``build_peer_status`` (the peer
    introspection tool) consume it so the two views can never drift. Pure +
    side-effect-free; safe on any task list.
    """
    out = {}
    for t in board_tasks or []:
        if not isinstance(t, dict):
            continue
        if t.get("status") == "claimed" and t.get("owner_conv_id"):
            out[t["owner_conv_id"]] = t.get("title", "")
    return out


def read_board(project_path: str, *, user_id: int) -> dict:
    """Read one normalized project board; failures degrade to an empty board."""
    empty = {"tasks": [], "open": 0, "claimed": 0, "done": 0, "blocked": 0}
    if not project_path:
        return empty
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        return get_storage_client().query(
            "board.list",
            {"project_path": normalized_path, "user_id": int(user_id)},
        )
    except Exception as error:
        logger.warning("[Board] read failed proj=%.40r: %s", normalized_path, error)
        return empty


def _maybe_create_isolation(
    project_path: str,
    task_id: str,
    title: str,
    conv_id: str,
    isolated: bool,
    write_set: list | None,
    result: dict,
    *,
    user_id: int,
) -> None:
    """Create the integration writer workspace for an ISOLATED epic — BEFORE
    the post-time dispatch trigger fires, so the kickoff's existence-peek
    finds it (workspace existence IS the isolation flag; no board schema
    change). A requested-isolation failure is returned to ``post_task``, which
    blocks the epic and never offers it to shared-tree dispatch."""
    if not isolated:
        return
    try:
        from lib.integration_control import create_workspace

        created = create_workspace(
            project_path,
            task_id,
            title,
            user_id=int(user_id),
            origin={
                "epicId": task_id,
                "convId": conv_id or "",
                "source": "board",
                "writeSet": [str(item) for item in (write_set or [])],
            },
        )
        result["isolated"] = True
        result["workspacePath"] = created.get("workspacePath", "")
    except Exception as e:
        logger.warning(
            "[Board] isolation workspace failed proj=%.40r epic=%s: %s",
            project_path,
            task_id,
            e,
        )
        result["isolated"] = False
        result["isolationError"] = str(e)


def post_task(
    project_path: str,
    conv_id: str,
    title: str,
    *,
    user_id: int,
    depends_on: list | None = None,
    write_set: list | None = None,
    isolated: bool = False,
) -> dict:
    """Post a new open epic and immediately offer it to the dispatcher."""
    title = (title or "").strip()[:_TITLE_MAX_CHARS]
    if not project_path:
        return {"ok": False, "error": "no project"}
    if not title:
        return {"ok": False, "error": "empty title"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.post",
            {
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "title": title,
                "depends_on": depends_on or [],
                "write_set": write_set or [],
                "max_active": _MAX_ACTIVE_TASKS,
                "max_done_retained": _MAX_DONE_RETAINED,
            },
            _invocation_cmd_id("board.post", normalized_path, conv_id, title),
        )
    except Exception as error:
        logger.error(
            "[Board] post failed proj=%.40r: %s", normalized_path, error, exc_info=True
        )
        return {"ok": False, "error": str(error)}
    if not result.get("ok"):
        return result

    task_id = str(result.get("id") or "")
    audit_log(
        "board_post", project_path=normalized_path, task_id=task_id, conv_id=conv_id
    )
    _maybe_create_isolation(
        normalized_path, task_id, title, conv_id, isolated, write_set, result,
        user_id=int(user_id),
    )
    if isolated and not result.get("isolated"):
        isolation_error = str(result.get("isolationError") or "unknown error")
        blocked = block_task(
            normalized_path,
            conv_id,
            task_id,
            f"[integration] isolated workspace unavailable: {isolation_error}",
            user_id=int(user_id),
        )
        result["isolationBlocked"] = bool(blocked.get("ok"))
        if not blocked.get("ok"):
            result["isolationBlockError"] = str(
                blocked.get("error") or "could not persist the isolation block")
            logger.error(
                "[Board] isolation fail-closed block failed epic=%s: %s",
                task_id,
                result["isolationBlockError"],
            )
        # Never offer a requested-isolated epic to the shared-tree dispatcher.
        # A human may inspect the block and post a fresh isolated epic after
        # the Git/storage issue is fixed.
        return result
    try:
        from lib.conversations.project_dispatch import on_epic_posted

        on_epic_posted(normalized_path, task_id, user_id=int(user_id))
    except Exception as error:
        logger.debug("[Board] post dispatch skipped: %s", error)
    return result


def claim_task(
    project_path: str,
    conv_id: str,
    task_id: str,
    *,
    user_id: int,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    dispatched: bool = False,
) -> dict:
    """Acquire or refresh an advisory, expiring epic lease."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.claim",
            {
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
                "ttl_ms": ttl_ms,
                "dispatched": dispatched,
            },
            _invocation_cmd_id("board.claim", normalized_path, task_id, conv_id),
        )
    except Exception as error:
        logger.warning(
            "[Board] claim failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if result.get("ok") and result.get("transitioned", True):
        _emit(
            "claimed",
            normalized_path,
            conv_id,
            f"Claimed: {result.get('title') or ''}",
            user_id=int(user_id),
            payload={"taskId": task_id},
        )
        audit_log(
            "board_claim",
            project_path=normalized_path,
            task_id=task_id,
            conv_id=conv_id,
        )
    return result


def complete_task(
    project_path: str, conv_id: str, task_id: str, *, user_id: int
) -> dict:
    """Mark an epic done and offer newly unblocked work to the dispatcher."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        from lib.integration_control import board_completion_gate

        integration = board_completion_gate(
            normalized_path, task_id, user_id=int(user_id))
    except Exception as error:
        logger.warning(
            "[Board] integration completion gate failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": "integration_status_unavailable"}
    if not integration.get("ok"):
        return {
            "ok": False,
            "error": "integration_not_merged",
            "integrationState": integration.get("state") or "unknown",
        }
    try:
        result = get_storage_client(write=True).command(
            "board.complete",
            {
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
            },
            _invocation_cmd_id("board.complete", normalized_path, task_id, conv_id),
        )
    except Exception as error:
        logger.warning(
            "[Board] complete failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if result.get("ok") and result.get("transitioned", True):
        _emit(
            "completed",
            normalized_path,
            conv_id,
            f"Completed: {result.get('title') or ''}",
            user_id=int(user_id),
            payload={"taskId": task_id},
        )
        audit_log(
            "board_complete",
            project_path=normalized_path,
            task_id=task_id,
            conv_id=conv_id,
        )
        try:
            from lib.conversations.project_dispatch import on_epic_completed

            on_epic_completed(
                normalized_path,
                completed_conv_id=conv_id,
                user_id=int(user_id),
            )
        except Exception as error:
            logger.debug("[Board] completion dispatch skipped: %s", error)
    return result


def block_task(
    project_path: str,
    conv_id: str,
    task_id: str,
    reason: str,
    *,
    user_id: int,
    question: str = "",
    options=None,
) -> dict:
    """Record an external gate, with an optional structured human question."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    reason = (reason or "").strip()
    if _claims_a_question_card(reason) and not (question or "").strip():
        return {"ok": False, "error": "question_required"}
    if len(reason) > _TITLE_MAX_CHARS:
        logger.warning(
            "[Board] block reason truncated proj=%.40r task=%s: %d -> %d chars",
            project_path,
            task_id,
            len(reason),
            _TITLE_MAX_CHARS,
        )
        reason = reason[:_TITLE_MAX_CHARS]

    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    question_json = _clean_block_question(question, options)
    try:
        result = get_storage_client(write=True).command(
            "board.mutate",
            {
                "action": "block",
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
                "reason": reason,
                "question_json": question_json,
            },
            _invocation_cmd_id("board.block", normalized_path, task_id, reason),
        )
    except Exception as error:
        logger.warning(
            "[Board] block failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if not result.get("ok"):
        return result

    block_count = int(result.get("block_count") or 0)
    blocked_until = int(result.get("blocked_until") or 0)
    cooldown_minutes = max(0, blocked_until - _now_ms()) // 60_000
    _emit(
        "blocked",
        normalized_path,
        conv_id,
        f"Blocked: {result.get('title') or ''}"
        + (f" — {reason}" if reason else "")
        + f" (retry in ~{cooldown_minutes}m, block #{block_count})",
        user_id=int(user_id),
        payload={
            "taskId": task_id,
            "reason": reason,
            "blockedUntil": blocked_until,
            "blockCount": block_count,
            "question": json.loads(question_json) if question_json else None,
        },
    )
    audit_log(
        "board_block",
        project_path=normalized_path,
        task_id=task_id,
        conv_id=conv_id,
        block_count=block_count,
    )
    return result


def reopen_task(project_path: str, conv_id: str, task_id: str, *, user_id: int) -> dict:
    """Clear a completed, claimed, or blocked epic back to open."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.reopen",
            {
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
            },
            _invocation_cmd_id("board.reopen", normalized_path, task_id, conv_id),
        )
    except Exception as error:
        logger.warning(
            "[Board] reopen failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if not (result.get("ok") and result.get("transitioned", True)):
        return result

    previous_status = result.get("from") or "open"
    previous_owner = result.get("prev_owner") or ""
    summary = f"Reopened: {result.get('title') or ''}"
    if previous_status == "claimed" and previous_owner:
        summary += f" (was claimed by {previous_owner})"
    _emit(
        "note",
        normalized_path,
        conv_id,
        summary,
        user_id=int(user_id),
        payload={
            "taskId": task_id,
            "reopened": True,
            "from": previous_status,
            "prevOwner": previous_owner,
        },
    )
    audit_log(
        "board_reopen",
        project_path=normalized_path,
        task_id=task_id,
        conv_id=conv_id,
        from_status=previous_status,
        prev_owner=previous_owner,
    )
    try:
        from lib.conversations.project_dispatch import on_epic_posted

        on_epic_posted(normalized_path, task_id, user_id=int(user_id))
    except Exception as error:
        logger.debug("[Board] reopen dispatch skipped: %s", error)
    return result


def delete_task(project_path: str, conv_id: str, task_id: str, *, user_id: int) -> dict:
    """Delete an epic unless an active epic still depends on it."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.mutate",
            {
                "action": "delete",
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
            },
            _invocation_cmd_id("board.delete", normalized_path, task_id, conv_id),
        )
    except Exception as error:
        logger.warning(
            "[Board] delete failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if not result.get("ok"):
        return result

    previous_status = result.get("prev_status") or "open"
    previous_owner = result.get("prev_owner") or ""
    summary = f"Deleted: {result.get('title') or ''} (was {previous_status}"
    if previous_owner:
        summary += f", claimed by {previous_owner}"
    summary += ")"
    _emit(
        "note",
        normalized_path,
        conv_id,
        summary,
        user_id=int(user_id),
        payload={
            "taskId": task_id,
            "deleted": True,
            "from": previous_status,
            "prevOwner": previous_owner,
        },
    )
    audit_log(
        "board_delete",
        project_path=normalized_path,
        task_id=task_id,
        conv_id=conv_id,
        from_status=previous_status,
        prev_owner=previous_owner,
    )
    return result


def answer_task(
    project_path: str,
    conv_id: str,
    task_id: str,
    answer: str,
    *,
    user_id: int,
) -> dict:
    """Resolve a pending human question and immediately resume the epic."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    answer = (answer or "").strip()[:_TITLE_MAX_CHARS]
    if not answer:
        return {"ok": False, "error": "missing answer"}
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.mutate",
            {
                "action": "answer",
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
                "answer": answer,
            },
            _invocation_cmd_id("board.answer", normalized_path, task_id, answer),
        )
    except Exception as error:
        logger.warning(
            "[Board] answer failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if not result.get("ok"):
        return result

    _emit(
        "answered",
        normalized_path,
        conv_id,
        f"Answered: {result.get('title') or ''} — {answer}",
        user_id=int(user_id),
        payload={
            "taskId": task_id,
            "question": result.get("question_text") or "",
            "answer": answer,
        },
    )
    audit_log(
        "board_answer",
        project_path=normalized_path,
        task_id=task_id,
        conv_id=conv_id,
        answer_len=len(answer),
    )
    try:
        from lib.conversations.project_dispatch import on_epic_answered

        on_epic_answered(normalized_path, task_id, user_id=int(user_id))
    except Exception as error:
        logger.debug("[Board] answer dispatch skipped: %s", error)
    return result


def set_write_set(
    project_path: str,
    conv_id: str,
    task_id: str,
    write_set: list,
    *,
    user_id: int,
) -> dict:
    """Replace an epic's declared write footprint without changing its state."""
    if not project_path or not task_id:
        return {"ok": False, "error": "missing project/task"}
    clean = []
    for path in write_set or []:
        value = str(path or "").strip()[:_TITLE_MAX_CHARS]
        if value and value not in clean:
            clean.append(value)
    from lib.conversations.project_feed import normalize_project_path

    normalized_path = normalize_project_path(project_path)
    try:
        result = get_storage_client(write=True).command(
            "board.write_set",
            {
                "project_path": normalized_path,
                "user_id": int(user_id),
                "conv_id": conv_id,
                "task_id": task_id,
                "write_set": clean,
            },
            _invocation_cmd_id("board.write-set", normalized_path, task_id, conv_id),
        )
    except Exception as error:
        logger.warning(
            "[Board] write-set failed proj=%.40r task=%s: %s",
            normalized_path,
            task_id,
            error,
        )
        return {"ok": False, "error": str(error)}
    if result.get("ok"):
        result.setdefault("write_set", clean)
        try:
            from lib.integration_control import update_workspace_write_set

            integration = update_workspace_write_set(
                normalized_path,
                task_id,
                clean,
                user_id=int(user_id),
            )
            result["integrationWriteSetUpdated"] = bool(
                integration.get("updated"))
        except Exception as error:
            logger.warning(
                "[Board] integration write-set sync failed proj=%.40r "
                "task=%s: %s",
                normalized_path,
                task_id,
                error,
            )
            return {
                "ok": False,
                "error": "integration_write_set_sync_failed",
                "detail": str(error),
                "write_set": clean,
            }
        audit_log(
            "board_write_set",
            project_path=normalized_path,
            task_id=task_id,
            conv_id=conv_id,
            write_count=len(clean),
        )
    return result


def _emit(
    kind: str,
    project_path: str,
    conv_id: str,
    summary: str,
    *,
    user_id: int,
    payload: dict | None = None,
) -> None:
    """Best-effort feed emission — never raises into the board caller."""
    try:
        from lib.conversations.project_feed import emit_project_event

        emit_project_event(
            project_path,
            conv_id or "",
            kind,
            summary,
            user_id=int(user_id),
            payload=payload,
        )
    except Exception as e:
        logger.debug("[Board] feed emit (%s) skipped: %s", kind, e)


# How much of an epic title the PROMPT INJECTION carries. Epics routinely hold
# a whole spec in `title` (stored uncapped up to _TITLE_MAX_CHARS), but the
# injection only has to answer "who is doing what, so I don't collide" — the
# spec is needed when PICKING UP an epic, which is a deliberate tool round.
_INJECT_TITLE_MAX_CHARS = 200


def _abridge_title(title: str) -> str:
    """First line of ``title``, bounded — for the per-turn injection only.

    Returns the title unchanged when it is already short, so the ellipsis stays
    a MEANINGFUL signal ("there is more behind this") rather than decoration on
    every row. The full text always remains reachable via ``project_board_read``
    — an abridgement the model cannot detect or undo would be the worse defect.
    """
    head = (title or "").strip().split("\n", 1)[0].strip()
    multiline = "\n" in (title or "").strip()
    if len(head) <= _INJECT_TITLE_MAX_CHARS and not multiline:
        return head
    return head[:_INJECT_TITLE_MAX_CHARS].rstrip() + " …"


def render_board_injection_block(
    project_path: str,
    current_conv_id: str = "",
    *,
    user_id: int,
    board_snapshot: dict | None = None,
) -> str:
    """The board as a per-turn PROMPT INJECTION — a coordination summary.

    Same lanes and the same avoid-duplication hint as the full render, but each
    epic is reduced to id + headline + status + owner. Measured on the live
    board this cut the per-turn cost from 16,764 chars to a small fraction,
    with no loss of coordination signal.

    Use ``render_board_block`` (the full render) for the ``project_board_read``
    TOOL and for any human-facing surface — those are pull-based and must show
    the complete epic text.
    """
    return _render_board(
        project_path,
        current_conv_id,
        user_id=user_id,
        abridged=True,
        board_snapshot=board_snapshot,
    )


def render_board_block(
    project_path: str, current_conv_id: str = "", *, user_id: int
) -> str:
    """Render the board IN FULL — the pull-based detail channel.

    Backs the ``project_board_read`` agent tool and the panel/influence reads:
    every epic's complete stored text, never abridged. The per-turn prompt
    injection deliberately uses ``render_board_injection_block`` instead.
    """
    return _render_board(project_path, current_conv_id, user_id=user_id, abridged=False)


def _render_board(
    project_path: str,
    current_conv_id: str = "",
    *,
    user_id: int,
    abridged: bool = False,
    board_snapshot: dict | None = None,
) -> str:
    """Shared board renderer — ONE lane-partitioning implementation.

    ``abridged`` selects the per-turn injection shape (short titles + a pointer
    to the detail tool); everything else — lane partitioning, lease expiry,
    the "(you)" stamp, the avoid-duplication hint — is identical, so the two
    consumers can never disagree about WHAT is on the board, only about how
    much of each epic they spell out.
    """
    _t = _abridge_title if abridged else (lambda s: s)
    board = (
        board_snapshot
        if board_snapshot is not None
        else read_board(project_path, user_id=user_id)
    )
    tasks = board.get("tasks") or []
    if not tasks:
        return ""
    # Leases (kind='lease') are path RESERVATIONS, not epics — partition them
    # out of every epic section and render them in their own "Held" block. Only
    # a LIVE lease (effective status still 'claimed') is a held reservation; an
    # expired one reads 'open' and is simply dropped (it holds nothing).
    epics = [t for t in tasks if t.get("kind") != "lease"]
    now = _now_ms()
    # An epic whose block cooldown is still LIVE (blocked_until > now) is
    # partitioned into its own "Blocked" lane — NOT the Open lane (where it
    # would read as "claim me" and get re-dispatched). Once the cooldown lapses
    # it falls back to Open automatically (at-read-time, no reaper).
    # An epic blocked WITH a structured human question waits for the ANSWER,
    # not for time — its own lane REGARDLESS of cooldown state (auto-retry is
    # paused until answered; the answer re-dispatches it immediately).
    pending_q = [
        t
        for t in epics
        if t["status"] == "open"
        and t.get("block_question")
        and not (t.get("human_answer") or "").strip()
    ]
    pending_ids = {t["id"] for t in pending_q}
    blocked_t = [
        t
        for t in epics
        if t["status"] == "open"
        and int(t.get("blocked_until") or 0) > now
        and t["id"] not in pending_ids
    ]
    blocked_ids = {t["id"] for t in blocked_t}
    open_t = [
        t
        for t in epics
        if t["status"] == "open"
        and t["id"] not in blocked_ids
        and t["id"] not in pending_ids
    ]
    claimed_t = [t for t in epics if t["status"] == "claimed"]
    done_t = [t for t in epics if t["status"] == "done"]
    if not (open_t or claimed_t or done_t or blocked_t or pending_q):
        return ""
    lines = [
        "[PROJECT BOARD] — shared coordination board for this project. "
        "Before starting work, CHECK it: claim an open epic so siblings "
        "know you own it, and do NOT duplicate an epic another "
        "conversation is already advancing."
        + (
            " Epics are shown ABRIDGED (headline only, marked …) — call "
            "project_board_read for an epic's full text before you work it."
            if abridged
            else ""
        )
    ]
    if claimed_t:
        lines.append("")
        lines.append("In progress (claimed by a conversation — AVOID DUPLICATING):")
        for t in claimed_t:
            owner = t["owner_conv_id"] or "another conversation"
            mine = " (you)" if current_conv_id and owner == current_conv_id else ""
            hint = (
                ""
                if mine
                else " — another conversation is advancing this; "
                "pick a different epic or coordinate, do not redo it"
            )
            lines.append(
                f"  • [{t['id']}] {_t(t['title'])} — claimed by {owner}{mine}{hint}"
            )
    if open_t:
        lines.append("")
        lines.append(
            "Open (unclaimed — claim one with project_board_claim before working it):"
        )
        for t in open_t:
            dep = (
                f" (depends on {', '.join(t['depends_on'])})" if t["depends_on"] else ""
            )
            lines.append(f"  • [{t['id']}] {_t(t['title'])}{dep}")
    if blocked_t:
        lines.append("")
        lines.append(
            "Waiting on an external gate (auto-retries on its own after a "
            "cooldown — no action needed):"
        )
        for t in blocked_t:
            mins = max(0, (int(t.get("blocked_until") or 0) - now) // 60_000)
            reason = (t.get("block_reason") or "").strip()
            why = f" — {reason}" if reason else ""
            cnt = int(t.get("block_count") or 0)
            lines.append(
                f"  • [{t['id']}] {_t(t['title'])}{why} "
                f"(retry in ~{mins}m, blocked {cnt}×)"
            )
    if pending_q:
        lines.append("")
        lines.append(
            "Waiting for the human's answer (auto-retry paused — the "
            "board panel shows a question; answering re-dispatches "
            "the epic immediately with the answer in context):"
        )
        for t in pending_q:
            q = ((t.get("block_question") or {}).get("q") or "").strip()
            qq = f" — Q: {q}" if q else ""
            lines.append(f"  • [{t['id']}] {_t(t['title'])}{qq}")
    if done_t:
        lines.append("")
        lines.append("Recently done:")
        for t in done_t[-8:]:
            lines.append(f"  • {_t(t['title'])}")
    return "\n".join(lines)


def execute_board_tool(
    fn_name: str,
    fn_args: dict,
    *,
    current_conv_id: str = "",
    project_path: str = "",
    user_id: int,
) -> str:
    """Execute a board agent tool → human-readable string."""
    try:
        if not project_path:
            return (
                "Error: the project board is only available in project mode "
                "(open a project first)."
            )
        if fn_name == "project_board_read":
            block = render_board_block(project_path, current_conv_id, user_id=user_id)
            return block or (
                "The project board is empty. If you discover a "
                "project-level epic, post it with project_board_post "
                "so sibling conversations can coordinate."
            )
        if fn_name == "project_board_post":
            res = post_task(
                project_path,
                current_conv_id,
                fn_args.get("title") or "",
                user_id=user_id,
                depends_on=fn_args.get("depends_on"),
                write_set=fn_args.get("write_set"),
                isolated=bool(fn_args.get("isolated")),
            )
            if not res.get("ok"):
                return f"Error posting epic: {res.get('error', 'unknown')}."
            out = f"Posted epic {res['id']} to the board."
            if res.get("isolated"):
                out += (
                    f" ISOLATED: a dedicated writer worktree was created at "
                    f"{res.get('workspacePath', '?')} — whoever picks up this "
                    f"epic works ONLY there and submits via integration_submit; "
                    f"the canonical checkout stays untouched."
                )
            elif res.get("isolationError"):
                out += (
                    f" NOTE: isolation was requested but the worktree could "
                    f"not be created ({res['isolationError']}); the epic was "
                    f"blocked and will NOT run in the shared tree."
                )
            return out
        if fn_name == "project_board_claim":
            res = claim_task(
                project_path,
                current_conv_id,
                fn_args.get("task_id") or "",
                user_id=user_id,
            )
            if res.get("ok"):
                return (
                    "Claimed. Siblings now see you own this epic; complete it "
                    "with project_board_complete when done."
                )
            if res.get("error") == "already_claimed":
                return (
                    f"NOT claimed — epic is already being advanced by "
                    f"conversation {res.get('owner', '?')}. Avoid duplicating "
                    f"it; pick a different open epic or coordinate."
                )
            return f"Error claiming epic: {res.get('error', 'unknown')}."
        if fn_name == "project_board_complete":
            res = complete_task(
                project_path,
                current_conv_id,
                fn_args.get("task_id") or "",
                user_id=user_id,
            )
            return (
                "Marked done."
                if res.get("ok")
                else f"Error completing epic: {res.get('error', 'unknown')}."
            )
        if fn_name == "project_board_block":
            res = block_task(
                project_path,
                current_conv_id,
                fn_args.get("task_id") or "",
                fn_args.get("reason") or "",
                user_id=user_id,
                question=fn_args.get("question") or "",
                options=fn_args.get("options"),
            )
            if res.get("ok") and (fn_args.get("question") or "").strip():
                return (
                    "Reported blocked WITH a question for the human. The "
                    "board panel now shows your question with answer "
                    "controls (one-click options / free text) in its "
                    '"Needs you" surface, and the collaboration bar counts '
                    "it as work that is STOPPED. The epic "
                    "will NOT auto-retry — the moment the human answers, "
                    "it is re-dispatched with the answer in the kickoff "
                    "context. Do NOT re-block on the same gate meanwhile.\n"
                    "While you wait: this epic is parked, but YOU are not. "
                    "Pick up another open epic, or advance any part of this "
                    "one that does not depend on the answer."
                )
            if res.get("ok"):
                mins = max(0, int(res.get("blocked_until") or 0) - _now_ms()) // 60_000
                return (
                    "Reported blocked. This epic is now on a self-expiring "
                    f"cooldown (~{mins}m, block #{res.get('block_count', 1)}) "
                    "so the autonomous heartbeat will NOT re-dispatch it "
                    "until the external gate has had time to clear. The "
                    "cooldown escalates on repeated blocks and auto-expires "
                    "(no human un-block needed); a human reopen resets it "
                    "for an immediate retry. Tag the reason with the block "
                    "class ([human-gated] vs [sibling]) so it is visible on "
                    "the board.\n"
                    "If this gate is really a DECISION you could make "
                    "yourself — reversible, and a matter of engineering "
                    "judgement rather than taste, policy or credentials — "
                    "reopen it, pick the most robust long-term option, and "
                    "record the choice with project_charter_propose instead "
                    "of leaving the epic parked (a proposal is the ONLY "
                    "agent path into the charter — commit is human-only)."
                )
            return f"Error reporting block: {res.get('error', 'unknown')}."
        return f"Error: Unknown board tool '{fn_name}'"
    except Exception as e:
        logger.warning("[Board] tool %s failed: %s", fn_name, e, exc_info=True)
        return f"Error executing {fn_name}: {e}"


__all__ = [
    "read_board",
    "post_task",
    "claim_task",
    "complete_task",
    "block_task",
    "reopen_task",
    "answer_task",
    "render_board_block",
    "execute_board_tool",
    "_effective_status",
    "claims_by_conv",
    "DEFAULT_LEASE_TTL_MS",
    "BLOCK_COOLDOWN_BASE_MS",
    "BLOCK_COOLDOWN_MAX_MS",
    "SIBLING_BLOCK_COOLDOWN_MS",
    "_block_cooldown_ms",
    "set_write_set",
]

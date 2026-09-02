"""Owner-scoped project Board and Watch operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import time
import uuid
from typing import Any

from lib.conversations.project_board_policy import (
    DEFAULT_LEASE_TTL_MS,
    SIBLING_BLOCK_TAG,
    block_cooldown_ms,
    effective_board_status,
)
from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


def _board_public(row: Mapping[str, Any], now: int) -> dict[str, Any]:
    stored_status = str(row["status"] or "open")
    lease = int(row["lease_expires_at"] or 0)
    status = effective_board_status(stored_status, lease, now)

    def decoded(name):
        value = _load(row[name])
        return value if isinstance(value, list) else []

    # Project the stored JSON into the public board contract.
    block_question = None
    _bq_raw = row["block_question"] or ""
    if _bq_raw:
        _bq = _load(_bq_raw)
        if isinstance(_bq, dict) and _bq.get("q"):
            block_question = {
                "q": str(_bq["q"]),
                "options": _bq["options"]
                if isinstance(_bq.get("options"), list)
                else [],
            }
    return {
        "id": row["id"],
        "project_path": row["project_path"],
        "title": row["title"],
        "status": status,
        # Expired claims expose no stale ownership, lease, or dispatch badge.
        "owner_conv_id": row["owner_conv_id"] if status == "claimed" else "",
        "lease_expires_at": lease if status == "claimed" else 0,
        "created_by_conv": row["created_by_conv"],
        "depends_on": decoded("depends_on"),
        "kind": row["kind"],
        "dispatched": bool(row["dispatched"]) and status == "claimed",
        "blocked_until": int(row["blocked_until"] or 0),
        "block_count": int(row["block_count"] or 0),
        "block_reason": row["block_reason"],
        "wait_paths": decoded("wait_paths"),
        "dispatch_target": row["dispatch_target"],
        "write_set": decoded("write_set"),
        "block_question": block_question,
        "human_answer": row["human_answer"],
        "blocked_by": row["blocked_by"],
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _board_owner(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "user_id", minimum=1)


def _remote_worktree_token(session: Session, conv_id: str, user_id: int) -> str:
    """Read the claim/post conversation's remote-root write_set token."""
    if not conv_id:
        return ""
    try:
        row = session.fetch_one(
            "SELECT settings_json FROM storage_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        )
        if row is None:
            return ""
        settings = _load(row["settings_json"]) or {}
        path = settings.get("projectPath") if isinstance(settings, Mapping) else ""
        return path if isinstance(path, str) and path.startswith("remote:") else ""
    except Exception as e:
        logger.debug("[Board] remote binding read failed conv=%s: %s", conv_id[:8], e)
        return ""


def _merge_remote_token(write_set: Any, token: str) -> list[str]:
    out = [str(item) for item in (write_set or [])]
    if token and token not in out:
        out.append(token)
    return out


def _board_list(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    rows = session.fetch_all(
        "SELECT * FROM storage_board_tasks "
        "WHERE user_id=? AND project_path=? ORDER BY created_at ASC",
        (user_id, project_path),
    )
    now = int(time.time() * 1000)
    tasks = [_board_public(row, now) for row in rows]
    counts = {"open": 0, "claimed": 0, "done": 0, "blocked": 0}
    for task in tasks:
        if task["kind"] == "lease":
            continue
        if task["status"] == "open" and int(task["blocked_until"]) > now:
            counts["blocked"] += 1
        elif task["status"] in counts:
            counts[task["status"]] += 1
    return {"tasks": tasks, **counts}


def _board_post(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    title = str(payload.get("title") or "").strip()[:2000]
    if not title:
        raise StorageError("database_protocol_error", "empty title")
    session.lock_key("board.project", f"{user_id}:{project_path}")
    max_active = _integer(payload, "max_active", default=200, minimum=1)
    active = session.fetch_one(
        "SELECT COUNT(*) AS count FROM storage_board_tasks "
        "WHERE user_id=? AND project_path=? AND status!='done'",
        (user_id, project_path),
    )
    if active and int(active["count"]) >= max_active:
        return {
            "ok": False,
            "error": f"board full: {max_active} active epics "
            "(complete or reopen some before posting more)",
        }
    # Bounded done-history: prune the OLDEST done rows beyond the retain cap in
    # the SAME transaction as the post (legacy post_task kept admission+prune+
    # insert atomic; that must live in the op, not the caller, to stay atomic).
    max_done = _integer(payload, "max_done_retained", default=100, minimum=0)
    done_rows = session.fetch_all(
        "SELECT id FROM storage_board_tasks "
        "WHERE user_id=? AND project_path=? AND status='done' "
        "ORDER BY updated_at ASC",
        (user_id, project_path),
    )
    overflow = len(done_rows) - max_done
    if overflow > 0:
        marks = ",".join("?" for _ in range(overflow))
        session.execute(
            f"DELETE FROM storage_board_tasks WHERE user_id=? AND id IN ({marks})",
            (user_id, *(r["id"] for r in done_rows[:overflow])),
        )
    now = int(time.time() * 1000)
    task_id = "pt_" + uuid.uuid4().hex[:16]
    conv_id = str(payload.get("conv_id") or "")
    depends = [str(item) for item in payload.get("depends_on") or []]
    write_set = _merge_remote_token(
        [str(item) for item in payload.get("write_set") or []],
        _remote_worktree_token(session, conv_id, user_id),
    )
    session.execute(
        "INSERT INTO storage_board_tasks "
        "(id,user_id,project_path,title,status,owner_conv_id,lease_expires_at,"
        "created_by_conv,depends_on,write_set,created_at,updated_at) "
        "VALUES (?,?,?,?,'open','',0,?,?,?,?,?)",
        (
            task_id,
            user_id,
            project_path,
            title,
            conv_id,
            _dump(depends),
            _dump(write_set),
            now,
            now,
        ),
    )
    return {"ok": True, "id": task_id}


def _board_claim(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    task_id = _required_text(payload, "task_id", 128)
    conv_id = str(payload.get("conv_id") or "")
    now = int(time.time() * 1000)
    session.lock_key("board.task", f"{user_id}:{task_id}")
    row = session.fetch_one(
        "SELECT * FROM storage_board_tasks "
        "WHERE id=? AND user_id=? AND project_path=?",
        (task_id, user_id, project_path),
    )
    if row is None:
        return {"ok": False, "error": "task not found"}
    if row["status"] == "done":
        return {"ok": False, "error": "already_done"}
    owner = str(row["owner_conv_id"] or "")
    lease_until = int(row["lease_expires_at"] or 0)
    effective = (
        "open" if row["status"] == "claimed" and lease_until <= now else row["status"]
    )
    if effective == "claimed" and owner and owner != conv_id:
        return {"ok": False, "error": "already_claimed", "owner": owner}
    refreshed = effective == "claimed" and owner == conv_id
    lease = now + max(60_000, int(payload.get("ttl_ms") or DEFAULT_LEASE_TTL_MS))
    # A self-refresh must not erase the fact that the project dispatcher
    # launched the claim merely because a generic heartbeat passed the default
    # False later.  The marker is monotonic for the life of this claim.
    dispatched = bool(payload.get("dispatched"))
    if refreshed:
        dispatched = dispatched or bool(row["dispatched"])
    try:
        current_write_set = _load(row["write_set"]) or []
    except Exception as e:
        logger.debug("[Board] write_set parse failed task=%s: %s", task_id, e)
        current_write_set = []
    merged_write_set = _merge_remote_token(
        current_write_set if isinstance(current_write_set, list) else [],
        _remote_worktree_token(session, conv_id, user_id),
    )
    # board.task's transaction lock serializes set_write_set and claim; the
    # owner/lease CAS below remains the cross-writer race guard.
    changed = session.execute(
        "UPDATE storage_board_tasks SET status='claimed', owner_conv_id=?, lease_expires_at=?, dispatched=?, updated_at=?, write_set=? "
        "WHERE id=? AND user_id=? AND project_path=? "
        "AND COALESCE(owner_conv_id,'')=? AND COALESCE(lease_expires_at,0)=?",
        (
            conv_id,
            lease,
            1 if dispatched else 0,
            now,
            _dump(merged_write_set),
            task_id,
            user_id,
            project_path,
            owner,
            lease_until,
        ),
    )
    if not changed:
        current = session.fetch_one(
            "SELECT owner_conv_id FROM storage_board_tasks "
            "WHERE id=? AND user_id=? AND project_path=?",
            (task_id, user_id, project_path),
        )
        current_owner = str(current["owner_conv_id"] or "") if current else ""
        return {
            "ok": False,
            "error": "already_claimed"
            if current_owner and current_owner != conv_id
            else "claim_conflict",
            **({"owner": current_owner} if current_owner else {}),
        }
    return {
        "ok": True,
        "lease_expires_at": lease,
        "title": row["title"],
        "transitioned": not refreshed,
        "refreshed": refreshed,
    }


def _board_dispatch(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically claim one epic and enqueue its workflow kickoff.

    A queue validation or insert failure rolls the claim back with the same
    storage transaction, so a failed dispatch cannot leave a phantom lease.
    """
    task_id = _required_text(payload, "task_id", 128)
    target_conv_id = _required_text(payload, "conv_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    message = payload.get("message")
    config = payload.get("config")
    if not isinstance(message, Mapping) or not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid dispatch document")
    if str(message.get("boardTaskId") or "") != task_id:
        raise StorageError(
            "database_protocol_error", "Dispatch payload does not match board task"
        )

    claim = _board_claim(
        session,
        {
            "project_path": payload.get("project_path"),
            "task_id": task_id,
            "conv_id": target_conv_id,
            "user_id": user_id,
            "ttl_ms": payload.get("ttl_ms"),
            "dispatched": True,
        },
    )
    if not claim.get("ok"):
        return claim

    # Local import keeps the board/watch operation module independent of the
    # queue operation registry while reusing the queue's ownership, ordering,
    # and duplicate-kickoff rules.
    from lib.storage_sidecar.operations_pkg._queue import _queue_enqueue

    queued = _queue_enqueue(
        session,
        {
            "user_id": user_id,
            "conv_id": target_conv_id,
            "queue_id": payload.get("queue_id"),
            "message": dict(message),
            "config": dict(config),
            "kind": "workflow_step",
            "priority": payload.get("priority", 50),
            "created_at_ms": payload.get("created_at_ms"),
        },
    )
    return {
        **claim,
        "queueId": queued["queueId"],
        "position": queued["position"],
        "deduped": bool(queued.get("deduped")),
    }


def _board_complete(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    task_id = _required_text(payload, "task_id", 128)
    now = int(time.time() * 1000)
    session.lock_key("board.task", f"{user_id}:{task_id}")
    row = session.fetch_one(
        "SELECT title,status FROM storage_board_tasks "
        "WHERE id=? AND user_id=? AND project_path=?",
        (task_id, user_id, project_path),
    )
    if row is None:
        return {"ok": False, "error": "task not found"}
    if str(row["status"] or "") == "done":
        return {
            "ok": True,
            "title": str(row["title"] or ""),
            "transitioned": False,
        }
    changed = session.execute(
        "UPDATE storage_board_tasks SET status='done', lease_expires_at=0, dispatched=0, "
        "blocked_until=0, block_count=0, block_reason='', wait_paths='[]', dispatch_target='', "
        "block_question='', human_answer='', updated_at=? "
        "WHERE id=? AND user_id=? AND project_path=? AND status<>'done'",
        (now, task_id, user_id, project_path),
    )
    if not changed:
        return {"ok": False, "error": "task not found"}
    return {
        "ok": True,
        "title": str(row["title"] or ""),
        "transitioned": True,
    }


def _board_mutate(session: Session, payload: Mapping[str, Any]) -> Any:
    action = _required_text(payload, "action", 32)
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    task_id = _required_text(payload, "task_id", 128)
    session.lock_key("board.task", f"{user_id}:{task_id}")
    row = session.fetch_one(
        "SELECT * FROM storage_board_tasks "
        "WHERE id=? AND user_id=? AND project_path=?",
        (task_id, user_id, project_path),
    )
    if row is None:
        return {"ok": False, "error": "task not found"}
    now = int(time.time() * 1000)
    if action == "block":
        reason = str(payload.get("reason") or "")[:2000]
        count = int(row["block_count"] or 0) + 1
        block_class = "sibling" if SIBLING_BLOCK_TAG in reason.lower() else "human"
        cooldown = block_cooldown_ms(count, block_class)
        question = payload.get("question_json") or ""
        changed = session.execute(
            "UPDATE storage_board_tasks SET blocked_until=?, block_count=?, "
            "block_reason=?, block_question=?, human_answer=?, blocked_by=?, "
            "updated_at=? WHERE id=? AND user_id=? AND project_path=? "
            "AND block_count=?",
            (
                now + cooldown,
                count,
                reason,
                question,
                "",
                str(payload.get("conv_id") or ""),
                now,
                task_id,
                user_id,
                project_path,
                count - 1,
            ),
        )
        return (
            {
                "ok": True,
                "blocked_until": now + cooldown,
                "block_count": count,
                "title": row["title"],
            }
            if changed
            else {"ok": False, "error": "block_conflict"}
        )
    if action == "reopen":
        status = str(row["status"] or "open")
        if status == "open" and int(row["blocked_until"] or 0) <= now:
            return {"ok": False, "error": "already_open"}
        changed = session.execute(
            "UPDATE storage_board_tasks SET status='open', owner_conv_id='', "
            "lease_expires_at=0, dispatched=0, blocked_until=0, block_count=0, "
            "block_reason='', wait_paths='[]', dispatch_target='', "
            "block_question='', human_answer='', updated_at=? "
            "WHERE id=? AND user_id=? AND project_path=? AND status=? "
            "AND COALESCE(owner_conv_id,'')=? AND blocked_until=?",
            (
                now,
                task_id,
                user_id,
                project_path,
                status,
                str(row["owner_conv_id"] or ""),
                int(row["blocked_until"] or 0),
            ),
        )
        return (
            {
                "ok": True,
                "from": status,
                "title": row["title"],
                "prev_owner": str(row["owner_conv_id"] or ""),
                "transitioned": True,
            }
            if changed
            else {"ok": False, "error": "reopen_conflict"}
        )
    if action == "delete":
        dependents = session.fetch_all(
            "SELECT title, depends_on FROM storage_board_tasks "
            "WHERE user_id=? AND project_path=? AND status!=? AND id!=?",
            (user_id, project_path, "done", task_id),
        )
        names = []
        for dep in dependents:
            if task_id in (_load(dep["depends_on"]) or []):
                names.append(str(dep["title"] or "")[:80])
        if names:
            return {"ok": False, "error": "has_dependents", "dependents": names}
        return (
            {
                "ok": True,
                "title": row["title"],
                "prev_status": str(row["status"] or "open"),
                "prev_owner": str(row["owner_conv_id"] or ""),
            }
            if session.execute(
                "DELETE FROM storage_board_tasks "
                "WHERE id=? AND user_id=? AND project_path=?",
                (task_id, user_id, project_path),
            )
            else {"ok": False, "error": "task not found"}
        )
    if action == "answer":
        answer = str(payload.get("answer") or "")[:2000]
        if not answer:
            return {"ok": False, "error": "missing answer"}
        if not str(row["block_question"] or "").strip():
            return {"ok": False, "error": "no_pending_question"}
        changed = session.execute(
            "UPDATE storage_board_tasks SET human_answer=?, blocked_until=0, "
            "block_count=0, block_reason='', block_question='', updated_at=? "
            "WHERE id=? AND user_id=? AND project_path=? AND block_question=?",
            (answer, now, task_id, user_id, project_path, row["block_question"]),
        )
        return (
            {
                "ok": True,
                "title": row["title"],
                "question_text": str(
                    (_load(row["block_question"]) or {}).get("q") or ""
                ),
            }
            if changed
            else {"ok": False, "error": "answer_conflict"}
        )
    if action == "write_set":
        changed = session.execute(
            "UPDATE storage_board_tasks SET write_set=?, updated_at=? "
            "WHERE id=? AND user_id=? AND project_path=?",
            (
                _dump(payload.get("write_set") or []),
                now,
                task_id,
                user_id,
                project_path,
            ),
        )
        return {"ok": True} if changed else {"ok": False, "error": "task not found"}
    if action == "migrate":
        target = str(payload.get("dispatch_target") or "").strip()
        if not target:
            raise StorageError("database_protocol_error", "missing dispatch target")
        changed = session.execute(
            "UPDATE storage_board_tasks SET dispatch_target=?, status='open', "
            "owner_conv_id='', lease_expires_at=0, dispatched=0, updated_at=? "
            "WHERE id=? AND user_id=? AND project_path=? AND status<>'done'",
            (target, now, task_id, user_id, project_path),
        )
        return {"ok": True} if changed else {"ok": False, "error": "migration_conflict"}
    raise StorageError("database_protocol_error", f"Unknown board action: {action}")


def _board_reopen(session: Session, payload: Mapping[str, Any]) -> Any:
    """Receipt-backed board reopen with its own lifecycle operation name."""
    return _board_mutate(session, {**dict(payload), "action": "reopen"})


def _board_write_set(session: Session, payload: Mapping[str, Any]) -> Any:
    """Naturally idempotent board write-set replacement."""
    return _board_mutate(session, {**dict(payload), "action": "write_set"})


def _watch_public(
    row: Mapping[str, Any], responses: list[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    kind = str(row["kind"])
    status = str(row["status"] or "open")
    return {
        "item_id": row["item_id"],
        "project_path": row["project_path"],
        "response_fingerprint": row["response_fingerprint"] or "",
        "kind": kind,
        "text": row["text"] or "",
        "status": status,
        "promotedAudit": bool(row["promoted"]),
        "promotionState": "none",
        "divergedSide": "",
        "injected": kind == "goal" and status == "open",
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "responses": list(responses or []),
    }


def _watch_response_rows(
    session: Session, item_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    rows = session.fetch_all(
        "SELECT sequence,response,pillar_state_json,trigger,ts FROM storage_watch_responses WHERE item_id=? ORDER BY sequence DESC LIMIT ?",
        (item_id, max(1, min(int(limit), 100))),
    )
    return [
        {
            "seq": int(row["sequence"]),
            "response": row["response"] or "",
            "pillar_state": _load(row["pillar_state_json"]) or {},
            "trigger": row["trigger"] or "",
            "ts": int(row["ts"] or 0),
        }
        for row in rows
    ]


def _watch_list(session: Session, payload: Mapping[str, Any]) -> Any:
    project_path = _required_text(payload, "project_path", 4096)
    user_id = _board_owner(payload)
    include_resolved = bool(payload.get("include_resolved", True))
    limit = max(1, min(int(payload.get("response_limit") or 20), 100))
    sql = "SELECT * FROM storage_watch_items WHERE user_id=? AND project_path=?"
    params: list[Any] = [user_id, project_path]
    if not include_resolved:
        sql += " AND status='open'"
    rows = session.fetch_all(sql + " ORDER BY updated_at DESC", tuple(params))
    return {
        "items": [
            _watch_public(row, _watch_response_rows(session, row["item_id"], limit))
            for row in rows
        ],
        "charterVersion": 0,
    }


def _watch_get(session: Session, payload: Mapping[str, Any]) -> Any:
    item_id = _required_text(payload, "item_id", 128)
    user_id = _board_owner(payload)
    row = session.fetch_one(
        "SELECT * FROM storage_watch_items WHERE item_id=? AND user_id=?",
        (item_id, user_id),
    )
    return (
        None
        if row is None
        else _watch_public(
            row,
            _watch_response_rows(
                session, item_id, int(payload.get("response_limit") or 20)
            ),
        )
    )


def _watch_mutate(session: Session, payload: Mapping[str, Any]) -> Any:
    action = _required_text(payload, "action", 32)
    user_id = _board_owner(payload)
    item_id = str(payload.get("item_id") or "")
    project_path = str(payload.get("project_path") or "")
    if action == "add":
        project_path = _required_text(payload, "project_path", 4096)
        item_id = item_id or ("watch_" + uuid.uuid4().hex[:16])
        now = int(time.time() * 1000)
        session.execute(
            "INSERT INTO storage_watch_items "
            "(item_id,user_id,project_path,kind,text,status,promoted,"
            "response_fingerprint,created_by_conv,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'open',0,'',?,?,?)",
            (
                item_id,
                user_id,
                project_path,
                str(payload.get("kind") or "concern"),
                str(payload.get("text") or "")[:8000],
                str(payload.get("created_by_conv") or ""),
                now,
                now,
            ),
        )
        row = session.fetch_one(
            "SELECT * FROM storage_watch_items WHERE item_id=? AND user_id=?",
            (item_id, user_id),
        )
        return {"ok": True, "item": _watch_public(row)}
    session.lock_key("watch.item", f"{user_id}:{item_id}")
    row = session.fetch_one(
        "SELECT * FROM storage_watch_items WHERE item_id=? AND user_id=?",
        (item_id, user_id),
    )
    if row is None:
        return {"ok": False, "error": "not found"}
    now = int(time.time() * 1000)
    if action == "edit":
        text = (
            row["text"]
            if payload.get("text") is None
            else str(payload.get("text") or "")[:8000]
        )
        kind = (
            row["kind"]
            if payload.get("kind") is None
            else str(payload.get("kind") or "")
        )
        changed = session.execute(
            "UPDATE storage_watch_items SET text=?,kind=?,response_fingerprint=?,"
            "updated_at=? WHERE item_id=? AND user_id=? AND text=? AND kind=? "
            "AND updated_at=?",
            (
                text,
                kind,
                "" if text != row["text"] else row["response_fingerprint"],
                now,
                item_id,
                user_id,
                row["text"],
                row["kind"],
                row["updated_at"],
            ),
        )
        return {"ok": True} if changed else {"ok": False, "error": "edit_conflict"}
    if action == "status":
        changed = session.execute(
            "UPDATE storage_watch_items SET status=?,updated_at=? "
            "WHERE item_id=? AND user_id=?",
            (str(payload.get("status") or ""), now, item_id, user_id),
        )
        return {"ok": True} if changed else {"ok": False, "error": "not found"}
    if action == "promote":
        changed = session.execute(
            "UPDATE storage_watch_items SET promoted=1, updated_at=? "
            "WHERE item_id=? AND user_id=?",
            (now, item_id, user_id),
        )
        return {"ok": True} if changed else {"ok": False, "error": "not found"}
    if action == "delete":
        session.execute(
            "DELETE FROM storage_watch_responses WHERE item_id=?", (item_id,)
        )
        return (
            {"ok": True}
            if session.execute(
                "DELETE FROM storage_watch_items WHERE item_id=? AND user_id=?",
                (item_id, user_id),
            )
            else {"ok": False, "error": "not found"}
        )
    raise StorageError("database_protocol_error", f"Unknown watch action: {action}")


def _watch_edit(session: Session, payload: Mapping[str, Any]) -> Any:
    return _watch_mutate(session, {**dict(payload), "action": "edit"})


def _watch_status(session: Session, payload: Mapping[str, Any]) -> Any:
    return _watch_mutate(session, {**dict(payload), "action": "status"})


def _watch_promote(session: Session, payload: Mapping[str, Any]) -> Any:
    return _watch_mutate(session, {**dict(payload), "action": "promote"})


def _watch_response_append(session: Session, payload: Mapping[str, Any]) -> Any:
    item_id = _required_text(payload, "item_id", 128)
    user_id = _board_owner(payload)
    session.lock_key("watch.response", f"{user_id}:{item_id}")
    row = session.fetch_one(
        "SELECT * FROM storage_watch_items WHERE item_id=? AND user_id=?",
        (item_id, user_id),
    )
    if row is None:
        return None
    guard = payload.get("fingerprint_guard")
    if guard:
        expected_fp, expected_updated, new_fp = guard
        changed = session.execute(
            "UPDATE storage_watch_items SET response_fingerprint=?, updated_at=? "
            "WHERE item_id=? AND user_id=? AND response_fingerprint=? "
            "AND updated_at=?",
            (
                str(new_fp),
                max(int(time.time() * 1000), int(expected_updated or 0) + 1),
                item_id,
                user_id,
                str(expected_fp),
                int(expected_updated),
            ),
        )
        if not changed:
            return {"conflict": True}
    current = session.fetch_one(
        "SELECT COALESCE(MAX(sequence),0) AS sequence FROM storage_watch_responses WHERE item_id=?",
        (item_id,),
    )
    sequence = int(current["sequence"]) + 1
    now = int(time.time() * 1000)
    session.execute(
        "INSERT INTO storage_watch_responses (item_id,sequence,project_path,response,pillar_state_json,trigger,ts) VALUES (?,?,?,?,?,?,?)",
        (
            item_id,
            sequence,
            row["project_path"],
            str(payload.get("response") or ""),
            _dump(payload.get("pillar_state") or {}),
            str(payload.get("trigger") or "manual"),
            now,
        ),
    )
    # Legacy parity: project_watch._RESPONSES_KEEP=100 by default, caller-side
    # tunable (same contract as the feed/status keep params).
    keep = _integer(payload, "keep", default=100, minimum=1)
    if sequence > keep:
        session.execute(
            "DELETE FROM storage_watch_responses WHERE item_id=? AND sequence<=?",
            (item_id, sequence - keep),
        )
    return {
        "seq": sequence,
        "response": str(payload.get("response") or ""),
        "pillar_state": payload.get("pillar_state") or {},
        "trigger": str(payload.get("trigger") or "manual"),
        "ts": now,
    }


# Offline import operations stay owner-scoped even when their legacy source
# tables predate identity. The outer payload is the authority; a document may
# repeat that owner for auditability but can never select a different owner.
_BOARD_IMPORT_JSON_COLUMNS = frozenset({"depends_on", "wait_paths", "write_set"})
_BOARD_IMPORT_COLUMNS = (
    "id",
    "user_id",
    "project_path",
    "title",
    "status",
    "owner_conv_id",
    "lease_expires_at",
    "created_by_conv",
    "depends_on",
    "kind",
    "dispatched",
    "blocked_until",
    "block_count",
    "block_reason",
    "wait_paths",
    "dispatch_target",
    "write_set",
    "block_question",
    "human_answer",
    "blocked_by",
    "created_at",
    "updated_at",
)
_BOARD_IMPORT_INT_COLUMNS = frozenset({
    "user_id",
    "lease_expires_at",
    "dispatched",
    "blocked_until",
    "block_count",
    "created_at",
    "updated_at",
})


def _import_document_owner(document: Mapping[str, Any], user_id: int) -> None:
    supplied = document.get("user_id")
    if supplied is None:
        return
    try:
        supplied_user_id = int(supplied)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            "database_protocol_error", "Invalid owner in import document"
        ) from exc
    if supplied_user_id != user_id:
        raise StorageError(
            "database_forbidden", "Import document owner does not match principal"
        )


def _board_task_canonical(
    document: Mapping[str, Any], *, user_id: int
) -> dict[str, Any]:
    _import_document_owner(document, user_id)
    canonical: dict[str, Any] = {"user_id": user_id}
    for column in _BOARD_IMPORT_COLUMNS:
        if column == "user_id":
            continue
        value = document.get(column)
        if column in _BOARD_IMPORT_JSON_COLUMNS:
            if not isinstance(value, list):
                parsed = _load(value)
                value = parsed if isinstance(parsed, list) else []
            canonical[column] = value
        elif column in _BOARD_IMPORT_INT_COLUMNS:
            canonical[column] = int(value or 0)
        else:
            canonical[column] = str(value or "")
    if not canonical["id"] or not canonical["project_path"]:
        raise StorageError(
            "database_protocol_error", "Invalid board import document"
        )
    return canonical


def _board_row_canonical(
    row: Mapping[str, Any], *, user_id: int
) -> dict[str, Any]:
    return _board_task_canonical(
        {column: row[column] for column in _BOARD_IMPORT_COLUMNS},
        user_id=user_id,
    )


def _board_import_batch(session: Session, payload: Mapping[str, Any]) -> Any:
    """Import a bounded legacy board batch under one explicit owner."""
    user_id = _board_owner(payload)
    documents = payload.get("documents")
    if not isinstance(documents, list) or not documents or len(documents) > 100:
        raise StorageError(
            "database_protocol_error",
            "Board import batch must contain 1..100 documents",
        )
    migrated = 0
    verified = 0
    digest = hashlib.sha256()
    for document in documents:
        if not isinstance(document, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid board import document"
            )
        canonical = _board_task_canonical(document, user_id=user_id)
        encoded = _dump(canonical)
        digest.update(hashlib.sha256(encoded).digest())
        session.lock_key("board.task", f"{user_id}:{canonical['id']}")
        current = session.fetch_one(
            "SELECT * FROM storage_board_tasks WHERE id=?",
            (canonical["id"],),
        )
        if current is not None:
            if int(current["user_id"]) != user_id:
                raise StorageError(
                    "database_conflict", "Board import identifier is already allocated"
                )
            if _dump(_board_row_canonical(current, user_id=user_id)) != encoded:
                raise StorageError(
                    "database_conflict",
                    f"Board import conflicts with {canonical['id']}",
                )
            verified += 1
            continue
        session.execute(
            "INSERT INTO storage_board_tasks ("
            + ", ".join(_BOARD_IMPORT_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _BOARD_IMPORT_COLUMNS)
            + ")",
            tuple(
                _dump(canonical[column])
                if column in _BOARD_IMPORT_JSON_COLUMNS
                else canonical[column]
                for column in _BOARD_IMPORT_COLUMNS
            ),
        )
        migrated += 1
    return {
        "migrated": migrated,
        "verified": verified,
        "count": len(documents),
        "digest": digest.hexdigest(),
    }


_WATCH_ITEM_COLUMNS = (
    "item_id",
    "user_id",
    "project_path",
    "kind",
    "text",
    "status",
    "promoted",
    "response_fingerprint",
    "created_by_conv",
    "created_at",
    "updated_at",
)
_WATCH_RESPONSE_COLUMNS = (
    "item_id",
    "sequence",
    "project_path",
    "response",
    "pillar_state_json",
    "trigger",
    "ts",
)


def _watch_item_canonical(
    document: Mapping[str, Any], *, user_id: int
) -> dict[str, Any]:
    _import_document_owner(document, user_id)
    canonical = {
        "item_id": str(document.get("item_id") or ""),
        "user_id": user_id,
        "project_path": str(document.get("project_path") or ""),
        "kind": str(document.get("kind") or ""),
        "text": str(document.get("text") or ""),
        "status": str(document.get("status") or "open"),
        "promoted": int(document.get("promoted") or 0),
        "response_fingerprint": str(document.get("response_fingerprint") or ""),
        "created_by_conv": str(document.get("created_by_conv") or ""),
        "created_at": int(document.get("created_at") or 0),
        "updated_at": int(document.get("updated_at") or 0),
    }
    if not canonical["item_id"] or not canonical["project_path"]:
        raise StorageError(
            "database_protocol_error", "Invalid watch import item"
        )
    return canonical


def _watch_response_canonical(document: Mapping[str, Any]) -> dict[str, Any]:
    pillar_state = document.get("pillar_state")
    if not isinstance(pillar_state, Mapping):
        parsed = _load(pillar_state)
        pillar_state = parsed if isinstance(parsed, Mapping) else {}
    canonical = {
        "item_id": str(document.get("item_id") or ""),
        "sequence": int(document.get("sequence") or 0),
        "project_path": str(document.get("project_path") or ""),
        "response": str(document.get("response") or ""),
        "pillar_state": dict(pillar_state),
        "trigger": str(document.get("trigger") or ""),
        "ts": int(document.get("ts") or 0),
    }
    if (
        not canonical["item_id"]
        or not canonical["project_path"]
        or canonical["sequence"] < 1
    ):
        raise StorageError(
            "database_protocol_error", "Invalid watch import response"
        )
    return canonical


def _watch_import_batch(session: Session, payload: Mapping[str, Any]) -> Any:
    """Import bounded Watch rows only beneath the explicit payload owner."""
    user_id = _board_owner(payload)
    items = payload.get("items") or []
    responses = payload.get("responses") or []
    if (
        not isinstance(items, list)
        or not isinstance(responses, list)
        or not (items or responses)
        or len(items) + len(responses) > 200
    ):
        raise StorageError(
            "database_protocol_error",
            "Watch import batch must contain 1..200 documents",
        )
    migrated_items = verified_items = 0
    migrated_responses = verified_responses = 0
    digest = hashlib.sha256()
    for document in items:
        if not isinstance(document, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid watch import item"
            )
        canonical = _watch_item_canonical(document, user_id=user_id)
        digest.update(hashlib.sha256(_dump(canonical)).digest())
        session.lock_key("watch.item", f"{user_id}:{canonical['item_id']}")
        current = session.fetch_one(
            "SELECT * FROM storage_watch_items WHERE item_id=?",
            (canonical["item_id"],),
        )
        if current is not None:
            if int(current["user_id"]) != user_id:
                raise StorageError(
                    "database_conflict", "Watch import identifier is already allocated"
                )
            existing = _watch_item_canonical(
                {column: current[column] for column in _WATCH_ITEM_COLUMNS},
                user_id=user_id,
            )
            if _dump(existing) != _dump(canonical):
                raise StorageError(
                    "database_conflict",
                    f"Watch import conflicts with {canonical['item_id']}",
                )
            verified_items += 1
            continue
        session.execute(
            "INSERT INTO storage_watch_items ("
            + ", ".join(_WATCH_ITEM_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _WATCH_ITEM_COLUMNS)
            + ")",
            tuple(canonical[column] for column in _WATCH_ITEM_COLUMNS),
        )
        migrated_items += 1
    for document in responses:
        if not isinstance(document, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid watch import response"
            )
        _import_document_owner(document, user_id)
        canonical = _watch_response_canonical(document)
        digest.update(hashlib.sha256(_dump({
            **canonical, "user_id": user_id,
        })).digest())
        session.lock_key("watch.response", f"{user_id}:{canonical['item_id']}")
        owner = session.fetch_one(
            "SELECT user_id,project_path FROM storage_watch_items WHERE item_id=?",
            (canonical["item_id"],),
        )
        if owner is None:
            raise StorageError(
                "database_conflict", "Watch response parent item is missing"
            )
        if int(owner["user_id"]) != user_id:
            raise StorageError(
                "database_conflict", "Watch response parent belongs to another owner"
            )
        if str(owner["project_path"]) != canonical["project_path"]:
            raise StorageError(
                "database_conflict", "Watch response project does not match its item"
            )
        current = session.fetch_one(
            "SELECT * FROM storage_watch_responses "
            "WHERE item_id=? AND sequence=?",
            (canonical["item_id"], canonical["sequence"]),
        )
        if current is not None:
            existing = _watch_response_canonical({
                "item_id": current["item_id"],
                "sequence": current["sequence"],
                "project_path": current["project_path"],
                "response": current["response"],
                "pillar_state": current["pillar_state_json"],
                "trigger": current["trigger"],
                "ts": current["ts"],
            })
            if _dump(existing) != _dump(canonical):
                raise StorageError(
                    "database_conflict",
                    "Watch response import conflicts with "
                    f"{canonical['item_id']}#{canonical['sequence']}",
                )
            verified_responses += 1
            continue
        session.execute(
            "INSERT INTO storage_watch_responses ("
            + ", ".join(_WATCH_RESPONSE_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _WATCH_RESPONSE_COLUMNS)
            + ")",
            tuple(
                _dump(canonical["pillar_state"])
                if column == "pillar_state_json"
                else canonical[column]
                for column in _WATCH_RESPONSE_COLUMNS
            ),
        )
        migrated_responses += 1
    return {
        "migrated_items": migrated_items,
        "verified_items": verified_items,
        "migrated_responses": migrated_responses,
        "verified_responses": verified_responses,
        "count": len(items) + len(responses),
        "digest": digest.hexdigest(),
    }

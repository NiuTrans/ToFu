"""Per-conversation queue and autopilot-marker operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any


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


def _queue_conv_id(payload: Mapping[str, Any]) -> str:
    return _required_text(payload, "conv_id", 256)


def _queue_owner(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "user_id", minimum=1)


def _require_owned_conversation(
    session: Session, conv_id: str, user_id: int,
) -> None:
    if session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations "
        "WHERE id=? AND user_id=?",
        (conv_id, user_id),
    ) is None:
        raise StorageError("database_not_found", "Conversation not found")


def _queue_marker_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "queueId": str(row["queue_id"]),
        "userId": int(row["user_id"]),
        "config": _load(row["config_json"]) or {},
        "createdAt": int(row["created_at_ms"]),
    }


def _queue_autopilot_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    row = session.fetch_one(
        "SELECT queue_id, user_id, config_json, created_at_ms "
        "FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return None if row is None else _queue_marker_document(row)


def _queue_autopilot_list_all(
    session: Session, _payload: Mapping[str, Any],
) -> Any:
    """Internal recovery feed across owners; never expose through a route."""
    rows = session.fetch_all(
        "SELECT conv_id, user_id, queue_id, config_json, created_at_ms "
        "FROM storage_autopilot_markers ORDER BY user_id, conv_id",
    )
    return [
        {
            "convId": str(row["conv_id"]),
            **_queue_marker_document(row),
        }
        for row in rows
    ]


def _queue_autopilot_arm(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    config = payload.get("config", {})
    if not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid autopilot marker config")
    _require_owned_conversation(session, conv_id, user_id)
    session.lock_key("autopilot_marker", f"{user_id}:{conv_id}")
    current = session.fetch_one(
        "SELECT queue_id, user_id, config_json, created_at_ms "
        "FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    if current is not None:
        return {
            "armed": False,
            **_queue_marker_document(current),
        }
    now = int(time.time() * 1000)
    session.execute(
        "INSERT INTO storage_autopilot_markers "
        "(conv_id, user_id, queue_id, config_json, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv_id, user_id, queue_id, _dump(dict(config)), now),
    )
    return {
        "armed": True,
        "queueId": queue_id,
        "config": dict(config),
        "createdAt": now,
    }


def _queue_autopilot_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    session.lock_key("autopilot_marker", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return {"cleared": bool(deleted)}


_QUEUE_KINDS = frozenset({"real", "peer_msg", "workflow_step", "autopilot"})


def _queue_kind(payload: Mapping[str, Any]) -> str:
    kind = payload.get("kind", "real")
    if not isinstance(kind, str) or kind not in _QUEUE_KINDS:
        raise StorageError("database_protocol_error", "Invalid queue kind")
    return kind


def _queue_priority(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "priority", default=100, minimum=0, maximum=1000)


def _queue_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """The ONE queue-row shape: durable fields (payload/config, read by the
    dispatch path) PLUS the get_queue preview contract (text/has*/peer
    attribution) the queue-bar poll reads without unpacking payload. Splitting
    these used to leave queue.list without the preview keys, so every sidecar
    row rendered as the generic attachment fallback in the UI."""
    payload = _load(row["payload_json"]) or {}
    result = {
        "queueId": str(row["id"]),
        "userId": int(row["user_id"]),
        "position": int(row["position"]),
        "kind": str(row["kind"] or "real"),
        "priority": int(row["priority"]),
        "timestamp": int(row["created_at_ms"]),
        # Preview contract — the documented lib.message_queue.get_queue keys:
        # a peer's clean _peerText wins over the framed model-facing body,
        # capped at 2000 chars like the legacy SQL branch.
        "text": str(
            payload.get("_peerText")
            if payload.get("_peerMessage")
            else payload.get("text", "") or ""
        )[:2000],
        "hasImages": bool(payload.get("images")),
        "hasPdfs": bool(payload.get("pdfTexts")),
        "hasRefs": bool(payload.get("convRefs")),
        "hasQuotes": bool(payload.get("replyQuotes")),
        "payload": payload,
        "config": _load(row["config_json"]) or {},
    }
    embedded_user = payload.get("_user_msg")
    source_message_id = str(
        (embedded_user.get("_msgId") if isinstance(embedded_user, Mapping) else "")
        or payload.get("_msgId") or ""
    )
    if source_message_id:
        result["sourceMessageId"] = source_message_id
    if payload.get("_peerMessage"):
        result.update(
            {
                "isPeerMessage": True,
                "fromConv": payload.get("_fromConv", ""),
                "isPeerHuman": bool(payload.get("_peerHuman")),
            }
        )
    return result


def _queue_rows(
    session: Session, conv_id: str, user_id: int,
) -> list[Mapping[str, Any]]:
    return session.fetch_all(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, "
        "priority, created_at_ms, leased_until_ms, lease_task_id "
        "FROM storage_queue_items WHERE conv_id = ? AND user_id = ? "
        "ORDER BY priority, position",
        (conv_id, user_id),
    )


def _queue_renumber(session: Session, conv_id: str, user_id: int) -> None:
    rows = session.fetch_all(
        "SELECT id FROM storage_queue_items WHERE conv_id = ? AND user_id = ? "
        "ORDER BY priority, position, id",
        (conv_id, user_id),
    )
    for position, row in enumerate(rows, 1):
        session.execute(
            "UPDATE storage_queue_items SET position = ? "
            "WHERE id = ? AND user_id = ?",
            (position, row["id"], user_id),
        )


def _queue_enqueue(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    kind = _queue_kind(payload)
    priority = _queue_priority(payload)
    message = payload.get("message", {})
    config = payload.get("config", {})
    if not isinstance(message, Mapping) or not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid queue document")
    _require_owned_conversation(session, conv_id, user_id)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    rows = _queue_rows(session, conv_id, user_id)
    board_task_id = message.get("boardTaskId")
    if kind == "workflow_step" and board_task_id:
        for row in rows:
            if row["kind"] == kind:
                candidate = _load(row["payload_json"]) or {}
                if candidate.get("boardTaskId") == board_task_id:
                    result = _queue_item(row)
                    result.pop("payload", None)
                    result.pop("config", None)
                    result["deduped"] = True
                    return result
    if kind == "autopilot":
        for row in rows:
            if row["kind"] == "autopilot":
                result = _queue_item(row)
                result.pop("payload", None)
                result.pop("config", None)
                result["deduped"] = True
                return result
    position = len(rows) + 1
    created_at = _integer(
        payload, "created_at_ms", default=int(time.time() * 1000), minimum=0
    )
    session.execute(
        "INSERT INTO storage_queue_items "
        "(id, user_id, conv_id, payload_json, config_json, position, kind, "
        "priority, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            queue_id,
            user_id,
            conv_id,
            _dump(dict(message)),
            _dump(dict(config)),
            position,
            kind,
            priority,
            created_at,
        ),
    )
    inserted = session.fetch_one(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, "
        "priority, created_at_ms, leased_until_ms, lease_task_id "
        "FROM storage_queue_items WHERE id=? AND user_id=?",
        (queue_id, user_id),
    )
    result = _queue_item(inserted)
    result.pop("payload", None)
    result.pop("config", None)
    result["deduped"] = False
    return result


def _queue_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    return [
        _queue_item(row) for row in _queue_rows(session, conv_id, user_id)
    ]


def _queue_remove(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_queue_items "
        "WHERE id = ? AND conv_id = ? AND user_id = ?",
        (queue_id, conv_id, user_id),
    )
    if deleted:
        _queue_renumber(session, conv_id, user_id)
    return {"removed": bool(deleted)}


def _queue_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_queue_items WHERE conv_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return {"cleared": max(0, int(deleted or 0))}


def _queue_dequeue(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    now_ms = _integer(payload, "now_ms", minimum=0)
    lease_ms = _integer(payload, "lease_ms", minimum=1, maximum=3600 * 1000)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    row = session.fetch_one(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, "
        "priority, created_at_ms, leased_until_ms, lease_task_id "
        "FROM storage_queue_items WHERE conv_id = ? AND user_id = ? AND kind != ? "
        "AND (leased_until_ms IS NULL OR leased_until_ms < ?) "
        "ORDER BY priority, position LIMIT 1",
        (conv_id, user_id, "autopilot", now_ms),
    )
    if row is None:
        return None
    session.execute(
        "UPDATE storage_queue_items SET leased_until_ms = ?, lease_task_id = ? "
        "WHERE id = ? AND user_id = ?",
        (now_ms + lease_ms, "", row["id"], user_id),
    )
    return _queue_item(row)


def _queue_lease_release(session: Session, payload: Mapping[str, Any]) -> Any:
    queue_id = _required_text(payload, "queue_id", 256)
    user_id = _queue_owner(payload)
    updated = session.execute(
        "UPDATE storage_queue_items SET leased_until_ms = NULL, "
        "lease_task_id = '' WHERE id = ? AND user_id = ?",
        (queue_id, user_id),
    )
    return {"released": bool(updated)}


def _queue_reap(session: Session, payload: Mapping[str, Any]) -> Any:
    """Release expired queue leases and return conversations to drain.

    The web process must not inspect task/queue tables directly in Sidecar
    mode.  Task liveness is still decided by the manager after this bounded
    storage repair; the Sidecar owns only the atomic lease reclamation.
    """
    now_ms = _integer(payload, "now_ms", minimum=0)
    force = bool(payload.get("force_reclaim"))
    predicate = (
        "kind != 'autopilot' AND leased_until_ms IS NOT NULL"
        if force
        else "kind != 'autopilot' AND leased_until_ms IS NOT NULL "
        "AND leased_until_ms < ?"
    )
    args = () if force else (now_ms,)
    rows = session.fetch_all(
        f"SELECT DISTINCT user_id, conv_id FROM storage_queue_items "
        f"WHERE {predicate} ORDER BY user_id, conv_id",
        args,
    )
    session.execute(
        f"UPDATE storage_queue_items SET leased_until_ms = NULL, "
        f"lease_task_id = '' WHERE {predicate}",
        args,
    )
    conversations = [
        {"userId": int(row["user_id"]), "convId": str(row["conv_id"])}
        for row in rows if row["conv_id"]
    ]
    # ``ok=False`` is the storage receipt contract for a clean no-write
    # verdict.  Idle reaper polls otherwise created thousands of permanent
    # receipts despite changing no lease state.
    return {"ok": bool(conversations), "conversations": conversations}


def _queue_lease_bind(session: Session, payload: Mapping[str, Any]) -> Any:
    queue_id = _required_text(payload, "queue_id", 256)
    user_id = _queue_owner(payload)
    task_id = _required_text(payload, "task_id", 256)
    now_ms = _integer(payload, "now_ms", minimum=0)
    lease_ms = _integer(payload, "lease_ms", minimum=1, maximum=3600 * 1000)
    updated = session.execute(
        "UPDATE storage_queue_items SET lease_task_id = ?, "
        "leased_until_ms = ? WHERE id = ? AND user_id = ?",
        (task_id, now_ms + lease_ms, queue_id, user_id),
    )
    return {"bound": bool(updated)}


def _queue_finalize(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_queue_items "
        "WHERE id = ? AND conv_id = ? AND user_id = ?",
        (queue_id, conv_id, user_id),
    )
    if deleted:
        _queue_renumber(session, conv_id, user_id)
    return {"finalized": bool(deleted)}


def _queue_depth(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    row = session.fetch_one(
        "SELECT COUNT(*) AS count FROM storage_queue_items "
        "WHERE conv_id = ? AND user_id = ? AND kind != 'autopilot'",
        (conv_id, user_id),
    )
    return {"depth": int(row["count"]) if row else 0}


def _queue_conversations_list_all(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Internal recovery feed, oldest queued conversation first.

    This crosses owners only for the process-internal dispatcher and is never
    exposed through a route.  Ordering by the oldest durable source makes the
    maintenance dispatch cap fair and deterministic.
    """
    kind = payload.get("kind")
    if kind is not None:
        kind = _queue_kind({"kind": kind})
        rows = session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at "
            "FROM storage_queue_items WHERE kind = ? "
            "GROUP BY user_id, conv_id "
            "ORDER BY oldest_created_at, user_id, conv_id",
            (kind,),
        )
    else:
        rows = session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at "
            "FROM storage_queue_items WHERE kind != 'autopilot' "
            "GROUP BY user_id, conv_id "
            "ORDER BY oldest_created_at, user_id, conv_id"
        )
    return [
        {"userId": int(row["user_id"]), "convId": str(row["conv_id"])}
        for row in rows if row["conv_id"]
    ]

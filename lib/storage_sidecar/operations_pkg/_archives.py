"""Owner-scoped semantic operations for compaction transcript archives."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


_MAX_RECEIPT_BYTES = 32 * 1024


def _identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    return (
        _required_text(payload, "conversation_id"),
        _integer(payload, "user_id", minimum=1),
    )


def _require_owned_conversation(
    session: Session, conversation_id: str, user_id: int,
) -> None:
    row = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations "
        "WHERE id=? AND user_id=?",
        (conversation_id, user_id),
    )
    if row is None:
        raise StorageError("database_not_found", "Conversation not found")


def _text(payload: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    return value


def _encoded_receipt(payload: Mapping[str, Any]) -> bytes:
    value = payload.get("receipt", {})
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_protocol_error", "Archive receipt must be an object"
        )
    encoded = _dump(dict(value))
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise StorageError(
            "database_protocol_error", "Archive receipt exceeds 32 KiB"
        )
    return encoded


def _decoded_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _load(row["receipt_json"])
    if not isinstance(value, dict):
        raise StorageError("database_integrity", "Archive receipt is malformed")
    return value


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    summary = str(row["summary"] or "")
    task_model = str(row["model"] or "")
    receipt = _decoded_receipt(row)
    return {
        "schemaVersion": "tofu.compaction-archive/v3",
        "id": str(row["archive_id"]),
        "convId": str(row["conversation_id"]),
        "createdAt": int(row["created_at_ms"]),
        "snapshotKind": "pre_compaction_transcript",
        "trigger": str(row["trigger"] or "force"),
        "taskId": str(row["task_id"] or ""),
        "roundNum": int(row["round_num"] or 0),
        "model": task_model,
        "taskModel": task_model,
        "tokensBefore": int(row["tokens_before"] or 0),
        "tokensAfter": int(row["tokens_after"] or 0),
        "tokenCountKind": "estimated",
        "msgsBefore": int(row["msgs_before"] or 0),
        "msgsAfter": int(row["msgs_after"] or 0),
        "reason": str(row["reason"] or ""),
        "payloadSize": int(row["payload_size"] or 0),
        "payloadSizeUnit": "bytes",
        "summaryPreview": summary[:240],
        "hasSummary": bool(summary),
        "hasReceipt": bool(receipt),
        "resultStatus": str(receipt.get("status") or "legacy"),
        "resultStrategy": str(receipt.get("strategy") or ""),
    }


def _archive_create(session: Session, payload: Mapping[str, Any]) -> Any:
    conversation_id, user_id = _identity(payload)
    archive_id = _required_text(payload, "archive_id", 128)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise StorageError(
            "database_protocol_error", "Archive messages must be a list"
        )
    _require_owned_conversation(session, conversation_id, user_id)
    encoded_messages = _dump(messages)
    encoded_receipt = _encoded_receipt(payload)
    created_at_ms = _integer(
        payload, "created_at_ms", default=int(time.time() * 1000), minimum=0
    )
    values = (
        archive_id,
        conversation_id,
        user_id,
        encoded_messages,
        _text(payload, "summary", maximum=200_000),
        encoded_receipt,
        _text(payload, "trigger", maximum=32) or "force",
        _text(payload, "task_id", maximum=512),
        _integer(payload, "round_num", default=0, minimum=0),
        _text(payload, "model", maximum=256),
        _integer(payload, "tokens_before", default=0, minimum=0),
        _integer(payload, "tokens_after", default=0, minimum=0),
        _integer(payload, "msgs_before", default=0, minimum=0),
        _integer(payload, "msgs_after", default=0, minimum=0),
        _text(payload, "reason", maximum=500),
        len(encoded_messages),
        created_at_ms,
    )
    inserted = session.execute(
        "INSERT INTO storage_compaction_archives("
        "archive_id,conversation_id,user_id,messages_json,summary,receipt_json,trigger,"
        "task_id,round_num,model,tokens_before,tokens_after,msgs_before,"
        "msgs_after,reason,payload_size,created_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(archive_id) DO NOTHING",
        values,
    )
    if not inserted:
        existing = session.fetch_one(
            "SELECT conversation_id,user_id,messages_json FROM "
            "storage_compaction_archives WHERE archive_id=?",
            (archive_id,),
        )
        if (
            existing is None
            or str(existing["conversation_id"]) != conversation_id
            or int(existing["user_id"]) != user_id
            or _dump(_load(existing["messages_json"])) != encoded_messages
        ):
            raise StorageError(
                "database_conflict", "Archive id has a conflicting payload"
            )
    return {"created": bool(inserted), "archiveId": archive_id}


def _archive_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conversation_id, user_id = _identity(payload)
    _require_owned_conversation(session, conversation_id, user_id)
    limit = _integer(payload, "limit", default=200, minimum=1, maximum=1000)
    rows = session.fetch_all(
        "SELECT archive_id,conversation_id,created_at_ms,trigger,task_id,"
        "round_num,model,tokens_before,tokens_after,msgs_before,msgs_after,"
        "reason,payload_size,SUBSTR(summary,1,240) AS summary,receipt_json "
        "FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=? "
        "ORDER BY created_at_ms,archive_id LIMIT ?",
        (conversation_id, user_id, limit),
    )
    return {"archives": [_metadata(row) for row in rows]}


def _archive_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conversation_id, user_id = _identity(payload)
    archive_id = _required_text(payload, "archive_id", 128)
    include_messages = payload.get("include_messages", True)
    if not isinstance(include_messages, bool):
        raise StorageError(
            "database_protocol_error",
            "Invalid include_messages in storage request",
        )
    _require_owned_conversation(session, conversation_id, user_id)
    selected_columns = (
        "*" if include_messages else
        "archive_id,conversation_id,user_id,summary,trigger,task_id,round_num,"
        "model,tokens_before,tokens_after,msgs_before,msgs_after,reason,"
        "payload_size,created_at_ms,receipt_json"
    )
    row = session.fetch_one(
        f"SELECT {selected_columns} FROM storage_compaction_archives WHERE archive_id=? "
        "AND conversation_id=? AND user_id=?",
        (archive_id, conversation_id, user_id),
    )
    if row is None:
        return None
    archive = {
        **_metadata(row),
        "summary": str(row["summary"] or ""),
        "receipt": _decoded_receipt(row),
        "messagesCount": int(row["msgs_before"] or 0),
    }
    if not include_messages:
        return {"archive": archive}
    messages = _load(row["messages_json"])
    if not isinstance(messages, list):
        raise StorageError("database_integrity", "Archive messages are malformed")
    archive["messagesCount"] = len(messages)
    return {
        "archive": {
            **archive,
        },
        "messages": messages,
    }


def _archive_update_summary(session: Session, payload: Mapping[str, Any]) -> Any:
    archive_id = _required_text(payload, "archive_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    active = session.fetch_one(
        "SELECT 1 AS present FROM storage_compaction_archives AS a "
        "JOIN storage_conversations AS c "
        "ON c.id=a.conversation_id AND c.user_id=a.user_id "
        "WHERE a.archive_id=? AND a.user_id=?",
        (archive_id, user_id),
    )
    if active is None:
        return {"updated": False}
    assignments = ["summary=?", "tokens_after=?", "msgs_after=?"]
    values: list[Any] = [
        _text(payload, "summary", maximum=200_000),
        _integer(payload, "tokens_after", minimum=0),
        _integer(payload, "msgs_after", minimum=0),
    ]
    if "receipt" in payload:
        assignments.append("receipt_json=?")
        values.append(_encoded_receipt(payload))
    values.extend((archive_id, user_id))
    updated = session.execute(
        "UPDATE storage_compaction_archives SET " + ",".join(assignments)
        + " WHERE archive_id=? AND user_id=?",
        tuple(values),
    )
    return {"updated": bool(updated)}


def _archive_delete_conversation(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    conversation_id, user_id = _identity(payload)
    deleted = session.execute(
        "DELETE FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=?",
        (conversation_id, user_id),
    )
    return {"deleted": int(deleted or 0)}


def _archive_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    conversation_id, user_id = _identity(payload)
    keep = _integer(payload, "keep", minimum=1, maximum=1000)
    rows = session.fetch_all(
        "SELECT archive_id FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=? "
        "ORDER BY created_at_ms DESC,archive_id DESC",
        (conversation_id, user_id),
    )
    stale_ids = [str(row["archive_id"]) for row in rows[keep:]]
    for archive_id in stale_ids:
        session.execute(
            "DELETE FROM storage_compaction_archives WHERE archive_id=? "
            "AND conversation_id=? AND user_id=?",
            (archive_id, conversation_id, user_id),
        )
    return {"deleted": len(stale_ids)}


__all__ = [
    "_archive_create",
    "_archive_list",
    "_archive_get",
    "_archive_update_summary",
    "_archive_delete_conversation",
    "_archive_prune",
]

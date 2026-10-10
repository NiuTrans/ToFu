"""Per-conversation queue and autopilot-marker operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import time
from typing import Any


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.turn_source_queue_contract import (
    KIND_AUTOPILOT,
    KIND_BACKGROUND_RECEIPT,
    BACKGROUND_RECEIPT_BATCH_MAX,
    KIND_GOAL_CONTINUATION,
    KIND_REAL,
    KIND_PEER_MSG,
    QUEUE_INJECTION_ACK_MAX_IDS,
    KIND_WORKFLOW,
    QUEUE_REAP_PROBE_CONTRACT,
    QUEUE_REAP_PROBE_CONVERSATIONS_FIELD,
    QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD,
    QUEUE_REAP_PROBE_REQUEST_FIELD,
    QUEUE_REAP_PROBE_RESPONSE_FIELD,
    TURN_SOURCE_KINDS,
)


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._queue_shared import (
    renumber_queue_positions as _queue_renumber,
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


def _queue_kind(payload: Mapping[str, Any]) -> str:
    kind = payload.get("kind", KIND_REAL)
    if not isinstance(kind, str) or kind not in TURN_SOURCE_KINDS:
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
        "kind": str(row["kind"] or KIND_REAL),
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
        "hasAttachments": bool(payload.get("attachments")),
        "hasRefs": bool(payload.get("convRefs")),
        "hasQuotes": bool(payload.get("replyQuotes")),
        "payload": payload,
        "config": _load(row["config_json"]) or {},
    }
    for column_name, public_name in (
        ("input_turn_id", "inputTurnId"),
        ("output_turn_id", "outputTurnId"),
        ("attempt_id", "attemptId"),
    ):
        value = str(row.get(column_name) or "")
        if value:
            result[public_name] = value
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
    from lib.background_command_projection import background_command_completion
    completion = background_command_completion(payload)
    if completion is not None:
        result["backgroundCommand"] = completion
    if payload.get("_steerFallback"):
        result["steerFallback"] = True
    return result


def _queue_rows(
    session: Session, conv_id: str, user_id: int,
) -> list[Mapping[str, Any]]:
    return session.fetch_all(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, "
        "priority, created_at_ms, leased_until_ms, lease_task_id, "
        "input_turn_id, output_turn_id, attempt_id "
        "FROM storage_queue_items WHERE conv_id = ? AND user_id = ? "
        "ORDER BY priority, position",
        (conv_id, user_id),
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
    if kind == KIND_REAL:
        # A later human command permanently supersedes a pending synthetic
        # continuation. Priority alone is insufficient: replaying the stale
        # row after the human turn would restore its old stamped objective.
        deleted = session.execute(
            "DELETE FROM storage_queue_items "
            "WHERE conv_id = ? AND user_id = ? AND kind = ?",
            (conv_id, user_id, KIND_GOAL_CONTINUATION),
        )
        if deleted:
            _queue_renumber(session, conv_id, user_id)
            rows = _queue_rows(session, conv_id, user_id)
    if kind in {KIND_AUTOPILOT, KIND_GOAL_CONTINUATION}:
        for row in rows:
            if row["kind"] == kind:
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
        "priority, created_at_ms, leased_until_ms, lease_task_id, "
        "input_turn_id, output_turn_id, attempt_id "
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
    mode = payload.get('mode', 'full')
    if mode not in ('full', 'preview', 'dispatchable', 'background_pending', 'background_page'):
        raise StorageError('database_protocol_error', 'Invalid queue list mode')
    paged = mode == 'background_page'
    page_size = payload.get('page_size', BACKGROUND_RECEIPT_BATCH_MAX)
    max_bytes = payload.get('max_bytes', 8 * 1024 * 1024)
    cursor = payload.get('cursor')
    if (type(page_size) is not int or not 1 <= page_size <= BACKGROUND_RECEIPT_BATCH_MAX
            or type(max_bytes) is not int or not 1024 <= max_bytes <= 8 * 1024 * 1024
            or (cursor is not None and (not isinstance(cursor, Mapping)
                or any(type(cursor.get(key)) is not int or not 0 <= cursor[key] < 2 ** 64
                       for key in ('priority', 'createdAt'))
                or not isinstance(cursor.get('queueId'), str) or not 1 <= len(cursor['queueId'].encode('utf-8')) <= 256))):
        raise StorageError('database_protocol_error', 'Invalid queue page bounds or cursor')
    result = []
    page_bytes = 0
    last = None
    more = False
    rows = _queue_rows(session, conv_id, user_id)
    if paged:
        rows.sort(key=lambda row: (row['priority'], row['created_at_ms'], row['id']))
    for row in rows:
        if paged and cursor is not None and (row['priority'], row['created_at_ms'], row['id']) <= (cursor['priority'], cursor['createdAt'], cursor['queueId']):
            continue
        if mode in ('background_pending', 'background_page'):
            if row['kind'] not in (KIND_BACKGROUND_RECEIPT, KIND_WORKFLOW):
                continue
            if row.get('attempt_id'):
                continue
            message = _load(row['payload_json']) or {}
            if not isinstance(message, Mapping) or not message.get('_backgroundCommand'):
                continue
        elif mode == 'dispatchable':
            if row['kind'] in (KIND_AUTOPILOT, KIND_BACKGROUND_RECEIPT):
                continue
            if row['kind'] == KIND_WORKFLOW:
                message = _load(row['payload_json']) or {}
                if isinstance(message, Mapping) and message.get('_backgroundCommand'):
                    continue
        item = _queue_item(row)
        if mode == 'preview':
            item.pop('payload', None)
            item.pop('config', None)
        if paged:
            row_bytes = len(json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) + bool(result)
            next_cursor = {'priority': row['priority'], 'createdAt': row['created_at_ms'], 'queueId': row['id']}
            overhead = len(json.dumps({'items': [], 'nextCursor': next_cursor}, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            if len(result) >= page_size or page_bytes + row_bytes + overhead > max_bytes:
                if not result:
                    raise StorageError('database_resource_exhausted', 'Queue page budget cannot hold one complete item')
                more = True
                break
            page_bytes += row_bytes
            last = next_cursor
        result.append(item)
        if mode == 'background_pending' and len(result) >= BACKGROUND_RECEIPT_BATCH_MAX:
            break
    return {'items': result, 'nextCursor': last if more else None} if paged else result


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


def _queue_injection_ack(session: Session, payload: Mapping[str, Any]) -> Any:
    """Acknowledge at most one inbox drain; never cancel accepted Turn pairs."""
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    ids = payload.get("queue_ids")
    if not isinstance(ids, list) or len(ids) > QUEUE_INJECTION_ACK_MAX_IDS:
        raise StorageError("database_protocol_error", "Invalid injection queue IDs")
    ids = sorted(set(_required_text({"id": value}, "id", 256) for value in ids))
    if not ids:
        return {"removed": 0, "removedQueueIds": []}
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    placeholders = ",".join("?" for _ in ids)
    rows = session.fetch_all(
        "SELECT id, kind, payload_json FROM storage_queue_items "
        "WHERE conv_id=? AND user_id=? "
        f"AND id IN ({placeholders}) "
        "AND COALESCE(input_turn_id,'')='' "
        "AND COALESCE(output_turn_id,'')='' AND COALESCE(attempt_id,'')='' "
        "AND kind IN (?,?,?) ORDER BY position, id",
        (conv_id, user_id, *ids, KIND_PEER_MSG, KIND_WORKFLOW,
         KIND_BACKGROUND_RECEIPT),
    )
    removed_ids = []
    for row in rows:
        message = _load(row["payload_json"]) or {}
        if not isinstance(message, Mapping) or message.get("_peerHuman"):
            continue
        completion_id = message.get("_backgroundCommand")
        if (row["kind"] == KIND_PEER_MSG and message.get("_peerMessage")) or (
            row["kind"] in (KIND_WORKFLOW, KIND_BACKGROUND_RECEIPT)
            and isinstance(completion_id, str) and completion_id
        ):
            removed_ids.append(str(row["id"]))
    if removed_ids:
        placeholders = ",".join("?" for _ in removed_ids)
        session.execute(
            "DELETE FROM storage_queue_items WHERE conv_id=? AND user_id=? "
            f"AND id IN ({placeholders})",
            (conv_id, user_id, *removed_ids),
        )
        _queue_renumber(session, conv_id, user_id)
    return {"removed": len(removed_ids), "removedQueueIds": removed_ids}


def acknowledge_background_command_receipt(
    session: Session, *, conv_id: str, user_id: int,
    acknowledgement: Any, projection: Mapping[str, Any],
) -> list[str]:
    """Validate receipt/queue identity, then remove in the caller's transaction.

    Missing rows are safe on lost-ACK replay. Present protected or mismatched
    rows fail closed instead of publishing a receipt beside pending intent.
    No independent sync event is emitted: the Turn event carries the removals.
    """
    if not isinstance(acknowledgement, Mapping):
        raise StorageError('database_protocol_error', 'Invalid background acknowledgement')
    block_id = _required_text(acknowledgement, 'block_id', 256)
    deliveries = acknowledgement.get('deliveries')
    if not isinstance(deliveries, list) or len(deliveries) > QUEUE_INJECTION_ACK_MAX_IDS:
        raise StorageError('database_protocol_error', 'Invalid background delivery batch')
    identities = {}
    for delivery in deliveries:
        if not isinstance(delivery, Mapping):
            raise StorageError('database_protocol_error', 'Invalid background delivery identity')
        queue_id = _required_text(delivery, 'queue_id', 256)
        command_id = _required_text(delivery, 'command_id', 128)
        if queue_id in identities:
            raise StorageError('database_protocol_error', 'Duplicate background delivery identity')
        identities[queue_id] = command_id
    receipts = [item for item in projection.get('_bgCommandInjects', [])
                if isinstance(item, Mapping) and item.get('blockId') == block_id]
    if len(receipts) != 1 or not isinstance(receipts[0].get('previews'), list):
        raise StorageError('database_protocol_error', 'Background acknowledgement requires its receipt')
    previews = receipts[0]['previews']
    if len(previews) > QUEUE_INJECTION_ACK_MAX_IDS:
        raise StorageError('database_protocol_error', 'Background receipt exceeds inbox capacity')
    receipt_ids = {}
    for preview in previews:
        if not isinstance(preview, Mapping):
            raise StorageError('database_protocol_error', 'Invalid background receipt preview')
        if 'queueId' in preview:
            queue_id = _required_text(preview, 'queueId', 256)
            if queue_id in receipt_ids:
                raise StorageError('database_protocol_error', 'Duplicate background receipt identity')
            receipt_ids[queue_id] = _required_text(preview, 'commandId', 128)
    if identities != receipt_ids:
        raise StorageError('database_protocol_error', 'Background receipt identities disagree')
    if not identities:
        return []  # Legacy/headless evidence has no delivery intent to remove.
    session.lock_key('queue-conversation', f'{user_id}:{conv_id}')
    placeholders = ','.join('?' for _ in identities)
    rows = session.fetch_all(
        'SELECT id,kind,payload_json,input_turn_id,output_turn_id,attempt_id '
        'FROM storage_queue_items WHERE conv_id=? AND user_id=? '
        f'AND id IN ({placeholders})', (conv_id, user_id, *identities))
    for row in rows:
        message = _load(row['payload_json']) or {}
        if (row['kind'] not in (KIND_WORKFLOW, KIND_BACKGROUND_RECEIPT)
                or not isinstance(message, Mapping)
                or message.get('_peerHuman')
                or message.get('_backgroundCommand') != identities[row['id']]
                or any(row[key] for key in ('input_turn_id', 'output_turn_id', 'attempt_id'))):
            raise StorageError('database_conflict', 'Background delivery intent no longer matches receipt')
    result = _queue_injection_ack(session, {
        'conv_id': conv_id, 'user_id': user_id, 'queue_ids': list(identities),
    })
    return result['removedQueueIds']


def _queue_injection_ack_capture(session, _operation_name, payload, result):
    """Publish only actual removals in the same transaction as the batch."""
    from lib.storage_sidecar.operations_pkg._turns_core import (
        _append_conversation_change, _committed_event_notice,
        _committed_events_result,
    )

    if not result["removedQueueIds"]:
        return result
    user_id = _queue_owner(payload)
    event = _append_conversation_change(
        session, conversation_id=_queue_conv_id(payload), user_id=user_id,
        change_type="turn.upsert",
        payload={"turns": [], "attempts": [],
                 "removedQueueIds": result["removedQueueIds"]},
    )
    return _committed_events_result(result, [_committed_event_notice(event, user_id)])


def _queue_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_queue_items WHERE conv_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return {"cleared": max(0, int(deleted or 0))}


def _queue_kind_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    """Delete one owner-scoped source kind without touching other intent."""
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    kind = _queue_kind(payload)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    deleted = session.execute(
        "DELETE FROM storage_queue_items "
        "WHERE conv_id = ? AND user_id = ? AND kind = ?",
        (conv_id, user_id, kind),
    )
    if deleted:
        _queue_renumber(session, conv_id, user_id)
    return {"cleared": max(0, int(deleted or 0))}


def _queue_dequeue(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _queue_conv_id(payload)
    user_id = _queue_owner(payload)
    now_ms = _integer(payload, "now_ms", minimum=0)
    lease_ms = _integer(payload, "lease_ms", minimum=1, maximum=3600 * 1000)
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    rows = _queue_rows(session, conv_id, user_id)
    row = next((candidate for candidate in rows
                if candidate['kind'] not in (KIND_AUTOPILOT,
                                              KIND_BACKGROUND_RECEIPT)
                and not (candidate['kind'] == KIND_WORKFLOW
                         and (_load(candidate['payload_json']) or {}).get(
                             '_backgroundCommand'))
                and (candidate['leased_until_ms'] is None
                     or int(candidate['leased_until_ms']) < now_ms)), None)
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
        "WHERE conv_id = ? AND user_id = ? "
        "AND kind NOT IN ('autopilot', 'background_receipt')",
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
    reap_probe_contract = payload.get(QUEUE_REAP_PROBE_REQUEST_FIELD)
    if reap_probe_contract not in (None, QUEUE_REAP_PROBE_CONTRACT):
        raise StorageError(
            "database_protocol_error", "Invalid queue reap probe contract",
        )
    kind = payload.get("kind")
    if reap_probe_contract is not None:
        if kind is not None:
            raise StorageError(
                "database_protocol_error",
                "Queue reap probe cannot filter one source kind",
            )
        now_ms = _integer(payload, "now_ms", minimum=0)
        rows = session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at, "
            "MAX(CASE WHEN leased_until_ms IS NOT NULL "
            "AND leased_until_ms < ? THEN 1 ELSE 0 END) AS has_expired_lease "
            "FROM storage_queue_items WHERE kind NOT IN "
            "('autopilot', 'background_receipt') "
            "GROUP BY user_id, conv_id "
            "ORDER BY oldest_created_at, user_id, conv_id",
            (now_ms,),
        )
        conversations = [
            {"userId": int(row["user_id"]), "convId": str(row["conv_id"])}
            for row in rows if row["conv_id"]
        ]
        return {
            QUEUE_REAP_PROBE_RESPONSE_FIELD: QUEUE_REAP_PROBE_CONTRACT,
            QUEUE_REAP_PROBE_CONVERSATIONS_FIELD: conversations,
            QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD: any(
                bool(row["has_expired_lease"]) for row in rows
            ),
        }
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
            "FROM storage_queue_items WHERE kind NOT IN "
            "('autopilot', 'background_receipt') "
            "GROUP BY user_id, conv_id "
            "ORDER BY oldest_created_at, user_id, conv_id"
        )
    return [
        {"userId": int(row["user_id"]), "convId": str(row["conv_id"])}
        for row in rows if row["conv_id"]
    ]

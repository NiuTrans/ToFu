"""Turn, attempt, projection, and visible-sync operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
from collections import defaultdict
import threading
import time
import uuid
from typing import Any


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.turn_projection_patch import (
    build_projection_patch,
    normalize_projection_document,
)
from lib.turn_verdict import normalize_turn_settlement


logger = get_logger(__name__)

# Non-terminal attempt events are a replay transport, never a second full
# projection authority. A structural bug previously wrote the cumulative
# projection on every SSE delta and grew this table by hundreds of gigabytes.
# Keep this limit code-owned (not environment-tunable) so deployments cannot
# accidentally disable the safety boundary. Terminal settlement remains
# exempt because it is a bounded once-per-attempt recovery anchor.
_ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES = 4 * 1024 * 1024
_attempt_event_metrics_lock = threading.Lock()
_attempt_event_metrics: dict[str, dict[str, int]] = defaultdict(
    lambda: {
        "accepted_events": 0,
        "accepted_payload_bytes": 0,
        "max_accepted_payload_bytes": 0,
        "rejected_events": 0,
        "rejected_payload_bytes": 0,
        "max_rejected_payload_bytes": 0,
    }
)


def _observe_attempt_event_payload(
    event_type: str, payload_bytes: int, *, accepted: bool
) -> None:
    prefix = "accepted" if accepted else "rejected"
    with _attempt_event_metrics_lock:
        metrics = _attempt_event_metrics[event_type]
        metrics[f"{prefix}_events"] += 1
        metrics[f"{prefix}_payload_bytes"] += payload_bytes
        maximum_key = f"max_{prefix}_payload_bytes"
        metrics[maximum_key] = max(metrics[maximum_key], payload_bytes)


def attempt_event_write_metrics() -> dict[str, Any]:
    """Return process-lifetime counters suitable for scrape-based growth alerts."""
    with _attempt_event_metrics_lock:
        by_type = {
            event_type: dict(values)
            for event_type, values in sorted(_attempt_event_metrics.items())
        }
    return {
        "max_nonterminal_payload_bytes": _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES,
        "by_type": by_type,
    }

_CONVERSATION_SYNC_EVENT_CONTRACT = "tofu.conversation-sync.event/v1"
_STORAGE_COMMITTED_EVENTS_CONTRACT = "storage.committed-events/v1"
_SYNC_PRIVATE_SETTING_KEYS = frozenset()
_TURN_CHANGE_CAPTURE_OPERATIONS = frozenset({
    "turn.append_settled",
    "turn.attempt.bind",
    "turn.attempt.create",
    "turn.attempt.dispatch_worker",
    "turn.branch.create",
    "turn.branch.delete",
    "turn.compact",
    "turn.create_pair",
    "turn.delete",
    "turn.event.record",
    "turn.projection.update",
    "turn.recover",
    "turn.related.announce",
    "turn.visible.sync",
})


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._records import (
    _append_event_row,
)


def _turn_identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    conv_id = _required_text(payload, "conversation_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    return conv_id, user_id


def _turn_exists(session: Session, payload: Mapping[str, Any]) -> bool:
    """Return a cheap authority witness without loading projections."""
    conv_id, user_id = _turn_identity(payload)
    return session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? LIMIT 1",
        (conv_id, user_id),
    ) is not None


def _conversation_sync_head(
    session: Session,
    conversation_id: str,
    user_id: int,
) -> int:
    row = session.fetch_one(
        "SELECT sync_sequence FROM storage_conversation_sync_heads "
        "WHERE conversation_id=? AND user_id=?",
        (conversation_id, user_id),
    )
    return int(row["sync_sequence"] or 0) if row is not None else 0


def _append_conversation_change(
    session: Session,
    *,
    conversation_id: str,
    user_id: int,
    change_type: str,
    payload: Mapping[str, Any],
    turn_id: str = "",
    attempt_id: str = "",
    occurred_at: int | None = None,
) -> dict[str, Any]:
    """Append one ordered sync event in the caller's mutation transaction."""
    session.lock_key("conversation_sync", f"{user_id}:{conversation_id}")
    now = int(occurred_at or time.time() * 1000)
    sequence = _conversation_sync_head(session, conversation_id, user_id) + 1
    event: dict[str, Any] = {
        "contract": _CONVERSATION_SYNC_EVENT_CONTRACT,
        "type": change_type,
        "conversationId": conversation_id,
        "syncSeq": sequence,
        "occurredAt": now,
        "payload": dict(payload),
    }
    if turn_id:
        event["turnId"] = turn_id
    if attempt_id:
        event["attemptId"] = attempt_id
    if sequence == 1:
        session.execute(
            "INSERT INTO storage_conversation_sync_heads "
            "(conversation_id,user_id,sync_sequence,updated_at) VALUES (?,?,?,?)",
            (conversation_id, user_id, sequence, now),
        )
    else:
        updated = session.execute(
            "UPDATE storage_conversation_sync_heads "
            "SET sync_sequence=?,updated_at=? "
            "WHERE conversation_id=? AND user_id=? AND sync_sequence=?",
            (sequence, now, conversation_id, user_id, sequence - 1),
        )
        if updated != 1:
            raise StorageError(
                "database_conflict", "Conversation sync sequence changed concurrently")
    session.execute(
        "INSERT INTO storage_conversation_changes "
        "(conversation_id,user_id,sync_sequence,change_type,turn_id,attempt_id,event_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            conversation_id,
            user_id,
            sequence,
            change_type,
            turn_id,
            attempt_id,
            _dump(event),
            now,
        ),
    )
    return event


def _conversation_owner_for_turn(
    session: Session,
    conversation_id: str,
    turn_id: str,
) -> int:
    """Resolve ownership from authority rows, never from an event producer."""
    row = session.fetch_one(
        "SELECT user_id FROM storage_conversation_turns "
        "WHERE conversation_id=? AND turn_id=?",
        (conversation_id, turn_id),
    )
    if row is None:
        raise StorageError(
            "database_protocol_error",
            "Conversation change references an unknown turn",
        )
    return int(row["user_id"])


def _committed_events_result(value: Any, events: list[dict[str, Any]]) -> Any:
    """Carry post-commit wakeups to the RPC client without changing value shape.

    The sidecar serializes this envelope only on its private loopback protocol.
    ``StorageClient`` unwraps ``value`` before domain callers see it and emits
    ``events`` only after the command ACK proves the transaction committed.
    """
    if not events:
        return value
    return {
        "_storageCommitContract": _STORAGE_COMMITTED_EVENTS_CONTRACT,
        "value": value,
        "events": events,
    }


def _committed_event_notice(
    event: Mapping[str, Any], user_id: int
) -> dict[str, Any]:
    """Private post-commit carrier; user scope never pollutes public events."""
    return {
        "contract": "storage.conversation-commit/v1",
        "userId": int(user_id),
        "event": dict(event),
    }


def _turn_change_capture(
    session: Session,
    operation_name: str,
    payload: Mapping[str, Any],
    result: Any,
) -> Any:
    """Atomically project every user-visible turn command into the sync log.

    This callback is registered on every semantic turn mutation.  Keeping the
    policy at the operation boundary means a newly added command must declare
    its replay behavior in the catalog; routes and task workers cannot perform
    a durable mutation and then race a best-effort notification.
    """
    clean_result = result
    attempt_events: list[Mapping[str, Any]] = []
    turn_projection_changes: list[Mapping[str, Any]] = []
    if isinstance(result, Mapping):
        clean_result = dict(result)
        private_events = clean_result.pop("_conversationSyncAttemptEvents", [])
        private_turn_change = clean_result.pop("_conversationSyncTurnPatch", None)
        if isinstance(private_turn_change, Mapping):
            turn_projection_changes.append(private_turn_change)
        private_turn_changes = clean_result.pop(
            "_conversationSyncTurnPatches", []
        )
        if isinstance(private_turn_changes, list):
            turn_projection_changes.extend(
                item for item in private_turn_changes if isinstance(item, Mapping)
            )
        if isinstance(private_events, list):
            attempt_events.extend(
                item for item in private_events if isinstance(item, Mapping)
            )
        if bool(clean_result.get("idempotentReplay")):
            return clean_result
        if clean_result.get("applied") is False:
            return clean_result

    committed: list[dict[str, Any]] = []

    if operation_name in {"turn.create_pair", "turn.append_settled"}:
        if not isinstance(clean_result, Mapping):
            raise StorageError(
                "database_protocol_error", "Turn command returned an invalid result"
            )
        turns = [
            item
            for item in (
                clean_result.get("submittedTurn"),
                clean_result.get("turn"),
            )
            if isinstance(item, Mapping)
        ]
        attempts = [
            item for item in (clean_result.get("attempt"),)
            if isinstance(item, Mapping)
        ]
        if not turns:
            raise StorageError(
                "database_protocol_error", "Turn command produced no turn"
            )
        conversation_id = str(turns[-1].get("conversationId") or "")
        turn_id = str(turns[-1].get("turnId") or "")
        user_id = _conversation_owner_for_turn(session, conversation_id, turn_id)
        event_payload: dict[str, Any] = {
            "turns": [dict(item) for item in turns],
            "attempts": [dict(item) for item in attempts],
            "conversationRevision": int(
                clean_result.get("conversationRevision") or 0
            ),
        }
        committed.append(_committed_event_notice(
            _append_conversation_change(
                session,
                conversation_id=conversation_id,
                user_id=user_id,
                change_type="turn.upsert",
                payload=event_payload,
                turn_id=turn_id,
                attempt_id=(
                    str(attempts[0].get("attemptId") or "") if attempts else ""
                ),
            ),
            user_id,
        ))

    elif operation_name == "turn.compact":
        if not isinstance(clean_result, Mapping) or not isinstance(
            clean_result.get("turn"), Mapping
        ):
            raise StorageError(
                "database_protocol_error", "Turn compaction produced no summary turn"
            )
        turn = dict(clean_result["turn"])
        conversation_id = str(turn.get("conversationId") or "")
        turn_id = str(turn.get("turnId") or "")
        user_id = _conversation_owner_for_turn(session, conversation_id, turn_id)
        # Compaction may delete many turns and patch one or more retained giant
        # projections. A compact replay event containing all those documents
        # would recreate the oversized-frame incident this operation fixes.
        # Peers receive one tiny, transactional invalidation and hydrate the
        # authoritative post-commit snapshot instead.
        committed.append(_committed_event_notice(
            _append_conversation_change(
                session,
                conversation_id=conversation_id,
                user_id=user_id,
                change_type="conversation.activity",
                payload={
                    "requiresSnapshot": True,
                    "conversationRevision": int(
                        clean_result.get("conversationRevision") or 0
                    ),
                },
                turn_id=turn_id,
            ),
            user_id,
        ))

    elif operation_name in {
        "turn.projection.update",
        "turn.branch.create",
        "turn.branch.delete",
    }:
        if not isinstance(clean_result, Mapping) or not isinstance(
            clean_result.get("turn"), Mapping
        ):
            raise StorageError(
                "database_protocol_error", "Turn mutation produced no turn"
            )
        turn = dict(clean_result["turn"])
        conversation_id = str(turn.get("conversationId") or "")
        turn_id = str(turn.get("turnId") or "")
        user_id = _conversation_owner_for_turn(session, conversation_id, turn_id)
        if not turn_projection_changes:
            raise StorageError(
                "database_protocol_error",
                "Turn projection mutation produced no compact replay patch",
            )
        change_payload: dict[str, Any] = {
            "turnPatches": [dict(item) for item in turn_projection_changes],
            "conversationRevision": int(
                clean_result.get("conversationRevision") or 0
            ),
        }
        deleted_ids = clean_result.get("deletedTurnIds")
        if isinstance(deleted_ids, list) and deleted_ids:
            change_payload["deletedTurnIds"] = [str(item) for item in deleted_ids]
        committed.append(_committed_event_notice(
            _append_conversation_change(
                session,
                conversation_id=conversation_id,
                user_id=user_id,
                change_type="turn.patch",
                payload=change_payload,
                turn_id=turn_id,
            ),
            user_id,
        ))

    elif operation_name == "turn.attempt.create":
        if (
            not isinstance(clean_result, Mapping)
            or not isinstance(clean_result.get("turn"), Mapping)
            or len(attempt_events) != 1
            or (
                payload.get("input_update") is not None
                and not turn_projection_changes
            )
        ):
            raise StorageError(
                "database_protocol_error",
                "Attempt creation produced incomplete compact replay state",
            )
        turn = dict(clean_result["turn"])
        conversation_id = str(turn.get("conversationId") or "")
        turn_id = str(turn.get("turnId") or "")
        user_id = _conversation_owner_for_turn(session, conversation_id, turn_id)
        # An atomic regenerate may also edit its submitted parent.  That
        # existing projection is retained as a revision-checked patch, never
        # as another full historic turn document.
        if turn_projection_changes:
            committed.append(_committed_event_notice(
                _append_conversation_change(
                    session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    change_type="turn.patch",
                    payload={
                        "turnPatches": [
                            dict(item) for item in turn_projection_changes
                        ],
                        "conversationRevision": int(
                            clean_result.get("conversationRevision") or 0
                        ),
                    },
                    turn_id=str(turn_projection_changes[0].get("turnId") or ""),
                ),
                user_id,
            ))
        deleted_ids = [
            str(item) for item in clean_result.get("deletedTurnIds") or [] if item
        ]
        if deleted_ids:
            committed.append(_committed_event_notice(
                _append_conversation_change(
                    session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    change_type="turn.deleted",
                    payload={
                        "deletedTurnIds": deleted_ids,
                        "conversationRevision": int(
                            clean_result.get("conversationRevision") or 0
                        ),
                    },
                ),
                user_id,
            ))

    elif operation_name == "turn.delete":
        if not isinstance(clean_result, Mapping):
            raise StorageError(
                "database_protocol_error", "Turn delete returned an invalid result"
            )
        conversation_id, user_id = _turn_identity(payload)
        deleted_ids = [
            str(item) for item in clean_result.get("deletedTurnIds") or [] if item
        ]
        if deleted_ids:
            committed.append(_committed_event_notice(
                _append_conversation_change(
                    session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    change_type="turn.deleted",
                    payload={
                        "deletedTurnIds": deleted_ids,
                        "conversationRevision": int(
                            clean_result.get("conversationRevision") or 0
                        ),
                    },
                ),
                user_id,
            ))

    elif operation_name not in {
        "turn.attempt.bind",
        "turn.attempt.dispatch_worker",
        "turn.event.record",
        "turn.recover",
        "turn.related.announce",
        "turn.visible.sync",
    }:
        raise StorageError(
            "database_protocol_error",
            f"Turn command {operation_name} has no conversation-sync projection",
        )

    for attempt_event in attempt_events:
        conversation_id = str(attempt_event.get("conversationId") or "")
        turn_id = str(attempt_event.get("turnId") or "")
        attempt_id = str(attempt_event.get("attemptId") or "")
        attempt_payload = attempt_event.get("payload")
        if (
            not isinstance(attempt_payload, Mapping)
            or not isinstance(attempt_payload.get("projectionPatch"), Mapping)
        ):
            raise StorageError(
                "database_protocol_error",
                "Attempt change produced no revision-checked projection patch",
            )
        user_id = _conversation_owner_for_turn(session, conversation_id, turn_id)
        committed.append(_committed_event_notice(
            _append_conversation_change(
                session,
                conversation_id=conversation_id,
                user_id=user_id,
                change_type="attempt.event",
                payload={"event": dict(attempt_event)},
                turn_id=turn_id,
                attempt_id=attempt_id,
            ),
            user_id,
        ))

    return _committed_events_result(clean_result, committed)


def _projection_change(
    *,
    turn_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    base_revision: int,
    target_revision: int,
    updated_at: int,
) -> dict[str, Any]:
    """Build the only retained wire form for an existing turn projection."""
    return {
        "turnId": str(turn_id),
        "baseProjectionRevision": int(base_revision),
        "targetProjectionRevision": int(target_revision),
        "updatedAt": int(updated_at),
        "projectionPatch": build_projection_patch(
            before,
            after,
            base_revision=base_revision,
            target_revision=target_revision,
        ),
    }


def _turn_public(row: Mapping[str, Any]) -> dict[str, Any]:
    stored_status = str(row["status"])
    raw_settlement = _load(row["settlement_json"]) or {}
    public_settlement = (
        normalize_turn_settlement(raw_settlement, status=stored_status)
        if stored_status in {"completed", "interrupted", "truncated", "failed", "superseded"}
        else raw_settlement
    )
    status = stored_status
    normalized_outcome = str(public_settlement.get("outcome") or "")
    if (stored_status in {"completed", "interrupted", "truncated", "failed", "superseded"}
            and normalized_outcome in {"completed", "interrupted", "truncated", "failed"}):
        status = normalized_outcome
    return {
        "turnId": str(row["turn_id"]),
        "conversationId": str(row["conversation_id"]),
        "laneId": str(row["lane_id"] or "main"),
        "parentTurnId": row["parent_turn_id"],
        "ordinal": int(row["ordinal"]),
        "actor": str(row["actor"]),
        "kind": str(row["kind"] or "reply"),
        "runId": str(row["run_id"] or ""),
        "status": status,
        "currentAttemptId": row["current_attempt_id"],
        "projection": _load(row["projection_json"]) or {},
        "projectionRevision": int(row["projection_revision"] or 0),
        "settlement": public_settlement,
        "createdAt": int(row["created_at"]),
        "updatedAt": int(row["updated_at"]),
    }


_TURN_SEARCH_UNSET = object()
_TURN_SEARCH_TEXT_MAX_BYTES = 10_000
_TURN_SEARCH_PROJECTION_NAME = "turn_search.v1"


def _mark_turn_search_projection_dirty(
    session: Session,
    *,
    entity_kind: str,
    user_id: int,
    entity_key: str,
) -> str:
    """Publish one idempotent dirty-set marker in the authority transaction.

    ``version_token`` is deliberately per-entity rather than a global
    sequence. A projection worker acknowledges only the token it read; if a
    newer mutation replaces the marker while materialization is in flight,
    the stale acknowledgement becomes a no-op and the entity is replayed.
    That gives SQLite and PostgreSQL the same crash/race semantics without a
    cross-tenant sequence hot spot.
    """
    if entity_kind not in {"turn", "conversation", "rebuild"}:
        raise StorageError(
            "database_internal", "Invalid turn-search projection entity"
        )
    key = str(entity_key or "")
    if not key or len(key) > 256:
        raise StorageError(
            "database_internal", "Invalid turn-search projection key"
        )
    token = uuid.uuid4().hex
    session.execute(
        "INSERT INTO storage_projection_outbox("
        "projection_name,entity_kind,user_id,entity_key,version_token,"
        "enqueued_at_ms) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(projection_name,entity_kind,user_id,entity_key) "
        "DO UPDATE SET version_token=excluded.version_token,"
        "enqueued_at_ms=excluded.enqueued_at_ms",
        (
            _TURN_SEARCH_PROJECTION_NAME,
            entity_kind,
            int(user_id),
            key,
            token,
            int(time.time() * 1000),
        ),
    )
    return token


def _mark_conversation_search_projection_dirty(
    session: Session, conversation_id: str, user_id: int,
) -> str:
    return _mark_turn_search_projection_dirty(
        session,
        entity_kind="conversation",
        user_id=int(user_id),
        entity_key=str(conversation_id),
    )


def _bounded_turn_search_text(value: str) -> str:
    """Return a UTF-8-safe fragment with a hard storage/wire byte ceiling."""
    encoded = value.encode("utf-8")
    if len(encoded) <= _TURN_SEARCH_TEXT_MAX_BYTES:
        return value
    return encoded[:_TURN_SEARCH_TEXT_MAX_BYTES].decode(
        "utf-8", errors="ignore")


def _turn_search_text(actor: str, projection: Mapping[str, Any]) -> str:
    """Build the searchable, human-readable fragment for one turn.

    Turn-native transcripts deliberately keep ``storage_conversations``'s
    legacy ``messages_json``/``search_text`` blobs frozen.  Rebuilding one
    conversation-sized aggregate on every terminal frame would make write
    cost grow with the whole history and recreate the writer-timeout incident.
    A turn-sized projection keeps the derived index proportional to the one
    authority row that changed.
    """
    try:
        from lib.conversations.search_index import build_search_text

        message = dict(projection)
        message["role"] = (
            "user"
            if actor in ("human", "critic", "virtual_user")
            else "assistant"
        )
        return _bounded_turn_search_text(str(build_search_text([message]) or ""))
    except Exception as exc:
        raise StorageError(
            "database_internal", "Turn search projection failed"
        ) from exc


def _upsert_turn_search_row(
    session: Session,
    row: Mapping[str, Any],
    *,
    projection: Any = _TURN_SEARCH_UNSET,
    projection_revision: int | None = None,
    status: str | None = None,
    updated_at: int | None = None,
) -> bool:
    """Mark one settled turn for independent search materialization.

    Live projections are intentionally skipped: legacy partial checkpoints
    also preserve the last settled search corpus until a terminal write.  A
    regenerate therefore keeps the prior settled result searchable while it
    runs, then replaces it exactly once on settlement. The marker is committed
    with the turn, while text extraction and index I/O happen outside the
    authority writer.
    """
    lane_id = str(row["lane_id"] or "main")
    turn_id = str(row["turn_id"])
    conversation_id = str(row["conversation_id"])
    if lane_id != "main":
        _mark_turn_search_projection_dirty(
            session,
            entity_kind="turn",
            user_id=int(row["user_id"]),
            entity_key=turn_id,
        )
        return False
    effective_status = str(status if status is not None else row["status"] or "")
    if effective_status in ("pending", "running"):
        return False
    # The parameters remain accepted because several mutation handlers already
    # have the updated projection in hand. Deliberately do not serialize that
    # document into the authority outbox: the worker reads the latest canonical
    # row after commit, so repeated updates collapse to one small marker.
    del projection, projection_revision, updated_at, conversation_id
    _mark_turn_search_projection_dirty(
        session,
        entity_kind="turn",
        user_id=int(row["user_id"]),
        entity_key=turn_id,
    )
    return True


def _delete_turn_search_rows(
    session: Session, rows: Mapping[str, Mapping[str, Any]]
) -> None:
    """Collapse an arbitrary turn deletion closure to conversation markers."""
    conversations = {
        (int(row["user_id"]), str(row["conversation_id"]))
        for row in rows.values()
    }
    for user_id, conversation_id in sorted(conversations):
        _mark_conversation_search_projection_dirty(
            session, conversation_id, user_id)


def _attempt_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attemptId": str(row["attempt_id"]),
        "conversationId": str(row["conversation_id"]),
        "turnId": str(row["turn_id"]),
        "commandId": str(row["command_id"]),
        "taskId": str(row["task_id"] or ""),
        "operation": str(row["operation"]),
        "status": str(row["status"]),
        "baseProjectionRevision": int(row["base_projection_revision"] or 0),
        "resumeAnchor": _load(row["resume_anchor_json"]) or {},
        "createdAt": int(row["created_at"]),
        "startedAt": row["started_at"],
        "settledAt": row["settled_at"],
    }


def _turn_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    if row is None:
        return None
    return _turn_public(row)


def _turn_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    lane_id = payload.get("lane_id")
    where = "conversation_id=? AND user_id=?"
    params: list[Any] = [conv_id, user_id]
    if lane_id:
        where += " AND lane_id=?"
        params.append(str(lane_id))
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns WHERE " + where + " ORDER BY ordinal",
        tuple(params),
    )
    return [_turn_public(row) for row in rows]


def _attempt_get(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    row = session.fetch_one(
        "SELECT a.* FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.attempt_id=? AND t.user_id=?",
        (attempt_id, user_id),
    )
    return None if row is None else _attempt_public(row)


def _turn_revision(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return int(row["rev"]) if row is not None else 0


# Delta-sync overlap window: a watermark-capturing read and the writer
# transaction it races are both covered as long as the writer's open
# transaction is shorter than this margin.  Inclusive ``>=`` filtering plus
# client-side (turnId, projectionRevision) dedupe makes the over-fetch
# harmless; a permanent miss requires a >OVERLAP open write tx straddling the
# watermark read, and even then self-heals at the next full hydrate.
_DELTA_OVERLAP_MS = 5000


# Tombstone retention: a delta client converges via tombstones only while its
# watermark is fresher than this horizon.  The watermark is in-memory client
# state (every page load re-anchors with a FULL snapshot), so a horizon far
# longer than any realistic single-page session keeps convergence exact while
# bounding the table.
_TOMBSTONE_RETENTION_MS = 7 * 24 * 60 * 60 * 1000


def _prune_turn_tombstones(session: Session, now: int) -> None:
    session.execute(
        "DELETE FROM storage_turn_tombstones WHERE deleted_at < ?",
        (now - _TOMBSTONE_RETENTION_MS,),
    )


def _turn_list_delta(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return only the turns changed since the client's watermark.

    The full turns list re-ships every projection on every resync — multi-MB
    per call for long conversations, and the conv_changed notify path used to
    trigger it per frame per tab (the measured resync storm).  This operation
    is the delta half of the sync protocol: rows are filtered by the
    single-writer ``updated_at`` clock with an overlap margin, and deletions
    ride the tombstone table so a delta client converges exactly like a full
    snapshot client.  ``serverNowMs`` is captured BEFORE the reads; combined
    with the overlap margin on the next request the watermark never skips a
    committed write.
    """
    conv_id, user_id = _turn_identity(payload)
    since_ms = _integer(payload, "since_ms", default=0, minimum=0)
    watermark = int(time.time() * 1000)
    lower = max(0, since_ms - _DELTA_OVERLAP_MS)
    raw_known_revisions = payload.get("known_revisions")
    known_revisions: dict[str, int] = {}
    if isinstance(raw_known_revisions, Mapping):
        # A client can hold at most the list endpoint's 2,000 turns.  Ignore
        # malformed entries rather than weakening the overlap safety window.
        for raw_turn_id, raw_revision in list(raw_known_revisions.items())[:2000]:
            try:
                known_revisions[str(raw_turn_id)] = max(0, int(raw_revision))
            except (TypeError, ValueError):
                continue
    if known_revisions:
        # The 5s overlap is intentionally retained to cover a writer whose
        # transaction straddles watermark capture.  Projection revision is the
        # exact client/server dedupe key: unchanged rows inside that overlap
        # must not re-ship — or even read — a multi-MB projection on every
        # retry. Select only lightweight revision columns first, then hydrate
        # the genuinely advanced rows in bounded, backend-portable IN chunks.
        revision_rows = session.fetch_all(
            "SELECT turn_id,projection_revision "
            "FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? AND updated_at>=?",
            (conv_id, user_id, lower),
        )
        changed_turn_ids = [
            str(row["turn_id"]) for row in revision_rows
            if int(row["projection_revision"] or 0)
            > known_revisions.get(str(row["turn_id"]), -1)
        ]
        rows = []
        for start in range(0, len(changed_turn_ids), 400):
            chunk = changed_turn_ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(session.fetch_all(
                "SELECT * FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? "
                f"AND turn_id IN ({placeholders})",
                (conv_id, user_id, *chunk),
            ))
        rows.sort(key=lambda row: int(row["ordinal"] or 0))
    else:
        rows = session.fetch_all(
            "SELECT * FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? AND updated_at>=? "
            "ORDER BY ordinal",
            (conv_id, user_id, lower),
        )
    tombstones = session.fetch_all(
        "SELECT turn_id FROM storage_turn_tombstones "
        "WHERE conversation_id=? AND user_id=? AND deleted_at>=?",
        (conv_id, user_id, lower),
    )
    return {
        "turns": [_turn_public(row) for row in rows],
        "deletedTurnIds": [str(row["turn_id"]) for row in tombstones],
        "serverNowMs": watermark,
    }


def _turn_events(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    after = _integer(payload, "after", default=0, minimum=0)
    limit = _integer(payload, "limit", default=1000, minimum=1, maximum=5000)
    owner = session.fetch_one(
        "SELECT 1 AS present FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.attempt_id=? AND t.user_id=?",
        (attempt_id, user_id),
    )
    if owner is None:
        return None
    rows = session.fetch_all(
        "SELECT payload_json FROM storage_attempt_events "
        "WHERE attempt_id=? AND sequence>? ORDER BY sequence LIMIT ?",
        (attempt_id, after, limit),
    )
    events = [_load(row["payload_json"]) or {} for row in rows]
    # Compatibility default: older clients understand only a full
    # ``payload.projection`` and receive one on the page tail.  New clients ask
    # for patch mode and replay the compact durable projectionPatch directly.
    if str(payload.get("projection_mode") or "full") != "patch":
        _hydrate_slim_frame_tail(session, attempt_id, events)
    return events


_SLIM_HYDRATABLE_TYPES = frozenset({
    "projection_updated", "interaction_request", "terminal_settlement",
})


def _hydrate_slim_frame_tail(
    session: Session, attempt_id: str, events: list[dict]
) -> None:
    """Attach the turn authority's current projection to the page's tail.

    Slim frames persist no projection copy (the turn row is the single
    authority).  A catching-up client folds every returned frame in order and
    only the LATEST projection affects its rendered state
    (frontend/src/core/turn-state.ts folds `payload.projection` when present
    and keeps the prior one otherwise), so hydrating exactly the last
    projection-bearing frame reproduces the fat-stream terminal state with a
    fraction of the read amplification.  Hydration is skipped when the
    attempt no longer owns the turn (superseded): the turn row's projection
    belongs to the newer attempt and must not be misattributed.
    """
    hydrate_index = None
    for index in range(len(events) - 1, -1, -1):
        envelope = events[index]
        body = envelope.get("payload")
        if (
            isinstance(body, dict)
            and envelope.get("type") in _SLIM_HYDRATABLE_TYPES
            and "projection" not in body
        ):
            hydrate_index = index
            break
    if hydrate_index is None:
        return
    turn_id = str(events[hydrate_index].get("turnId") or "")
    if not turn_id:
        return
    turn = session.fetch_one(
        "SELECT current_attempt_id, projection_json FROM "
        "storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    if turn is None or turn["current_attempt_id"] != attempt_id:
        return
    projection = _load(turn["projection_json"])
    if isinstance(projection, dict):
        events[hydrate_index]["payload"]["projection"] = projection


def _turn_events_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    """Bounded TTL deletion of settled attempts' transport event streams.

    ``storage_attempt_events`` is the SSE transport log: replay value ends
    with the reconnect window, while every frame historically carried a full
    projection copy (2026-08-20: 281 GiB / 71% of the authority).  The turn
    rows (``storage_conversation_turns``) are the permanent authority and
    keep final projections + settlements, so an OLD settled attempt's event
    stream is dead weight.  This op deletes it in bounded, resumable slices:

    • only attempts terminally settled before ``settled_before_ms`` are
      eligible — live attempts are never touched;
    • at most ``max_attempts`` attempts and ``max_rows`` rows per call, in
      settled-at order (oldest first); an attempt larger than the row budget
      is drained across calls (PK-windowed deletes);
    • ``remaining`` reports unfinished work so the caller loops until 0.
    """
    settled_before = _integer(payload, "settled_before_ms", minimum=1)
    max_attempts = _integer(payload, "max_attempts", default=16, minimum=1, maximum=256)
    max_rows = _integer(payload, "max_rows", default=4096, minimum=1, maximum=200_000)
    candidates = session.fetch_all(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a "
        "WHERE a.status NOT IN ('pending','running') "
        "AND a.settled_at IS NOT NULL AND a.settled_at < ? "
        "AND EXISTS (SELECT 1 FROM storage_attempt_events AS e "
        "            WHERE e.attempt_id=a.attempt_id) "
        "ORDER BY a.settled_at, a.attempt_id LIMIT ?",
        (settled_before, max_attempts + 1),
    )
    overflow = len(candidates) > max_attempts
    candidates = candidates[:max_attempts]
    deleted_rows = 0
    deleted_attempts = 0
    for candidate in candidates:
        if deleted_rows >= max_rows:
            break
        attempt_id = candidate["attempt_id"]
        max_seq_row = session.fetch_one(
            "SELECT COALESCE(MAX(sequence),0) AS n FROM "
            "storage_attempt_events WHERE attempt_id=?",
            (attempt_id,),
        )
        remaining_seq = int(max_seq_row["n"]) if max_seq_row else 0
        cursor = 0
        while cursor < remaining_seq and deleted_rows < max_rows:
            window = min(max_rows - deleted_rows, 1024)
            upper = cursor + window
            deleted_rows += session.execute(
                "DELETE FROM storage_attempt_events WHERE attempt_id=? "
                "AND sequence>? AND sequence<=?",
                (attempt_id, cursor, upper),
            )
            cursor = upper
        if cursor < remaining_seq:
            break
        deleted_attempts += 1
    # Progress hint for the caller loop: every candidate not fully drained
    # (including a budget-cut partial one) plus the overflow probe marker.
    remaining = (1 if overflow else 0) + (len(candidates) - deleted_attempts)
    return {
        "deleted_rows": deleted_rows,
        "deleted_attempts": deleted_attempts,
        "remaining": remaining,
    }


def _insert_attempt_event(
    session: Session,
    *,
    attempt_id: str,
    sequence: int,
    conversation_id: str,
    turn_id: str,
    projection_revision: int,
    event_type: str,
    envelope: Mapping[str, Any],
    created_at: int,
) -> int:
    """Insert one measured attempt event and enforce the transport budget."""
    encoded_envelope = _dump(envelope)
    payload_bytes = len(encoded_envelope)
    if (
        event_type != "terminal_settlement"
        and payload_bytes > _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES
    ):
        _observe_attempt_event_payload(event_type, payload_bytes, accepted=False)
        raise StorageError(
            "storage_payload_too_large",
            "Non-terminal attempt event exceeds the durable transport limit",
        )
    session.execute(
        "INSERT INTO storage_attempt_events "
        "(attempt_id,sequence,conversation_id,turn_id,projection_revision,type,"
        "payload_json,payload_bytes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            sequence,
            conversation_id,
            turn_id,
            projection_revision,
            event_type,
            encoded_envelope,
            payload_bytes,
            created_at,
        ),
    )
    _observe_attempt_event_payload(event_type, payload_bytes, accepted=True)
    return payload_bytes


def _turn_event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    conv_id = _required_text(payload, "conversation_id", 256)
    turn_id = _required_text(payload, "turn_id", 128)
    event_type = _required_text(payload, "type", 128)
    projection_revision = _integer(payload, "projection_revision", minimum=0)
    session.lock_key("attempt_events", attempt_id)
    current = session.fetch_one(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM storage_attempt_events WHERE attempt_id=?",
        (attempt_id,),
    )
    sequence = int(current["sequence"]) + 1
    # Wire-shape parity with legacy ``turn_lifecycle._append_event``: the
    #   event body MUST nest under ``payload``.  Both frontend turn stores
    #   (typed core/turn-state.ts and the inline app-runtime fallback) read
    #   ``event.payload.projection`` / ``.status`` / ``.settlement`` /
    #   ``.turns`` — the previous sidecar envelope spread the body at the top
    #   level, so under sidecar authority every live turn event parsed as
    #   ``payload=undefined``: projections, tool rounds, status transitions,
    #   settlements and continuation hops were all silently dropped, and the
    #   UI only ever caught up via a full conversation refetch (the
    #   "new tool calls need a refresh" report, 2026-08-18).
    envelope = {
        "conversationId": conv_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": sequence,
        "projectionRevision": projection_revision,
        "type": event_type,
        "payload": dict(payload.get("event") or {}),
    }
    payload_bytes = _insert_attempt_event(
        session,
        attempt_id=attempt_id,
        sequence=sequence,
        conversation_id=conv_id,
        turn_id=turn_id,
        projection_revision=projection_revision,
        event_type=event_type,
        envelope=envelope,
        created_at=int(time.time() * 1000),
    )
    return {
        "sequence": sequence,
        "event": envelope,
        "payloadBytes": payload_bytes,
    }


def _ensure_turn_conversation_header(
    session: Session, payload: Mapping[str, Any]
) -> tuple[Mapping[str, Any], int]:
    """Resolve or create one owner-scoped conversation header for a turn command."""
    conv_id, user_id = _turn_identity(payload)
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    raw_defaults = payload.get("conversation_defaults") or {}
    defaults = raw_defaults if isinstance(raw_defaults, Mapping) else {}
    now = int(payload.get("now") or time.time() * 1000)
    if conversation is None:
        if not bool(defaults.get("allowCreate")):
            raise StorageError("database_not_found", "Conversation not found")
        settings = (
            dict(defaults.get("settings"))
            if isinstance(defaults.get("settings"), Mapping)
            else {}
        )
        settings.pop("activeTaskId", None)
        session.execute(
            "INSERT INTO storage_conversations "
            "(id,user_id,title,messages_json,created_at_ms,updated_at_ms,settings_json,msg_count,search_text,rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
            (
                conv_id,
                user_id,
                str(defaults.get("title") or "New Chat")[:500],
                _dump([]),
                int(defaults.get("createdAt") or now),
                now,
                _dump(settings),
                "",
            ),
        )
        conversation = {"rev": 0}
    if isinstance(defaults.get("settings"), Mapping):
        current_row = session.fetch_one(
            "SELECT settings_json FROM storage_conversations "
            "WHERE id=? AND user_id=?",
            (conv_id, user_id),
        )
        current_settings = dict(_load(current_row["settings_json"]) or {})
        incoming_settings = dict(defaults["settings"])
        incoming_settings.pop("activeTaskId", None)
        current_settings.update(incoming_settings)
        session.execute(
            "UPDATE storage_conversations SET settings_json=?,updated_at_ms=? "
            "WHERE id=? AND user_id=?",
            (_dump(current_settings), now, conv_id, user_id),
        )
    return conversation, now


def _turn_create_pair(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    command_id = _required_text(payload, "command_id", 256)
    lane_id = str(payload.get("lane_id") or "main")
    input_actor = str(payload.get("input_actor") or "human")
    output_actor = str(payload.get("output_actor") or "assistant")
    if input_actor not in {"human", "virtual_user", "critic"} or output_actor not in {
        "assistant",
        "planner",
        "critic",
        "virtual_user",
    }:
        raise StorageError("database_protocol_error", "Invalid turn actor")
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    session.lock_key("turn_command", f"{user_id}:{conv_id}:{command_id}")
    existing = session.fetch_one(
        "SELECT a.attempt_id, a.turn_id FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.conversation_id=? AND a.command_id=? AND t.user_id=?",
        (conv_id, command_id, user_id),
    )
    if existing is not None:
        turn = session.fetch_one(
            "SELECT * FROM storage_conversation_turns "
            "WHERE turn_id=? AND user_id=?",
            (existing["turn_id"], user_id),
        )
        attempt = session.fetch_one(
            "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
            (existing["attempt_id"],),
        )
        submitted = (
            session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
                (turn["parent_turn_id"],),
            )
            if turn is not None and turn.get("parent_turn_id")
            else None
        )
        return {
            "submittedTurn": _turn_public(submitted) if submitted is not None else None,
            "turn": _turn_public(turn),
            "attempt": _attempt_public(attempt),
            "conversationRevision": _turn_revision(session, payload),
            "streamCursor": 1,
            "idempotentReplay": True,
            # A command can commit and lose its ACK before the route claims
            # dispatch.  Replaying that exact command must finish the launch;
            # claim_attempt_start remains the single-winner guard once a task
            # is already dispatching or bound.
            "_needsStart": attempt["status"] == "pending" and not attempt["task_id"],
        }
    conv, now = _ensure_turn_conversation_header(session, payload)

    parent_turn_id = payload.get("parent_turn_id")
    if parent_turn_id:
        parent = session.fetch_one(
            "SELECT * FROM storage_conversation_turns "
            "WHERE turn_id=? AND conversation_id=? AND user_id=?",
            (parent_turn_id, conv_id, user_id),
        )
        if parent is None:
            raise StorageError(
                "turn_parent_invalid", "Parent turn does not exist"
            )
    if input_actor == "human":
        live = session.fetch_one(
            "SELECT t.* FROM storage_conversation_turns AS t "
            "JOIN storage_generation_attempts AS a "
            "ON a.attempt_id=t.current_attempt_id "
            "WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? "
            "AND a.status IN ('pending','running') "
            "ORDER BY t.ordinal DESC LIMIT 1",
            (conv_id, user_id, lane_id),
        )
        if live is not None:
            raise StorageError(
                "turn_in_progress",
                "This lane already has a live generation attempt.",
            )
    if bool(payload.get("require_parent_is_lane_tail")):
        tail = session.fetch_one(
            "SELECT turn_id FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? AND lane_id=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (conv_id, user_id, lane_id),
        )
        if tail is None or tail["turn_id"] != parent_turn_id:
            raise StorageError(
                "turn_lane_advanced",
                "The lane advanced while the continuation was prepared.",
            )
    input_projection = payload.get("input_projection")
    if isinstance(input_projection, str):
        input_projection = {"content": input_projection}
    if not isinstance(input_projection, Mapping):
        input_projection = {"content": ""}
    input_projection = normalize_projection_document(input_projection)
    row = session.fetch_one(
        "SELECT COALESCE(MAX(ordinal), -1) AS ordinal FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id=?",
        (conv_id, user_id, lane_id),
    )
    input_turn_id = str(uuid.uuid4())
    output_turn_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    input_attempt_id = str(uuid.uuid4()) if input_actor != "human" else None
    submitted_settlement = {
        "outcome": "completed",
        "cause": "submitted" if input_actor == "human" else "orchestration_generated",
        "resumeOptions": [],
    }
    session.execute(
        "INSERT INTO storage_conversation_turns "
        "(turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            input_turn_id,
            conv_id,
            user_id,
            lane_id,
            parent_turn_id,
            int(row["ordinal"]) + 1,
            input_actor,
            str(payload.get("input_kind") or "input"),
            str(payload.get("run_id") or ""),
            "completed",
            input_attempt_id,
            _dump(input_projection),
            1,
            _dump(submitted_settlement),
            now,
            now,
        ),
    )
    if input_attempt_id:
        input_command_id = f"{command_id}:input"
        session.execute(
            "INSERT INTO storage_generation_attempts "
            "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,"
            "status,base_projection_revision,resume_anchor_json,config_json,"
            "error_json,created_at,started_at,settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                input_attempt_id,
                conv_id,
                input_turn_id,
                input_command_id,
                "",
                "generate",
                "completed",
                0,
                _dump({}),
                _dump({"runId": str(payload.get("run_id") or "")}),
                _dump({}),
                now,
                now,
                now,
            ),
        )
        input_event = {
            "conversationId": conv_id,
            "turnId": input_turn_id,
            "attemptId": input_attempt_id,
            "seq": 1,
            "projectionRevision": 1,
            "type": "terminal_settlement",
            "payload": {
                "status": "completed",
                "settlement": submitted_settlement,
                "projection": input_projection,
            },
        }
        _insert_attempt_event(
            session,
            attempt_id=input_attempt_id,
            sequence=1,
            conversation_id=conv_id,
            turn_id=input_turn_id,
            projection_revision=1,
            event_type="terminal_settlement",
            envelope=input_event,
            created_at=now,
        )
    session.execute(
        "INSERT INTO storage_conversation_turns "
        "(turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            output_turn_id,
            conv_id,
            user_id,
            lane_id,
            input_turn_id,
            int(row["ordinal"]) + 2,
            output_actor,
            str(payload.get("kind") or "reply"),
            str(payload.get("run_id") or ""),
            "pending",
            attempt_id,
            _dump({"content": "", "thinking": "", "segments": [], "toolRounds": []}),
            1,
            _dump({}),
            now,
            now,
        ),
    )
    session.execute(
        "INSERT INTO storage_generation_attempts "
        "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            conv_id,
            output_turn_id,
            command_id,
            "",
            "generate",
            "pending",
            0,
            _dump({}),
            _dump(payload.get("config") or {}),
            _dump({}),
            now,
        ),
    )
    event = {
        "conversationId": conv_id,
        "turnId": output_turn_id,
        "attemptId": attempt_id,
        "seq": 1,
        "projectionRevision": 1,
        "type": "status_changed",
        "payload": {"status": "pending"},
    }
    _insert_attempt_event(
        session,
        attempt_id=attempt_id,
        sequence=1,
        conversation_id=conv_id,
        turn_id=output_turn_id,
        projection_revision=1,
        event_type="status_changed",
        envelope=event,
        created_at=now,
    )
    revision = int(conv["rev"]) + 1
    session.execute(
        "UPDATE storage_conversations SET rev=?, updated_at_ms=? WHERE id=? AND user_id=?",
        (revision, now, conv_id, user_id),
    )
    input_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (input_turn_id,)
    )
    output_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (output_turn_id,)
    )
    attempt_row = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    _upsert_turn_search_row(session, input_row)
    _upsert_turn_search_row(session, output_row)
    return {
        "submittedTurn": _turn_public(input_row),
        "turn": _turn_public(output_row),
        "attempt": _attempt_public(attempt_row),
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": False,
        "_needsStart": True,
    }


def _turn_append_settled(session: Session, payload: Mapping[str, Any]) -> Any:
    """Append one already-settled turn to the canonical transcript.

    This is the semantic ingestion boundary for external channels, restores,
    and deterministic fixtures. It writes the same turn/attempt/search/sync
    records as live generation and never accepts a conversation-sized array.
    """
    conv_id, user_id = _turn_identity(payload)
    actor = _required_text(payload, "actor", 32)
    if actor not in {"human", "assistant", "planner", "critic", "virtual_user"}:
        raise StorageError("database_protocol_error", "Invalid settled turn actor")
    status = str(payload.get("status") or "completed")
    if status in {"pending", "running"} or status not in {
        "completed", "failed", "interrupted", "truncated", "superseded"
    }:
        raise StorageError("database_protocol_error", "Invalid settled turn status")
    projection = payload.get("projection") or {}
    if not isinstance(projection, Mapping):
        raise StorageError("database_protocol_error", "Invalid turn projection")
    projection = normalize_projection_document(dict(projection))
    settlement = payload.get("settlement") or {
        "outcome": status,
        "cause": "ingested",
        "resumeOptions": [],
    }
    if not isinstance(settlement, Mapping):
        raise StorageError("database_protocol_error", "Invalid turn settlement")
    lane_id = str(payload.get("lane_id") or "main")
    command_id = _required_text(payload, "command_id", 256)
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    conversation, command_now = _ensure_turn_conversation_header(session, payload)
    tail = session.fetch_one(
        "SELECT turn_id,ordinal FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id=? "
        "ORDER BY ordinal DESC LIMIT 1",
        (conv_id, user_id, lane_id),
    )
    parent_turn_id = str(tail["turn_id"]) if tail is not None else None
    ordinal = int(tail["ordinal"]) + 1 if tail is not None else 0
    now = _integer(payload, "created_at", default=command_now, minimum=0)
    turn_id = str(payload.get("turn_id") or uuid.uuid4())
    attempt_id = str(uuid.uuid4()) if actor != "human" else None
    session.execute(
        "INSERT INTO storage_conversation_turns "
        "(turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,"
        "kind,run_id,status,current_attempt_id,projection_json,"
        "projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            turn_id,
            conv_id,
            user_id,
            lane_id,
            parent_turn_id,
            ordinal,
            actor,
            str(payload.get("kind") or "ingested"),
            str(payload.get("run_id") or ""),
            status,
            attempt_id,
            _dump(projection),
            _dump(dict(settlement)),
            now,
            now,
        ),
    )
    attempt_public = None
    if attempt_id is not None:
        error = payload.get("error") or {}
        session.execute(
            "INSERT INTO storage_generation_attempts "
            "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,"
            "status,base_projection_revision,resume_anchor_json,config_json,"
            "error_json,created_at,started_at,settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                conv_id,
                turn_id,
                command_id,
                "",
                "ingest",
                status,
                _dump({}),
                _dump(payload.get("config") or {}),
                _dump(error),
                now,
                now,
                now,
            ),
        )
        attempt_public = _attempt_public(session.fetch_one(
            "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
            (attempt_id,),
        ))
    revision = int(conversation["rev"]) + 1
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? "
        "WHERE id=? AND user_id=?",
        (revision, now, conv_id, user_id),
    )
    turn_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (turn_id,))
    _upsert_turn_search_row(session, turn_row)
    return {
        "turn": _turn_public(turn_row),
        "attempt": attempt_public,
        "conversationRevision": revision,
    }


def _turn_attempt_claim(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    session.lock_key("attempt_dispatch", attempt_id)
    owner = session.fetch_one(
        "SELECT 1 AS present FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.attempt_id=? AND t.user_id=?",
        (attempt_id, user_id),
    )
    if owner is None:
        return False
    changed = session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=? "
        "AND status='pending' AND task_id=''",
        (f"@dispatching:{attempt_id}", attempt_id),
    )
    return bool(changed)


def _turn_attempt_create(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    command_id = _required_text(payload, "command_id", 256)
    operation = _required_text(payload, "operation", 64)
    expected_revision = _integer(payload, "expected_projection_revision", minimum=0)
    if operation not in {"continue", "checkpoint_resume", "regenerate"}:
        raise StorageError("database_protocol_error", "Invalid attempt operation")
    session.lock_key("turn_command", f"{conv_id}:{command_id}")
    existing = session.fetch_one(
        "SELECT a.attempt_id, a.turn_id FROM storage_generation_attempts a "
        "WHERE a.conversation_id=? AND a.command_id=?",
        (conv_id, command_id),
    )
    if existing is not None:
        attempt_row = session.fetch_one(
            "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
            (existing["attempt_id"],),
        )
        turn_row = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
            (existing["turn_id"],),
        )
        replay = {
            "turn": _turn_public(turn_row),
            "attempt": _attempt_public(attempt_row),
            "conversationRevision": _turn_revision(session, payload),
            "streamCursor": 1,
            "idempotentReplay": True,
            "_needsStart": attempt_row["status"] == "pending"
            and not attempt_row["task_id"],
        }
        if operation == "regenerate":
            # The first execution truncated the lane tail in the same
            # transaction; its tombstones carry that attempt's commit
            # timestamp, so a replayed response reports the same discard
            # set. A same-millisecond sibling delete can over-report ids
            # here, but every tombstoned id is genuinely deleted, so client
            # eviction still converges.
            replay["deletedTurnIds"] = [
                str(row["turn_id"])
                for row in session.fetch_all(
                    "SELECT turn_id FROM storage_turn_tombstones "
                    "WHERE conversation_id=? AND user_id=? AND deleted_at=?",
                    (conv_id, user_id, int(attempt_row["created_at"])),
                )
            ]
        return replay
    session.lock_key("turn_attempt", turn_id)
    turn = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    if turn is None:
        raise StorageError("database_not_found", "Turn not found")
    public = _turn_public(turn)
    if expected_revision != public["projectionRevision"]:
        raise StorageError(
            "turn_projection_stale",
            "The turn changed since this command was prepared.",
        )
    current_id = public.get("currentAttemptId")
    if current_id:
        current = session.fetch_one(
            "SELECT status FROM storage_generation_attempts WHERE attempt_id=?",
            (current_id,),
        )
        if current is not None and current["status"] in ("pending", "running"):
            raise StorageError(
                "database_conflict", "This turn already has a live attempt."
            )
    settlement = public["settlement"]
    options = {
        item if isinstance(item, str) else item.get("operation")
        for item in settlement.get("resumeOptions") or []
    }
    if operation != "regenerate" and operation not in options:
        raise StorageError(
            "database_conflict",
            f"{operation} is not available for the current settlement.",
        )
    anchors = {
        item.get("operation"): item.get("anchor") or {}
        for item in settlement.get("resumeOptions") or []
        if isinstance(item, Mapping)
    }
    requested_anchor = payload.get("resume_anchor")
    anchor = dict(anchors.get(operation) or {})
    if requested_anchor is not None and dict(requested_anchor) != anchor:
        raise StorageError(
            "database_conflict", "The requested resume anchor is not current."
        )
    now = int(time.time() * 1000)
    projection = dict(public["projection"])
    submitted = None
    submitted_projection_changes: list[dict[str, Any]] = []
    input_update = payload.get("input_update")
    if input_update is not None:
        if operation != "regenerate":
            raise StorageError(
                "database_conflict", "Only regenerate may edit its submitted turn."
            )
        parent_id = turn["parent_turn_id"]
        parent = (
            session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
                (conv_id, user_id, parent_id),
            )
            if parent_id
            else None
        )
        if parent is None or parent["actor"] not in ("human", "virtual_user", "critic"):
            raise StorageError(
                "database_conflict",
                "The generated turn has no editable submitted parent.",
            )
        expected_input = payload.get("expected_input_projection_revision")
        if expected_input is None or int(expected_input) != int(
            parent["projection_revision"]
        ):
            raise StorageError(
                "database_conflict", "The submitted turn changed since editing began."
            )
        updated_input = (
            input_update
            if isinstance(input_update, Mapping)
            else {"content": str(input_update)}
        )
        previous_input_projection = _load(parent["projection_json"]) or {}
        if not isinstance(previous_input_projection, Mapping):
            previous_input_projection = {}
        next_input_projection = dict(updated_input)
        parent_base_revision = int(parent["projection_revision"])
        parent_target_revision = parent_base_revision + 1
        changed_input = session.execute(
            "UPDATE storage_conversation_turns SET projection_json=?, projection_revision=?, updated_at=? "
            "WHERE turn_id=? AND projection_revision=?",
            (
                _dump(next_input_projection),
                parent_target_revision,
                now,
                parent_id,
                parent_base_revision,
            ),
        )
        if changed_input != 1:
            raise StorageError(
                "database_conflict",
                "The submitted turn changed while regeneration was applied.",
            )
        submitted_projection_changes.append(_projection_change(
            turn_id=str(parent_id),
            before=previous_input_projection,
            after=next_input_projection,
            base_revision=parent_base_revision,
            target_revision=parent_target_revision,
            updated_at=now,
        ))
        parent = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (parent_id,)
        )
        _upsert_turn_search_row(session, parent)
        submitted = _turn_public(parent)
    discarded_turn_ids: list[str] = []
    if operation == "regenerate":
        projection = {"content": "", "thinking": "", "segments": [], "toolRounds": []}
        # A regenerate retry supersedes the whole lane tail: every turn after
        # the regenerated turn (plus branch lanes rooted inside that tail) is
        # truncated in this same transaction, so no client can observe a
        # half-rewritten history. The closure also fails closed when any tail
        # turn is still live.
        tail_ids = [
            str(row["turn_id"])
            for row in session.fetch_all(
                "SELECT turn_id FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal>?",
                (conv_id, user_id, turn["lane_id"], turn["ordinal"]),
            )
        ]
        if tail_ids:
            discarded_turn_ids = _delete_turn_row_set(
                session,
                conv_id,
                user_id,
                _turn_deletion_closure(session, conv_id, user_id, tail_ids),
                now,
            )
            _prune_turn_tombstones(session, now)
    elif operation == "checkpoint_resume":
        projection["content"] = anchor.get("content", "")
        projection["thinking"] = anchor.get("thinking", "")
        kept = int(anchor.get("keptToolRounds", 0) or 0)
        projection["toolRounds"] = list((projection.get("toolRounds") or [])[:kept])
        projection["segments"] = list(anchor.get("segments") or [])
    new_revision = public["projectionRevision"] + 1
    attempt_id = str(uuid.uuid4())
    session.execute(
        "INSERT INTO storage_generation_attempts "
        "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            conv_id,
            turn_id,
            command_id,
            "",
            operation,
            "pending",
            public["projectionRevision"],
            _dump(anchor),
            _dump(payload.get("config") or {}),
            _dump({}),
            now,
        ),
    )
    session.execute(
        "UPDATE storage_conversation_turns SET status=?, current_attempt_id=?, projection_json=?, "
        "projection_revision=?, settlement_json=?, updated_at=? WHERE turn_id=? AND projection_revision=?",
        (
            "pending",
            attempt_id,
            _dump(projection),
            new_revision,
            _dump({}),
            now,
            turn_id,
            public["projectionRevision"],
        ),
    )
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    attempt_public = _attempt_public(attempt)
    event = {
        "conversationId": conv_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": 1,
        "projectionRevision": new_revision,
        "type": "status_changed",
        "payload": {
            "status": "pending",
            "operation": operation,
            "projectionPatch": build_projection_patch(
                public["projection"],
                projection,
                base_revision=public["projectionRevision"],
                target_revision=new_revision,
            ),
            "turnState": {
                "turnId": turn_id,
                "status": "pending",
                "currentAttemptId": attempt_id,
                "settlement": {},
                "updatedAt": now,
            },
            "attempts": [attempt_public],
        },
    }
    _insert_attempt_event(
        session,
        attempt_id=attempt_id,
        sequence=1,
        conversation_id=conv_id,
        turn_id=turn_id,
        projection_revision=new_revision,
        event_type="status_changed",
        envelope=event,
        created_at=now,
    )
    revision_row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    revision = int(revision_row["rev"]) + 1 if revision_row else 1
    session.execute(
        "UPDATE storage_conversations SET rev=?, updated_at_ms=? WHERE id=? AND user_id=?",
        (revision, now, conv_id, user_id),
    )
    turn = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (turn_id,)
    )
    result = {
        "turn": _turn_public(turn),
        "attempt": attempt_public,
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": False,
        "_needsStart": True,
        "_conversationSyncAttemptEvents": [event],
        "_conversationSyncTurnPatches": submitted_projection_changes,
    }
    if submitted is not None:
        result["submittedTurn"] = submitted
    if operation == "regenerate":
        result["deletedTurnIds"] = discarded_turn_ids
    return result


def _turn_projection_update(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    expected = _integer(payload, "expected_projection_revision", minimum=0)
    session.lock_key("turn_attempt", turn_id)
    row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    if row is None:
        raise StorageError("database_not_found", "Turn not found")
    if row["status"] in ("pending", "running"):
        raise StorageError("turn_in_progress", "A running turn cannot be edited.")
    if int(row["projection_revision"]) != expected:
        raise StorageError(
            "turn_projection_stale", "The turn changed since editing began.")
    projection = payload.get("projection")
    if not isinstance(projection, Mapping):
        projection = {"content": str(projection or "")}
    previous_projection = _load(row["projection_json"]) or {}
    if not isinstance(previous_projection, Mapping):
        previous_projection = {}
    next_projection = dict(projection)
    now = int(time.time() * 1000)
    revision = expected + 1
    changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?, projection_revision=?, updated_at=? "
        "WHERE turn_id=? AND projection_revision=?",
        (_dump(next_projection), revision, now, turn_id, expected),
    )
    if not changed:
        raise StorageError(
            "turn_projection_stale", "The turn changed while the edit was applied."
        )
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, conv_id, user_id),
    )
    updated = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (turn_id,)
    )
    _upsert_turn_search_row(session, updated)
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "turn": _turn_public(updated),
        "conversationRevision": int(conversation["rev"]),
        "_conversationSyncTurnPatch": _projection_change(
            turn_id=turn_id,
            before=previous_projection,
            after=next_projection,
            base_revision=expected,
            target_revision=revision,
            updated_at=now,
        ),
    }


def _turn_related_announce(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    turn_ids = [str(value) for value in payload.get("turn_ids") or [] if value]
    if not turn_ids:
        return False
    session.lock_key("attempt_events", attempt_id)
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    if attempt is None or attempt["status"] not in ("pending", "running"):
        return False
    root = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
        (attempt["turn_id"],),
    )
    if (root is None or int(root["user_id"]) != user_id
            or root["current_attempt_id"] != attempt_id):
        return False
    related, related_attempts = [], []
    for turn_id in turn_ids:
        row = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND conversation_id=?",
            (turn_id, attempt["conversation_id"]),
        )
        if row is None:
            continue
        related.append(_turn_public(row))
        if row["current_attempt_id"]:
            child = session.fetch_one(
                "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
                (row["current_attempt_id"],),
            )
            if child is not None:
                related_attempts.append(_attempt_public(child))
    if not related:
        return False
    now = int(time.time() * 1000)
    root_projection = _load(root["projection_json"]) or {}
    if not isinstance(root_projection, Mapping):
        root_projection = {}
    revision = int(root["projection_revision"]) + 1
    session.execute(
        "UPDATE storage_conversation_turns SET projection_revision=?, updated_at=? "
        "WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
        (revision, now, root["turn_id"], attempt_id, int(root["projection_revision"])),
    )
    event_result = _turn_event_append(
        session,
        {
            "attempt_id": attempt_id,
            "conversation_id": attempt["conversation_id"],
            "turn_id": root["turn_id"],
            "projection_revision": revision,
            "type": "projection_updated",
            "event": {
                "projectionPatch": build_projection_patch(
                    root_projection,
                    root_projection,
                    base_revision=int(root["projection_revision"]),
                    target_revision=revision,
                ),
                "turns": [
                    item for item in related
                    if item.get("turnId") != root["turn_id"]
                ],
                "attempts": related_attempts,
                "updateKind": "related_turns_created",
            },
        },
    )
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, attempt["conversation_id"], root["user_id"]),
    )
    return {
        "changed": True,
        "_conversationSyncAttemptEvents": [event_result["event"]],
    }


def _turn_branch_create(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    parent_id = _required_text(payload, "parent_turn_id", 128)
    expected = _integer(payload, "expected_projection_revision", minimum=0)
    session.lock_key("turn_attempt", parent_id)
    parent = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, parent_id),
    )
    if parent is None:
        raise StorageError("database_not_found", "Parent turn not found")
    public = _turn_public(parent)
    if public["status"] in ("pending", "running"):
        raise StorageError(
            "database_conflict", "A running parent turn cannot be branched."
        )
    if expected != public["projectionRevision"]:
        raise StorageError(
            "database_conflict", "The parent turn changed before branch creation."
        )
    now = int(time.time() * 1000)
    lane = {
        "laneId": f"lane_{uuid.uuid4()}",
        "parentTurnId": parent_id,
        "title": str(payload.get("title") or "Branch")[:200],
        "icon": "⑂",
        "kind": str(payload.get("kind") or "branch")[:80],
        "anchorText": str(payload.get("anchor_text") or "")[:1000],
        "parentSelection": str(payload.get("parent_selection") or "")[:10000],
        "createdAt": now,
    }
    previous_projection = dict(public["projection"])
    projection = dict(previous_projection)
    projection["_branchLanes"] = list(projection.get("_branchLanes") or []) + [lane]
    revision = expected + 1
    session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?, projection_revision=?, updated_at=? "
        "WHERE turn_id=? AND projection_revision=?",
        (_dump(projection), revision, now, parent_id, expected),
    )
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, conv_id, user_id),
    )
    updated = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (parent_id,)
    )
    _upsert_turn_search_row(session, updated)
    conv = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "turn": _turn_public(updated),
        "lane": lane,
        "conversationRevision": int(conv["rev"]),
        "_conversationSyncTurnPatch": _projection_change(
            turn_id=parent_id,
            before=previous_projection,
            after=projection,
            base_revision=expected,
            target_revision=revision,
            updated_at=now,
        ),
    }


def _turn_branch_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    parent_id = _required_text(payload, "parent_turn_id", 128)
    lane_id = _required_text(payload, "lane_id", 128)
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    parent = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, parent_id),
    )
    if parent is None:
        raise StorageError("database_not_found", "Parent turn not found")
    if _turn_row_is_live(session, parent):
        raise StorageError(
            "database_conflict", "A running parent turn cannot delete a branch."
        )
    projection = _load(parent["projection_json"]) or {}
    if not isinstance(projection, Mapping):
        projection = {}
    previous_projection = dict(projection)
    projection = dict(projection)
    descriptors = list(projection.get("_branchLanes") or [])
    kept = [item for item in descriptors if item.get("laneId") != lane_id]
    if len(kept) == len(descriptors):
        raise StorageError("database_not_found", "Branch lane not found")
    child_rows = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns WHERE conversation_id=? AND lane_id=?",
        (conv_id, lane_id),
    )
    now = int(time.time() * 1000)
    delete_rows = _turn_deletion_closure(
        session,
        conv_id,
        user_id,
        [str(child["turn_id"]) for child in child_rows],
    )
    deleted_turn_ids = _delete_turn_row_set(
        session, conv_id, user_id, delete_rows, now)
    projection["_branchLanes"] = kept
    _prune_turn_tombstones(session, now)
    revision = int(parent["projection_revision"]) + 1
    session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?, projection_revision=?, updated_at=? WHERE turn_id=?",
        (_dump(projection), revision, now, parent_id),
    )
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, conv_id, user_id),
    )
    updated = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (parent_id,)
    )
    _upsert_turn_search_row(session, updated)
    conv = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "turn": _turn_public(updated),
        "deletedLaneId": lane_id,
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": int(conv["rev"]),
        "_conversationSyncTurnPatch": _projection_change(
            turn_id=parent_id,
            before=previous_projection,
            after=projection,
            base_revision=int(parent["projection_revision"]),
            target_revision=revision,
            updated_at=now,
        ),
    }


def _turn_row_is_live(session: Session, row: Mapping[str, Any]) -> bool:
    """True when a visible row or its current attempt can still mutate."""
    if str(row["status"] or "") in ("pending", "running"):
        return True
    attempt_id = row["current_attempt_id"]
    if not attempt_id:
        return False
    attempt = session.fetch_one(
        "SELECT status FROM storage_generation_attempts WHERE attempt_id=?",
        (attempt_id,),
    )
    return bool(
        attempt is not None
        and str(attempt["status"] or "") in ("pending", "running")
    )


def _turn_deletion_closure(
    session: Session,
    conv_id: str,
    user_id: int,
    wanted: list[str],
) -> dict[str, Mapping[str, Any]]:
    """Resolve explicit turns plus every nested branch lane they own.

    A parent projection owns its ``_branchLanes`` descriptors. Deleting only
    the first level leaves nested branch rows unreachable but durable. Resolve
    the full closure before the first write and validate that every row is
    settled, so both ordinary delete and atomic compaction fail closed.
    """
    rows: dict[str, Mapping[str, Any]] = {}
    pending_turn_ids = list(dict.fromkeys(str(item) for item in wanted if item))
    pending_lane_ids: list[str] = []
    seen_lanes: set[str] = set()

    while pending_turn_ids or pending_lane_ids:
        while pending_turn_ids:
            turn_id = pending_turn_ids.pop()
            if turn_id in rows:
                continue
            row = session.fetch_one(
                "SELECT * FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? AND turn_id=?",
                (conv_id, user_id, turn_id),
            )
            if row is None:
                raise StorageError("database_not_found", "Turn not found")
            rows[turn_id] = row
            projection = _load(row["projection_json"]) or {}
            if isinstance(projection, Mapping):
                for descriptor in projection.get("_branchLanes") or []:
                    if isinstance(descriptor, Mapping) and descriptor.get("laneId"):
                        pending_lane_ids.append(str(descriptor["laneId"]))

        if pending_lane_ids:
            lane_id = pending_lane_ids.pop()
            if lane_id in seen_lanes:
                continue
            seen_lanes.add(lane_id)
            children = session.fetch_all(
                "SELECT * FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? AND lane_id=?",
                (conv_id, user_id, lane_id),
            )
            pending_turn_ids.extend(
                str(child["turn_id"]) for child in children
                if str(child["turn_id"]) not in rows
            )

    for row in rows.values():
        if _turn_row_is_live(session, row):
            raise StorageError(
                "database_conflict",
                "A running turn or child branch cannot be compacted/deleted.",
            )
    return rows


def _delete_turn_row_set(
    session: Session,
    conv_id: str,
    user_id: int,
    rows: Mapping[str, Mapping[str, Any]],
    now: int,
) -> list[str]:
    """Delete a prevalidated closure and emit delta-sync tombstones."""
    delete_ids = sorted(rows)
    _delete_turn_search_rows(session, rows)
    # Stay below SQLite's host-parameter ceiling while using set-wise deletes;
    # the old per-turn loop multiplied statement and fsync overhead on long
    # histories, making a logically small compaction hit the writer watchdog.
    for offset in range(0, len(delete_ids), 256):
        chunk = delete_ids[offset:offset + 256]
        marks = ",".join("?" for _ in chunk)
        session.execute(
            "DELETE FROM storage_attempt_events WHERE attempt_id IN ("
            "SELECT attempt_id FROM storage_generation_attempts "
            "WHERE turn_id IN (" + marks + "))",
            tuple(chunk),
        )
        session.execute(
            "DELETE FROM storage_generation_attempts WHERE turn_id IN ("
            + marks + ")",
            tuple(chunk),
        )
        session.execute(
            "DELETE FROM storage_conversation_turns WHERE turn_id IN ("
            + marks + ")",
            tuple(chunk),
        )
    for turn_id in delete_ids:
        session.execute(
            "INSERT INTO storage_turn_tombstones "
            "(conversation_id,user_id,turn_id,deleted_at) VALUES (?,?,?,?) "
            "ON CONFLICT(conversation_id,turn_id) DO NOTHING",
            (conv_id, user_id, turn_id, now),
        )
    return delete_ids


def _turn_compact(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically replace a folded turn region with one summary turn.

    The caller performs the expensive LLM summary outside the transaction,
    then submits only a compact delta: stable turns to delete, retained turn
    projections that were intra-turn-folded, and the synthetic summary. A
    conversation-revision CAS closes the summary-time race; every structural
    mutation lands in this single writer transaction or none of it does.
    """
    conv_id, user_id = _turn_identity(payload)
    expected_revision = _integer(
        payload, "expected_conversation_revision", minimum=0)
    summary_turn_id = _required_text(payload, "summary_turn_id", 128)
    summary_projection = normalize_projection_document(
        payload.get("summary_projection"))
    summary_compaction = summary_projection.get("compaction")
    if (
        not isinstance(summary_compaction, Mapping)
        or summary_compaction.get("blockId") != "compaction"
    ):
        raise StorageError(
            "database_protocol_error",
            "Turn compaction summary marker is required.",
        )
    wanted = list(dict.fromkeys(
        str(item) for item in payload.get("delete_turn_ids") or [] if item
    ))
    raw_updates = payload.get("projection_updates") or []
    if not isinstance(raw_updates, list):
        raise StorageError(
            "database_protocol_error", "projection_updates must be a list")
    insert_after = str(payload.get("insert_after_turn_id") or "")
    insert_before = str(payload.get("insert_before_turn_id") or "")
    if not insert_after and not insert_before:
        raise StorageError(
            "database_protocol_error", "Summary insertion anchor is required")

    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        raise StorageError("database_not_found", "Conversation not found")
    actual_revision = int(conversation["rev"] or 0)
    if actual_revision != expected_revision:
        return {
            "applied": False,
            "conversationRevision": actual_revision,
        }
    if session.fetch_one(
        "SELECT turn_id FROM storage_conversation_turns WHERE turn_id=?",
        (summary_turn_id,),
    ) is not None:
        raise StorageError("database_conflict", "Summary turn already exists")

    main_rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main' "
        "ORDER BY ordinal",
        (conv_id, user_id),
    )
    if not main_rows:
        raise StorageError("database_not_found", "Turn transcript not found")
    main_by_id = {str(row["turn_id"]): row for row in main_rows}
    unknown_delete_ids = set(wanted) - set(main_by_id)
    if unknown_delete_ids:
        raise StorageError("database_not_found", "Compaction turn not found")

    delete_rows = _turn_deletion_closure(
        session, conv_id, user_id, wanted)
    delete_ids = set(delete_rows)
    retained_rows = [
        row for row in main_rows if str(row["turn_id"]) not in delete_ids
    ]
    retained_by_id = {str(row["turn_id"]): row for row in retained_rows}
    if not retained_rows:
        raise StorageError(
            "database_protocol_error", "Compaction must preserve a live tail")
    for row in retained_rows:
        if _turn_row_is_live(session, row):
            raise StorageError(
                "database_conflict", "A running turn cannot be compacted.")

    if insert_after and insert_after not in retained_by_id:
        raise StorageError(
            "database_protocol_error", "Invalid summary predecessor")
    if insert_before and insert_before not in retained_by_id:
        raise StorageError(
            "database_protocol_error", "Invalid summary successor")
    retained_ids = [str(row["turn_id"]) for row in retained_rows]
    after_index = retained_ids.index(insert_after) if insert_after else -1
    before_index = retained_ids.index(insert_before) if insert_before else len(retained_ids)
    if before_index != after_index + 1:
        raise StorageError(
            "database_protocol_error",
            "Summary anchors must be adjacent after folded turns are removed",
        )
    if insert_after:
        summary_ordinal = int(retained_by_id[insert_after]["ordinal"]) + 1
    else:
        summary_ordinal = int(retained_by_id[insert_before]["ordinal"]) - 1
    if insert_before and summary_ordinal >= int(
        retained_by_id[insert_before]["ordinal"]
    ):
        raise StorageError(
            "database_conflict", "No ordinal gap for compaction summary")

    normalized_updates: list[tuple[Mapping[str, Any], dict[str, Any], int]] = []
    seen_update_ids: set[str] = set()
    for update in raw_updates:
        if not isinstance(update, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid projection update")
        turn_id = str(update.get("turn_id") or "")
        if not turn_id or turn_id in seen_update_ids or turn_id not in retained_by_id:
            raise StorageError(
                "database_protocol_error", "Invalid retained turn update")
        seen_update_ids.add(turn_id)
        row = retained_by_id[turn_id]
        expected_projection_revision = update.get("expected_projection_revision")
        if (
            not isinstance(expected_projection_revision, int)
            or isinstance(expected_projection_revision, bool)
            or expected_projection_revision < 0
            or int(row["projection_revision"]) != expected_projection_revision
        ):
            raise StorageError(
                "database_conflict", "A retained turn changed before compaction")
        normalized_updates.append((
            row,
            normalize_projection_document(update.get("projection")),
            expected_projection_revision,
        ))

    now = int(time.time() * 1000)
    advanced = session.execute(
        "UPDATE storage_conversations SET rev=rev+1,updated_at_ms=? "
        "WHERE id=? AND user_id=? AND rev=?",
        (now, conv_id, user_id, expected_revision),
    )
    if advanced != 1:
        return {
            "applied": False,
            "conversationRevision": actual_revision,
        }

    deleted_turn_ids = _delete_turn_row_set(
        session, conv_id, user_id, delete_rows, now)
    for row, projection, projection_revision in normalized_updates:
        changed = session.execute(
            "UPDATE storage_conversation_turns SET projection_json=?,"
            "projection_revision=?,updated_at=? "
            "WHERE turn_id=? AND projection_revision=?",
            (
                _dump(projection), projection_revision + 1, now,
                row["turn_id"], projection_revision,
            ),
        )
        if changed != 1:
            raise StorageError(
                "database_conflict", "A retained turn changed during compaction")
        _upsert_turn_search_row(
            session,
            row,
            projection=projection,
            projection_revision=projection_revision + 1,
            updated_at=now,
        )

    settlement = {
        "outcome": "completed",
        "cause": "manual_compaction",
        "resumeOptions": [],
    }
    session.execute(
        "INSERT INTO storage_conversation_turns "
        "(turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,"
        "actor,kind,run_id,status,current_attempt_id,projection_json,"
        "projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            summary_turn_id, conv_id, user_id, "main",
            insert_after or None, summary_ordinal, "assistant", "compaction",
            "", "completed", None, _dump(summary_projection), 1,
            _dump(settlement), now, now,
        ),
    )

    # Preserve a navigable ancestry chain. The first retained turn after the
    # summary points to it; any other main turn whose parent was folded does
    # the same. Branch rows owned by folded parents are in delete_rows.
    for row in retained_rows:
        turn_id = str(row["turn_id"])
        parent_id = str(row["parent_turn_id"] or "")
        if turn_id == insert_before or parent_id in delete_ids:
            session.execute(
                "UPDATE storage_conversation_turns "
                "SET parent_turn_id=?,updated_at=? WHERE turn_id=?",
                (summary_turn_id, now, turn_id),
            )

    _prune_turn_tombstones(session, now)
    summary_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
        (summary_turn_id,),
    )
    _upsert_turn_search_row(session, summary_row)
    return {
        "applied": True,
        "turn": _turn_public(summary_row),
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": expected_revision + 1,
    }


def _turn_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    wanted = list(
        dict.fromkeys(str(value) for value in payload.get("turn_ids") or [] if value)
    )
    if not wanted:
        raise StorageError("database_protocol_error", "turnIds required")
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    rows = _turn_deletion_closure(session, conv_id, user_id, wanted)
    now = int(time.time() * 1000)
    delete_ids = _delete_turn_row_set(
        session, conv_id, user_id, rows, now)
    _prune_turn_tombstones(session, now)
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, conv_id, user_id),
    )
    conv = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "deletedTurnIds": delete_ids,
        "conversationRevision": int(conv["rev"]),
    }


def _turn_recover(session: Session, payload: Mapping[str, Any]) -> Any:
    now = int(time.time() * 1000)
    # Bounded-chunk recovery (2026-08-19 "侧边栏好多回答中 + badge 永远重连中"
    # incident): this used to settle EVERY orphaned turn in ONE transaction.
    # Each settlement rewrites the whole turn row (SQLite rewrites the record,
    # multi-MiB projection_json included) and inserts an event row carrying the
    # full projection — ~1-3s per big turn on a network FS — so more than a
    # couple of large projections reliably blew the 5s writer watchdog and the
    # whole recovery ROLLED BACK, leaving 'running' zombie turns forever.
    # Settle at most ``max_rows`` turns / ``max_bytes`` of projection payload
    # per call (always ≥1 row so an oversized projection still progresses) and
    # report the remainder; the caller loops until ``remaining`` is 0.
    max_rows = _integer(payload, "max_rows", default=8, minimum=1, maximum=500)
    max_bytes = _integer(payload, "max_bytes", default=2_000_000, minimum=1)
    from lib.storage_sidecar.projection_codec import (
        projection_hydration_byte_upper_bound,
    )
    # Optional liveness guards for the POST-SERVING backstop (unused on the
    # boot path, where the registry is empty by construction): only settle
    # attempts created before ``created_before_ms`` and never touch an attempt
    # whose bound task is live in this process right now.
    created_before_ms = payload.get("created_before_ms")
    if created_before_ms is not None:
        created_before_ms = _integer(payload, "created_before_ms", minimum=0)
    _exclude = payload.get("exclude_task_ids")
    exclude_task_ids = (
        {str(t) for t in _exclude if t}
        if isinstance(_exclude, (list, tuple, set))
        else set()
    )
    rows = session.fetch_all(
        "SELECT a.*, t.user_id, t.projection_json, t.projection_revision "
        "FROM storage_generation_attempts a JOIN storage_conversation_turns t "
        "ON t.turn_id=a.turn_id AND t.current_attempt_id=a.attempt_id "
        "WHERE a.status IN ('pending','running')"
        + (" AND a.created_at < ?" if created_before_ms is not None else ""),
        ((created_before_ms,) if created_before_ms is not None else ()),
    )
    recovered = 0
    remaining = 0
    chunk_rows = 0
    chunk_bytes = 0
    recovered_events: list[dict[str, Any]] = []
    for row in rows:
        if exclude_task_ids and str(row["task_id"] or "") in exclude_task_ids:
            continue
        # Budget the hydrated working set, not SQLite's stored byte count or
        # PostgreSQL JSONB's misleading top-level dict key count.
        projection_len = projection_hydration_byte_upper_bound(
            row["projection_json"]
        )
        if chunk_rows and (
            chunk_rows >= max_rows or chunk_bytes + projection_len > max_bytes
        ):
            remaining += 1
            continue
        projection = _load(row["projection_json"]) or {}
        # Resume options must be COMPUTED from the durable projection, never
        # hardcoded to regenerate-only: the projection already carries the
        # partial content + completed tool rounds, so the same settlement the
        # legacy path (lib.turn_lifecycle.recover_running_attempts) produces
        # applies here — lossless 'continue' for a prefill-capable model with
        # a content tail, 'checkpoint_resume' when leading tool rounds are
        # done, 'regenerate' always. The regenerate-only hardcode dropped the
        # honest tool-checkpoint resume the user could have continued from.
        from lib.turn_lifecycle import _settlement as _compute_settlement

        _, settlement = _compute_settlement(
            {
                "model": (_load(row["config_json"]) or {}).get("model", ""),
                "content": projection.get("content", ""),
                "thinking": projection.get("thinking", ""),
                "toolRounds": projection.get("toolRounds", []),
            },
            {"type": "aborted", "finishReason": "interrupted"},
            projection,
        )
        settlement["cause"] = "server_restart"
        old_revision = int(row["projection_revision"])
        new_revision = old_revision + 1
        changed = session.execute(
            "UPDATE storage_generation_attempts SET status='interrupted', settled_at=? "
            "WHERE attempt_id=? AND status IN ('pending','running')",
            (now, row["attempt_id"]),
        )
        if not changed:
            continue
        session.execute(
            "UPDATE storage_conversation_turns SET status='interrupted', projection_revision=?, "
            "settlement_json=?, updated_at=? WHERE turn_id=? AND current_attempt_id=? "
            "AND projection_revision=?",
            (
                new_revision,
                _dump(settlement),
                now,
                row["turn_id"],
                row["attempt_id"],
                old_revision,
            ),
        )
        event = {
            "conversationId": row["conversation_id"],
            "turnId": row["turn_id"],
            "attemptId": row["attempt_id"],
            "seq": 1
            + int(
                session.fetch_one(
                    "SELECT COALESCE(MAX(sequence),0) AS n FROM storage_attempt_events WHERE attempt_id=?",
                    (row["attempt_id"],),
                )["n"]
            ),
            "projectionRevision": new_revision,
            "type": "terminal_settlement",
            "payload": {
                "status": "interrupted",
                "settlement": settlement,
                "projectionPatch": build_projection_patch(
                    projection,
                    projection,
                    base_revision=old_revision,
                    target_revision=new_revision,
                ),
            },
        }
        _insert_attempt_event(
            session,
            attempt_id=row["attempt_id"],
            sequence=event["seq"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            projection_revision=new_revision,
            event_type="terminal_settlement",
            envelope=event,
            created_at=now,
        )
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            (now, row["conversation_id"], row["user_id"]),
        )
        recovered_turn = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
            (row["turn_id"],),
        )
        _upsert_turn_search_row(session, recovered_turn)
        recovered_events.append(event)
        recovered += 1
        chunk_rows += 1
        chunk_bytes += projection_len
    # Rows skipped over the budget are retried by the caller's next chunk.
    return {
        "recovered": recovered,
        "remaining": remaining,
        "_conversationSyncAttemptEvents": recovered_events,
    }


def _turn_cleanup(session: Session, payload: Mapping[str, Any]) -> Any:
    retention_ms = max(
        _integer(payload, "retention_ms", default=6 * 60 * 60 * 1000, minimum=0), 0
    )
    limit = _integer(payload, "limit", default=500, minimum=1, maximum=5000)
    cutoff = int(time.time() * 1000) - retention_ms
    rows = session.fetch_all(
        "SELECT a.attempt_id FROM storage_generation_attempts a "
        "WHERE a.status='superseded' AND a.superseded_at IS NOT NULL AND a.superseded_at<? "
        "AND NOT EXISTS (SELECT 1 FROM storage_conversation_turns t WHERE t.current_attempt_id=a.attempt_id) "
        "ORDER BY a.superseded_at LIMIT ?",
        (cutoff, limit),
    )
    for row in rows:
        session.execute(
            "DELETE FROM storage_attempt_events WHERE attempt_id=?",
            (row["attempt_id"],),
        )
        session.execute(
            "DELETE FROM storage_generation_attempts WHERE attempt_id=?",
            (row["attempt_id"],),
        )
    return len(rows)


def _turn_search_backfill(session: Session, payload: Mapping[str, Any]) -> Any:
    """Compatibility command that schedules an out-of-authority rebuild.

    Historical callers still issue this command. It now performs one tiny
    dirty-set UPSERT and returns immediately; the sidecar-owned projection
    runtime scans through the read pool and writes its independent store.
    """
    del payload
    _mark_turn_search_projection_dirty(
        session,
        entity_kind="rebuild",
        user_id=0,
        entity_key="all",
    )
    return {
        "scanned": 0,
        "indexed": 0,
        "failed": 0,
        "projectionBytes": 0,
        "nextCursor": "",
        "remaining": False,
        "scheduled": True,
    }


def _partition_visible_messages(messages: Any) -> tuple[list[Any], list[Any]]:
    """Drop only provably empty legacy virtual-user projection ghosts.

    ``turn.visible.sync`` is the compatibility boundary that projects an
    executor's legacy message list into authoritative turn rows.  Empty
    virtual-user shells and their directly-adjacent empty aborted assistant
    have no user-visible or durable content, so they must never cross that
    boundary.  Every other object is retained byte-for-byte.
    """
    source = list(messages) if isinstance(messages, (list, tuple)) else []
    dropped_indices: set[int] = set()

    def _empty(value: Any) -> bool:
        return not str(value or "").strip()

    for index, message in enumerate(source):
        if (
            isinstance(message, Mapping)
            and message.get("role") == "user"
            and message.get("_isVirtualUser")
            and _empty(message.get("content"))
        ):
            dropped_indices.add(index)

    for index, message in enumerate(source):
        if index in dropped_indices or not isinstance(message, Mapping):
            continue
        if (
            message.get("role") == "assistant"
            and _empty(message.get("content"))
            and _empty(message.get("thinking"))
            and message.get("finishReason") == "aborted"
            and not message.get("toolRounds")
            and index - 1 in dropped_indices
        ):
            dropped_indices.add(index)

    kept = [
        message for index, message in enumerate(source)
        if index not in dropped_indices
    ]
    dropped = [
        message for index, message in enumerate(source)
        if index in dropped_indices
    ]
    return kept, dropped


def _visible_shape(
    message: Mapping[str, Any], default_kind: str
) -> tuple[str, str, dict[str, Any]]:
    from lib.orchestration_message_compat import normalize_flow_message
    from lib.turn_projection_patch import normalize_projection_document

    message = normalize_flow_message(message)
    role = message.get("role")
    if message.get("_isVirtualUser"):
        actor, kind = "virtual_user", "autopilot_virtual_user"
    elif message.get("_isFlowReview"):
        actor, kind = "critic", "flow_node"
    elif message.get("_isFlowPlanner"):
        actor, kind = "planner", "flow_node"
    elif message.get("_flowNodeId") or message.get("_flowRunId"):
        actor, kind = ("critic" if role == "user" else "assistant"), "flow_node"
    else:
        actor, kind = (
            ("critic" if role == "user" else "assistant"),
            (default_kind or "flow_node"),
        )
    # Visible phase rows pass through the same projection vocabulary as normal
    # turn lifecycle writes.  The legacy message document remains an executor
    # input only; its role/runtime flags may select actor/kind above but never
    # leak into the durable/public projection.
    projection = normalize_projection_document(message)
    projection.setdefault("content", "")
    projection.setdefault("thinking", "")
    projection.setdefault("segments", [])
    projection.setdefault("toolRounds", [])
    phase = {
        "iteration": message.get("_flowIteration") or message.get("_flowPlannerIteration"),
        "approved": message.get("_flowApproved"),
        "nextPhase": message.get("_flowNextPhase"),
        "stuck": message.get("_isStuck"),
        "flowNodeId": message.get("_flowNodeId"),
        "flowRunId": message.get("_flowRunId"),
    }
    projection["orchestration"] = {
        key: value for key, value in phase.items() if value is not None
    }
    return actor, kind, projection


def _turn_visible_sync(session: Session, payload: Mapping[str, Any]) -> Any:
    from lib.orchestration_message_compat import is_flow_turn_kind

    conv_id = _required_text(payload, "conversation_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    attempt_id = _required_text(payload, "attempt_id", 128)
    root_id = _required_text(payload, "root_turn_id", 128)
    messages, _ = _partition_visible_messages(payload.get("messages") or [])
    if not messages:
        return None
    session.lock_key("attempt_events", attempt_id)
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    root = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND turn_id=?",
        (conv_id, root_id),
    )
    if (attempt is None or root is None or int(root["user_id"]) != user_id
            or root["current_attempt_id"] != attempt_id):
        return None
    root_base_revision = int(root["projection_revision"] or 0)
    root_projection_before = _load(root["projection_json"]) or {}
    if not isinstance(root_projection_before, Mapping):
        root_projection_before = {}
    now = int(time.time() * 1000)
    run_id = str(root["run_id"] or (payload.get("run_id") or attempt_id))
    previous_id = root["parent_turn_id"]
    related, visible_ids = [], []
    changed = False
    sync_event: dict[str, Any] | None = None
    for index, message in enumerate(messages):
        actor, kind, projection = _visible_shape(
            message, str(payload.get("default_kind") or "flow_node")
        )
        if index == 0:
            turn_id = root_id
            visible_ids.append(turn_id)
            previous_id = turn_id
            if not is_flow_turn_kind(root["kind"]):
                updated_root = session.execute(
                    "UPDATE storage_conversation_turns SET actor=?, kind=?, run_id=?, projection_json=?, projection_revision=?, updated_at=? "
                    "WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
                    (
                        actor,
                        kind,
                        run_id,
                        _dump(projection),
                        root_base_revision + 1,
                        now,
                        root_id,
                        attempt_id,
                        root_base_revision,
                    ),
                )
                if updated_root != 1:
                    raise StorageError(
                        "database_conflict",
                        "The visible root turn changed during synchronization.",
                    )
                root = session.fetch_one(
                    "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
                    (root_id,),
                )
                changed = True
            _upsert_turn_search_row(session, root)
            related.append(_turn_public(root))
            continue
        turn_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"turn-attempt:{attempt_id}:visible:{index}")
        )
        child_attempt_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"turn-attempt:{attempt_id}:visible-attempt:{index}"
            )
        )
        visible_ids.append(turn_id)
        existing = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (turn_id,)
        )
        if existing is None:
            ordinal_row = session.fetch_one(
                "SELECT COALESCE(MAX(ordinal),-1) AS ordinal FROM storage_conversation_turns WHERE conversation_id=? AND lane_id=?",
                (conv_id, root["lane_id"]),
            )
            ordinal = int(ordinal_row["ordinal"]) + 1
            settlement = {
                "outcome": "completed",
                "cause": "phase_completed",
                "providerFinishReason": None,
                "error": None,
                "resumeOptions": [
                    {"operation": "regenerate", "anchor": {"type": "turn_start"}}
                ],
            }
            session.execute(
                "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    conv_id,
                    root["user_id"],
                    root["lane_id"],
                    previous_id,
                    ordinal,
                    actor,
                    kind,
                    run_id,
                    "completed",
                    child_attempt_id,
                    _dump(projection),
                    1,
                    _dump(settlement),
                    now,
                    now,
                ),
            )
            session.execute(
                "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at,started_at,settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    child_attempt_id,
                    conv_id,
                    turn_id,
                    f"run:{attempt_id}:visible:{index}",
                    "",
                    "generate",
                    "completed",
                    0,
                    _dump({}),
                    _dump({"runId": run_id}),
                    _dump({}),
                    now,
                    now,
                    now,
                ),
            )
            event = {
                "conversationId": conv_id,
                "turnId": turn_id,
                "attemptId": child_attempt_id,
                "seq": 1,
                "projectionRevision": 1,
                "type": "terminal_settlement",
                "payload": {
                    "status": "completed",
                    "settlement": settlement,
                },
            }
            _insert_attempt_event(
                session,
                attempt_id=child_attempt_id,
                sequence=1,
                conversation_id=conv_id,
                turn_id=turn_id,
                projection_revision=1,
                event_type="terminal_settlement",
                envelope=event,
                created_at=now,
            )
            existing = session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (turn_id,)
            )
            changed = True
        _upsert_turn_search_row(session, existing)
        related.append(_turn_public(existing))
        previous_id = turn_id
    if changed:
        root = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (root_id,)
        )
        revision = int(root["projection_revision"] or 0)
        if revision == root_base_revision:
            revision = root_base_revision + 1
            bumped_root = session.execute(
                "UPDATE storage_conversation_turns SET projection_revision=?, updated_at=? "
                "WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
                (revision, now, root_id, attempt_id, root_base_revision),
            )
            if bumped_root != 1:
                raise StorageError(
                    "database_conflict",
                    "The visible root turn changed during synchronization.",
                )
            root = session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (root_id,)
            )
        elif revision != root_base_revision + 1:
            raise StorageError(
                "database_conflict",
                "The visible root turn revision advanced unexpectedly.",
            )
        related[0] = _turn_public(root)
        root_projection_after = _load(root["projection_json"]) or {}
        if not isinstance(root_projection_after, Mapping):
            root_projection_after = {}
        event_result = _turn_event_append(
            session,
            {
                "attempt_id": attempt_id,
                "conversation_id": conv_id,
                "turn_id": root_id,
                "projection_revision": revision,
                "type": "projection_updated",
                "event": {
                    "projectionPatch": build_projection_patch(
                        root_projection_before,
                        root_projection_after,
                        base_revision=root_base_revision,
                        target_revision=revision,
                    ),
                    "turns": [
                        item for item in related
                        if item.get("turnId") != root_id
                    ],
                    "updateKind": "visible_turns_committed",
                },
            },
        )
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            (now, conv_id, root["user_id"]),
        )
        sync_event = event_result["event"]
    return {
        "visibleTurnIds": visible_ids,
        "_conversationSyncAttemptEvents": [sync_event] if sync_event else [],
    }


def _turn_attempt_bind(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    task_id = _required_text(payload, "task_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    session.lock_key("turn_attempt", attempt_id)
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    if attempt is None:
        return None
    turn = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
        (attempt["turn_id"],),
    )
    if (turn is None or int(turn["user_id"]) != user_id
            or turn["current_attempt_id"] != attempt_id):
        return None
    now = int(time.time() * 1000)
    sync_event: dict[str, Any] | None = None
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=?, status=CASE WHEN status='pending' THEN 'running' ELSE status END, started_at=COALESCE(started_at,?) WHERE attempt_id=?",
        (task_id, now, attempt_id),
    )
    if attempt["status"] == "pending":
        revision = int(turn["projection_revision"]) + 1
        changed = session.execute(
            "UPDATE storage_conversation_turns SET status='running', projection_revision=?, updated_at=? WHERE turn_id=? AND current_attempt_id=? AND status='pending' AND projection_revision=?",
            (
                revision,
                now,
                turn["turn_id"],
                attempt_id,
                int(turn["projection_revision"]),
            ),
        )
        if changed:
            turn_projection = _load(turn["projection_json"]) or {}
            if not isinstance(turn_projection, Mapping):
                turn_projection = {}
            event_result = _turn_event_append(
                session,
                {
                    "attempt_id": attempt_id,
                    "conversation_id": attempt["conversation_id"],
                    "turn_id": turn["turn_id"],
                    "projection_revision": revision,
                    "type": "status_changed",
                    "event": {
                        "status": "running",
                        "projectionPatch": build_projection_patch(
                            turn_projection,
                            turn_projection,
                            base_revision=int(turn["projection_revision"]),
                            target_revision=revision,
                        ),
                    },
                },
            )
            session.execute(
                "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
                (now, turn["conversation_id"], turn["user_id"]),
            )
            sync_event = event_result["event"]
    row = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    result = _attempt_public(row)
    result["_conversationSyncAttemptEvents"] = (
        [sync_event] if sync_event else []
    )
    return result


def _turn_sync_snapshot(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read the authoritative rows and replay head in one read transaction."""
    conv_id, user_id = _turn_identity(payload)
    conversation = session.fetch_one(
        "SELECT rev,settings_json FROM storage_conversations "
        "WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        return None
    sync_sequence = _conversation_sync_head(session, conv_id, user_id)
    turns = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? ORDER BY ordinal",
        (conv_id, user_id),
    )
    attempts = session.fetch_all(
        "SELECT a.* FROM storage_generation_attempts a "
        "JOIN storage_conversation_turns t ON t.turn_id=a.turn_id "
        "WHERE a.conversation_id=? AND t.user_id=? "
        "ORDER BY a.created_at,a.attempt_id",
        (conv_id, user_id),
    )
    queue_rows = session.fetch_all(
        "SELECT id,payload_json,position,kind,priority,created_at_ms "
        "FROM storage_queue_items WHERE conv_id=? AND user_id=? "
        "ORDER BY priority,position,id",
        (conv_id, user_id),
    )
    queue_items: list[dict[str, Any]] = []
    for row in queue_rows:
        queue_payload = _load(row["payload_json"]) or {}
        user_message = queue_payload.get("_user_msg") or {}
        text = (
            user_message.get("content")
            or queue_payload.get("_peerText")
            or queue_payload.get("text")
            or ""
        )
        item = {
            "queueId": str(row["id"]),
            "position": int(row["position"]),
            "kind": str(row["kind"] or "real"),
            "priority": int(row["priority"]),
            "timestamp": int(
                user_message.get("timestamp")
                or queue_payload.get("timestamp")
                or row["created_at_ms"]
            ),
            "text": str(text),
            "hasImages": bool(queue_payload.get("images")),
            "hasPdfs": bool(queue_payload.get("pdfTexts")),
            "hasRefs": bool(queue_payload.get("convRefs")),
            "hasQuotes": bool(queue_payload.get("replyQuotes")),
        }
        source_message_id = str(
            user_message.get("_msgId") or queue_payload.get("_msgId") or ""
        )
        if source_message_id:
            item["sourceMessageId"] = source_message_id
        if queue_payload.get("_peerMessage"):
            item.update({
                "isPeerMessage": True,
                "fromConv": str(queue_payload.get("_fromConv") or ""),
                "isPeerHuman": bool(queue_payload.get("_peerHuman")),
            })
        queue_items.append(item)
    settings = _load(conversation["settings_json"]) or {}
    if not isinstance(settings, Mapping):
        raise StorageError(
            "database_integrity", "Conversation settings are malformed"
        )
    public_settings = {
        key: value
        for key, value in settings.items()
        if key not in _SYNC_PRIVATE_SETTING_KEYS
    }
    return {
        "conversationId": conv_id,
        "conversationRevision": int(conversation["rev"] or 0),
        "syncSequence": sync_sequence,
        "settings": public_settings,
        "turns": [_turn_public(row) for row in turns],
        "attempts": [_attempt_public(row) for row in attempts],
        "queueItems": queue_items,
    }


def _turn_sync_changes(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read one contiguous ordered conversation-change page after a cursor."""
    conv_id, user_id = _turn_identity(payload)
    after = _integer(payload, "after", default=0, minimum=0)
    limit = _integer(payload, "limit", default=500, minimum=1, maximum=2000)
    conversation = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        return None
    head = _conversation_sync_head(session, conv_id, user_id)
    if after > head:
        return {
            "head": head,
            "events": [],
            "resetRequired": True,
            "resetReason": "cursor_invalid",
        }
    rows = session.fetch_all(
        "SELECT sync_sequence,event_json FROM storage_conversation_changes "
        "WHERE conversation_id=? AND user_id=? AND sync_sequence>? "
        "ORDER BY sync_sequence LIMIT ?",
        (conv_id, user_id, after, limit),
    )
    if head > after and (
        not rows or int(rows[0]["sync_sequence"] or 0) != after + 1
    ):
        return {
            "head": head,
            "events": [],
            "resetRequired": True,
            "resetReason": "cursor_expired",
        }
    events = [_load(row["event_json"]) or {} for row in rows]
    return {
        "head": head,
        "events": events,
        "resetRequired": False,
        "hasMore": bool(rows and int(rows[-1]["sync_sequence"]) < head),
    }


def _turn_sync_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    """Bounded TTL deletion for replay rows; snapshot rows remain authority.

    A cursor older than the retained prefix deterministically receives
    ``sync.reset_required(cursor_expired)`` from ``_turn_sync_changes``.  The
    per-conversation head is never deleted here, so a snapshot taken before or
    after this maintenance transaction always anchors at an exact sequence.
    """
    created_before = _integer(payload, "created_before_ms", minimum=1)
    max_rows = _integer(
        payload, "max_rows", default=512, minimum=1, maximum=20_000
    )
    rows = session.fetch_all(
        "SELECT conversation_id,user_id,sync_sequence "
        "FROM storage_conversation_changes WHERE created_at<? "
        "ORDER BY created_at,conversation_id,user_id,sync_sequence LIMIT ?",
        (created_before, max_rows + 1),
    )
    overflow = len(rows) > max_rows
    selected = rows[:max_rows]
    deleted = 0
    for row in selected:
        deleted += session.execute(
            "DELETE FROM storage_conversation_changes "
            "WHERE conversation_id=? AND user_id=? AND sync_sequence=?",
            (
                row["conversation_id"],
                int(row["user_id"]),
                int(row["sync_sequence"]),
            ),
        )
    return {"deletedRows": deleted, "remaining": bool(overflow)}


def _turn_event_record(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    session.lock_key("attempt_events", attempt_id)
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    if attempt is None or attempt["status"] not in ("pending", "running"):
        return {"applied": False}
    turn = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
        (attempt["turn_id"],),
    )
    if (turn is None or int(turn["user_id"]) != user_id
            or turn["current_attempt_id"] != attempt_id):
        return {"applied": False}
    now = int(payload.get("now") or time.time() * 1000)
    old_revision = int(turn["projection_revision"])
    new_revision = old_revision + 1
    terminal = bool(payload.get("terminal"))
    status = str(payload.get("status") or ("running" if not terminal else "failed"))
    projection = payload.get("projection") or {}
    previous_projection = _load(turn["projection_json"]) or {}
    if not isinstance(previous_projection, Mapping):
        previous_projection = {}
    settlement = payload.get("settlement") or {}
    # DELTA-class writes (delta / program_output / tool_progress) advance only
    # the cumulative content/thinking text.  Re-folding the whole toolRounds
    # list on every allowed delta made the authority write grow O(toolRounds)
    # with the turn length; the full fold is cadence-gated in turn_lifecycle
    # (see _delta_text_fields + _structural_fold_due) and still runs on
    # structural/terminal frames.  The cheap path patches the two text keys in
    # place instead of re-serializing the whole document.
    slim = bool(payload.get("slim")) and not terminal
    if slim:
        content = str(payload.get("content") or "")
        thinking = str(payload.get("thinking") or "")
        next_projection = dict(previous_projection)
        next_projection["content"] = content
        next_projection["thinking"] = thinking
        if session.backend == "postgres":
            projection_expr = (
                "jsonb_set(jsonb_set(projection_json, '{content}', "
                "to_jsonb(?::text), true), '{thinking}', to_jsonb(?::text), true)"
            )
        else:
            projection_expr = (
                "json_set(projection_json, '$.content', ?, '$.thinking', ?)"
            )
        projection_bytes = len(_dump({"content": content, "thinking": thinking}))
        changed = session.execute(
            "UPDATE storage_conversation_turns SET status=?, "
            f"projection_json={projection_expr}, projection_revision=?, "
            "updated_at=? WHERE turn_id=? AND current_attempt_id=? "
            "AND projection_revision=?",
            (
                status,
                content,
                thinking,
                new_revision,
                now,
                turn["turn_id"],
                attempt_id,
                old_revision,
            ),
        )
    else:
        # Serialize once: the turn-row update and the frame's observability
        # byte count share the same bytes.
        projection_json = _dump(projection)
        projection_bytes = len(projection_json)
        changed = session.execute(
            "UPDATE storage_conversation_turns SET status=?, projection_json=?, projection_revision=?, settlement_json=?, updated_at=? WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
            (
                status,
                projection_json,
                new_revision,
                _dump(settlement),
                now,
                turn["turn_id"],
                attempt_id,
                old_revision,
            ),
        )
        next_projection = dict(projection) if isinstance(projection, Mapping) else {}
    if not changed:
        return {"applied": False}
    if terminal:
        session.execute(
            "UPDATE storage_generation_attempts SET status=?, error_json=?, settled_at=? WHERE attempt_id=? AND status IN ('pending','running')",
            (status, _dump(payload.get("error") or {}), now, attempt_id),
        )
    event_type = (
        "terminal_settlement"
        if terminal
        else str(payload.get("event_type") or "projection_updated")
    )
    event_payload = dict(payload.get("event_payload") or {})
    # Compact durable transport (2026-08-23 wire-amplification root fix): the
    # full projection already landed transactionally on the turn row.  Store
    # the exact revision-to-revision patch for replay, including terminal
    # frames, rather than a second full copy.  Legacy readers still receive a
    # hydrated full projection on the requested page tail; patch-aware SSE
    # readers opt out of that expansion.
    event_payload.pop("projection", None)
    event_payload["projectionPatch"] = build_projection_patch(
        previous_projection,
        next_projection,
        base_revision=old_revision,
        target_revision=new_revision,
    )
    event_payload["status"] = status
    event_payload["projectionBytes"] = projection_bytes
    event_result = _turn_event_append(
        session,
        {
            "attempt_id": attempt_id,
            "conversation_id": attempt["conversation_id"],
            "turn_id": turn["turn_id"],
            "projection_revision": new_revision,
            "type": event_type,
            "event": event_payload,
        },
    )
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        (now, attempt["conversation_id"], turn["user_id"]),
    )
    if terminal:
        _upsert_turn_search_row(
            session,
            turn,
            projection=next_projection,
            projection_revision=new_revision,
            status=status,
            updated_at=now,
        )
    # Optional frame carrier (2026-08-20 double-write root fix): a live turn
    # frame historically persisted TWICE per push — once here, once via a
    # separate event.append — two commands, two queue slots, and NO atomicity
    # between the turn authority and the cold-replay log (one could commit
    # while the other timed out; the skew surfaced as "Event sequence has a
    # conflicting payload" on retry).  When the caller attaches the frame's
    # storage_events row, it commits in THIS transaction: one frame = one
    # authority transaction.  A conflict here rolls the whole frame back —
    # fail-closed, the caller withholds the push.
    task_event_result = None
    task_event = payload.get("task_event")
    if task_event is not None:
        if not isinstance(task_event, Mapping):
            raise StorageError("database_protocol_error", "Invalid task event carrier")
        task_event_result = _append_event_row(session, task_event)
    return {
        "applied": True,
        "status": status,
        "projection_revision": new_revision,
        "task_event": task_event_result,
        "_conversationSyncAttemptEvents": [event_result["event"]],
    }

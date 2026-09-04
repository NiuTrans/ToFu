"""Shared turn row projections, metrics, change capture and search-row helpers."""
from __future__ import annotations
from typing import Any
from collections.abc import Mapping
from lib.storage_sidecar.adapters.base import Session
from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
from lib.turn_projection_patch import build_projection_patch
from collections import defaultdict
from lib.log import get_logger
from lib.turn_verdict import normalize_turn_settlement
import threading
import time
import uuid


logger = get_logger(__name__)


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
        "projection_blob_write_skips": 0,
        "projection_blob_write_skipped_bytes": 0,
        "projection_blob_write_deferrals": 0,
        "projection_blob_write_deferred_bytes": 0,
        "projection_checkpoint_materializations": 0,
        "projection_checkpoint_materialized_bytes": 0,
        "projection_inline_released_bytes": 0,
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


def _observe_projection_blob_write_skip(
    event_type: str, projection_bytes: int
) -> None:
    """Count canonical projection bytes kept out of an authority UPDATE."""
    with _attempt_event_metrics_lock:
        metrics = _attempt_event_metrics[event_type]
        metrics["projection_blob_write_skips"] += 1
        metrics["projection_blob_write_skipped_bytes"] += projection_bytes


def _observe_projection_blob_write_deferral(
    event_type: str, projection_bytes: int,
) -> None:
    """Count changed projection bytes retained behind a durable patch head."""
    with _attempt_event_metrics_lock:
        metrics = _attempt_event_metrics[event_type]
        metrics["projection_blob_write_deferrals"] += 1
        metrics["projection_blob_write_deferred_bytes"] += max(
            0, projection_bytes)


def _observe_projection_checkpoint_materialization(
    event_type: str,
    checkpoint_bytes: int,
    inline_released_bytes: int,
) -> None:
    """Count bounded live checkpoint writes and one-time inline releases."""
    with _attempt_event_metrics_lock:
        metrics = _attempt_event_metrics[event_type]
        metrics["projection_checkpoint_materializations"] += 1
        metrics["projection_checkpoint_materialized_bytes"] += max(
            0, checkpoint_bytes)
        metrics["projection_inline_released_bytes"] += max(
            0, inline_released_bytes)


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


def _stored_projection_payload_bytes(raw: Any, decoded: Mapping[str, Any]) -> int:
    """Measure an unchanged stored projection without re-encoding on SQLite.

    Psycopg decodes JSONB before the semantic operation sees it, so that lane
    uses the same canonical codec as a write. SQLite exposes the exact bytes
    already resident in the row and can avoid both serialization and BLOB I/O.
    """
    if isinstance(raw, memoryview):
        return len(raw)
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    return len(_dump(decoded))


_CONVERSATION_SYNC_EVENT_CONTRACT = "tofu.conversation-sync.event/v1"


_STORAGE_COMMITTED_EVENTS_CONTRACT = "storage.committed-events/v1"


_SYNC_PRIVATE_SETTING_KEYS = frozenset()


_TURN_CHANGE_CAPTURE_OPERATIONS = frozenset({
    "turn.append_settled",
    "turn.attempt.bind",
    "turn.attempt.create",
    "turn.attempt.dispatch_worker",
    "turn.attempt.start",
    "turn.branch.create",
    "turn.branch.delete",
    "turn.compact",
    "turn.create_pair",
    "turn.queue.activate",
    "turn.queue.cancel",
    "turn.steer.commit",
    "turn.delete",
    "turn.event.record",
    "turn.projection.update",
    "turn.recover",
    "turn.related.announce",
    "turn.visible.sync",
})


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


def _conversation_change_envelope(
    *,
    conversation_id: str,
    sync_sequence: int,
    change_type: str,
    occurred_at: int,
    payload: Mapping[str, Any],
    turn_id: str = "",
    attempt_id: str = "",
) -> dict[str, Any]:
    """Build the single public envelope used by writes and reference reads."""
    event: dict[str, Any] = {
        "contract": _CONVERSATION_SYNC_EVENT_CONTRACT,
        "type": change_type,
        "conversationId": conversation_id,
        "syncSeq": sync_sequence,
        "occurredAt": occurred_at,
        "payload": dict(payload),
    }
    if turn_id:
        event["turnId"] = turn_id
    if attempt_id:
        event["attemptId"] = attempt_id
    return event


def _append_conversation_change(
    session: Session,
    *,
    conversation_id: str,
    user_id: int,
    change_type: str,
    payload: Mapping[str, Any],
    turn_id: str = "",
    attempt_id: str = "",
    attempt_sequence: int | None = None,
    occurred_at: int | None = None,
) -> dict[str, Any]:
    """Append one ordered sync event in the caller's mutation transaction."""
    if attempt_sequence is not None and (
        isinstance(attempt_sequence, bool)
        or not isinstance(attempt_sequence, int)
        or attempt_sequence < 1
        or change_type != "attempt.event"
        or not turn_id
        or not attempt_id
    ):
        raise StorageError(
            "database_protocol_error", "Invalid attempt-event change reference")
    session.lock_key("conversation_sync", f"{user_id}:{conversation_id}")
    now = int(occurred_at or time.time() * 1000)
    sequence = _conversation_sync_head(session, conversation_id, user_id) + 1
    event = _conversation_change_envelope(
        conversation_id=conversation_id,
        sync_sequence=sequence,
        change_type=change_type,
        occurred_at=now,
        payload=payload,
        turn_id=turn_id,
        attempt_id=attempt_id,
    )
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
        "(conversation_id,user_id,sync_sequence,change_type,turn_id,attempt_id,"
        "attempt_sequence,event_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            conversation_id,
            user_id,
            sequence,
            change_type,
            turn_id,
            attempt_id,
            attempt_sequence,
            _dump({}) if attempt_sequence is not None else _dump(event),
            now,
        ),
    )
    return event


def _conversation_change_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode an inline change or hydrate one exact AttemptEvent reference."""
    attempt_sequence = row["attempt_sequence"]
    if attempt_sequence is None:
        event = _load(row["event_json"])
        if not isinstance(event, Mapping):
            raise StorageError(
                "database_integrity", "Stored conversation change is invalid")
        return dict(event)
    if (
        isinstance(attempt_sequence, bool)
        or not isinstance(attempt_sequence, int)
        or attempt_sequence < 1
        or str(row["change_type"]) != "attempt.event"
    ):
        raise StorageError(
            "database_integrity", "Stored attempt-event change reference is invalid")
    attempt_event = _load(row["attempt_event_json"])
    event_sequence = (
        attempt_event.get("seq") if isinstance(attempt_event, Mapping) else None
    )
    if (
        not isinstance(attempt_event, Mapping)
        or not isinstance(attempt_event.get("payload"), Mapping)
        or isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or event_sequence != attempt_sequence
        or str(attempt_event.get("conversationId") or "")
        != str(row["conversation_id"])
        or str(attempt_event.get("turnId") or "") != str(row["turn_id"])
        or str(attempt_event.get("attemptId") or "") != str(row["attempt_id"])
    ):
        raise StorageError(
            "database_integrity", "Attempt-event change reference is unresolved")
    return _conversation_change_envelope(
        conversation_id=str(row["conversation_id"]),
        sync_sequence=int(row["sync_sequence"]),
        change_type="attempt.event",
        occurred_at=int(row["created_at"]),
        payload={"event": dict(attempt_event)},
        turn_id=str(row["turn_id"]),
        attempt_id=str(row["attempt_id"]),
    )


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

    if operation_name in {
        "turn.create_pair", "turn.append_settled", "turn.queue.activate",
    }:
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
        if isinstance(clean_result.get("queueItem"), Mapping):
            event_payload["queueItemUpserts"] = [
                dict(clean_result["queueItem"])
            ]
        if operation_name == "turn.queue.activate" and clean_result.get("queueId"):
            event_payload["removedQueueIds"] = [str(clean_result["queueId"])]
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
        "turn.steer.commit",
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
                        **({
                            "removedQueueIds": [str(clean_result["queueId"])]
                        } if operation_name == "turn.queue.cancel"
                            and clean_result.get("queueId") else {}),
                        "conversationRevision": int(
                            clean_result.get("conversationRevision") or 0
                        ),
                    },
                ),
                user_id,
            ))

    elif operation_name in {"turn.delete", "turn.queue.cancel"}:
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
                        **({
                            "removedQueueIds": [str(clean_result["queueId"])]
                        } if operation_name == "turn.queue.cancel"
                            and clean_result.get("queueId") else {}),
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
        "turn.attempt.start",
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
        attempt_sequence = attempt_event.get("seq")
        attempt_payload = attempt_event.get("payload")
        if (
            isinstance(attempt_sequence, bool)
            or not isinstance(attempt_sequence, int)
            or attempt_sequence < 1
            or not isinstance(attempt_payload, Mapping)
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
                attempt_sequence=attempt_sequence,
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


def _stored_object(value: Any, field_name: str) -> dict[str, Any]:
    """Decode one object-valued authority field without falsy coercion."""
    loaded = _load(value)
    if not isinstance(loaded, Mapping):
        raise StorageError(
            "database_integrity", f"Stored {field_name} must be an object")
    return dict(loaded)


def _optional_row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read optional columns from dict rows and stdlib ``sqlite3.Row`` alike."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _turn_public(
    session: Session, row: Mapping[str, Any],
) -> dict[str, Any]:
    stored_status = str(row["status"])
    raw_settlement = _stored_object(row["settlement_json"], "turn settlement")
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
        "presentationId": str(
            _optional_row_value(row, "presentation_id") or row["turn_id"]
        ),
        "conversationId": str(row["conversation_id"]),
        "laneId": str(row["lane_id"] or "main"),
        "parentTurnId": row["parent_turn_id"],
        "ordinal": int(row["ordinal"]),
        "actor": str(row["actor"]),
        "kind": str(row["kind"] or "reply"),
        "runId": str(row["run_id"] or ""),
        "status": status,
        "currentAttemptId": row["current_attempt_id"],
        "projection": projection_from_turn_row(session, row),
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
    resume_anchor = _stored_object(
        row["resume_anchor_json"], "attempt resume anchor")
    public = {
        "attemptId": str(row["attempt_id"]),
        "conversationId": str(row["conversation_id"]),
        "turnId": str(row["turn_id"]),
        "commandId": str(row["command_id"]),
        "taskId": str(row["task_id"] or ""),
        "operation": str(row["operation"]),
        "status": str(row["status"]),
        "baseProjectionRevision": int(row["base_projection_revision"] or 0),
        "resumeAnchor": resume_anchor,
        "createdAt": int(row["created_at"]),
        "startedAt": row["started_at"],
        "settledAt": row["settled_at"],
    }
    queue_id = str(_optional_row_value(row, "queue_id") or "")
    queue_state = str(_optional_row_value(row, "queue_state") or "")
    if queue_id and queue_state == "pending" and row["status"] == "pending":
        public["queueBinding"] = {"queueId": queue_id, "state": "pending"}
    return public


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

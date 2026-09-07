"""Turn compaction, deletion, recovery and cleanup handlers."""
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
from lib.storage_sidecar.turn_projection_head import (
    discard_projection_cache_for_row,
    projection_from_turn_row,
    projection_head_state,
)
from lib.storage_sidecar.projection_codec import (
    STORAGE_PROJECTION_MAX_HYDRATION_RATIO,
    projection_hydration_byte_upper_bound,
)
from lib.storage_sidecar.turn_projection_cache import (
    PROJECTION_CACHE_CHARGE_MULTIPLIER,
)
from lib.storage_sidecar.turn_projection_write import (
    delete_turn_projection_checkpoint,
)
from lib.turn_projection_patch import (
    build_projection_patch,
    normalize_projection_document,
)
import time
from lib.storage_sidecar.operations_pkg._turns_core import _delete_turn_search_rows, _turn_identity, _turn_public, _upsert_turn_search_row
from lib.storage_sidecar.operations_pkg._turns_events import _insert_attempt_event
from lib.storage_sidecar.operations_pkg._turns_read import _prune_turn_tombstones


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
            projection = projection_from_turn_row(session, row)
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


def _expire_change_prefix_for_deleted_turns(
    session: Session,
    conv_id: str,
    user_id: int,
    turn_ids: list[str],
) -> None:
    """Advance replay past references whose AttemptEvents must be deleted."""
    marks = ",".join("?" for _ in turn_ids)
    row = session.fetch_one(
        "SELECT MAX(c.sync_sequence) AS replay_floor "
        "FROM storage_conversation_changes AS c "
        "JOIN storage_generation_attempts AS a ON a.attempt_id=c.attempt_id "
        "WHERE c.conversation_id=? AND c.user_id=? "
        "AND c.attempt_sequence IS NOT NULL AND a.turn_id IN (" + marks + ")",
        (conv_id, user_id, *turn_ids),
    )
    replay_floor = row["replay_floor"] if row is not None else None
    if replay_floor is None:
        return
    session.execute(
        "DELETE FROM storage_conversation_changes "
        "WHERE conversation_id=? AND user_id=? AND sync_sequence<=?",
        (conv_id, user_id, int(replay_floor)),
    )


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
        # Avoid a dangling replay reference without copying the deleted
        # attempt's potentially multi-MiB event back into the sync table.
        _expire_change_prefix_for_deleted_turns(
            session, conv_id, user_id, chunk)
        session.execute(
            "DELETE FROM storage_raw_archives WHERE user_id=? AND turn_id IN ("
            + marks + ")",
            tuple([user_id, *chunk]),
        )
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
            "DELETE FROM storage_turn_projection_checkpoints WHERE turn_id IN ("
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
            "projection_revision=?,projection_checkpoint_revision=NULL,"
            "projection_materialized_revision=NULL,"
            "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
            "WHERE turn_id=? AND projection_revision=?",
            (
                _dump(projection), projection_revision + 1, now,
                row["turn_id"], projection_revision,
            ),
        )
        if changed != 1:
            raise StorageError(
                "database_conflict", "A retained turn changed during compaction")
        delete_turn_projection_checkpoint(session, str(row["turn_id"]))
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
        "turn": _turn_public(session, summary_row),
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


def _turn_recovery_projection_budget(row: Mapping[str, Any]) -> int:
    """Charge a live inline/checkpoint base without selecting checkpoint JSON."""
    head = projection_head_state(row)
    if head.checkpoint_active:
        checkpoint_bytes = row["projection_checkpoint_bytes"]
        if (
            isinstance(checkpoint_bytes, bool)
            or not isinstance(checkpoint_bytes, int)
            or checkpoint_bytes < 1
        ):
            raise StorageError(
                "database_integrity",
                "Turn projection checkpoint byte evidence is missing",
            )
        base_bytes = (
            checkpoint_bytes * STORAGE_PROJECTION_MAX_HYDRATION_RATIO)
    else:
        base_bytes = projection_hydration_byte_upper_bound(
            row["projection_json"])
    return (
        base_bytes
        + head.patch_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER
    )


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
    max_bytes = _integer(
        payload, "max_bytes", default=2_000_000, minimum=1,
        maximum=8 * 1024 * 1024,
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
        "SELECT a.*,t.user_id,t.current_attempt_id,t.projection_json,"
        "t.projection_revision,t.projection_checkpoint_revision,"
        "t.projection_materialized_revision,"
        "t.projection_patch_count,t.projection_patch_bytes,"
        "cp.projection_bytes AS projection_checkpoint_bytes "
        "FROM storage_generation_attempts a JOIN storage_conversation_turns t "
        "ON t.turn_id=a.turn_id AND t.current_attempt_id=a.attempt_id "
        "LEFT JOIN storage_turn_projection_checkpoints cp ON "
        "cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id "
        "AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id "
        "AND cp.projection_revision=t.projection_checkpoint_revision "
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
        projection_len = _turn_recovery_projection_budget(row)
        if chunk_rows and (
            chunk_rows >= max_rows or chunk_bytes + projection_len > max_bytes
        ):
            remaining += 1
            continue
        projection = projection_from_turn_row(session, row)
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
        turn_changed = session.execute(
            "UPDATE storage_conversation_turns SET status='interrupted',"
            "projection_json=?,projection_revision=?,"
            "projection_checkpoint_revision=NULL,"
            "projection_materialized_revision=NULL,projection_patch_count=0,"
            "projection_patch_bytes=0,settlement_json=?,updated_at=? "
            "WHERE turn_id=? AND current_attempt_id=? "
            "AND projection_revision=?",
            (
                _dump(projection),
                new_revision,
                _dump(settlement),
                now,
                row["turn_id"],
                row["attempt_id"],
                old_revision,
            ),
        )
        if turn_changed != 1:
            raise StorageError(
                "database_conflict",
                "Turn changed while restart recovery was materialized",
            )
        delete_turn_projection_checkpoint(session, str(row["turn_id"]))
        discard_projection_cache_for_row(session, row)
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
        "AND NOT EXISTS (SELECT 1 FROM storage_conversation_changes c "
        "WHERE c.attempt_id=a.attempt_id AND c.attempt_sequence IS NOT NULL) "
        "ORDER BY a.superseded_at LIMIT ?",
        (cutoff, limit),
    )
    for row in rows:
        session.execute(
            "DELETE FROM storage_raw_archives WHERE attempt_id=?",
            (row["attempt_id"],),
        )
        session.execute(
            "DELETE FROM storage_attempt_events WHERE attempt_id=?",
            (row["attempt_id"],),
        )
        session.execute(
            "DELETE FROM storage_generation_attempts WHERE attempt_id=?",
            (row["attempt_id"],),
        )
    return len(rows)

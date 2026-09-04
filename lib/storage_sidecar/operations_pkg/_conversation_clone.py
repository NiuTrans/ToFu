"""Atomic, inert conversation snapshot cloning.

This slice owns clone-specific identity remapping and live-presentation
terminalization.  The source graph remains executable; the destination is a
self-contained historical snapshot and never receives attempts or run latches.
"""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any
import uuid

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._turns import (
    _mark_conversation_search_projection_dirty,
)
from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
from lib.turn_projection_segments import projection_with_stable_segments


_LIVE_TURN_STATUSES = frozenset({"pending", "running"})
_CLONE_RUNTIME_KEYS = frozenset({
    "_activeAttemptId",
    "_attemptId",
    "_authoritativeActiveTaskIds",
    "_commandPending",
    "_flowRunId",
    "_needsStart",
    "_runId",
    "_streamCursor",
    "_streaming",
    "activeTaskId",
    "approvalRequired",
    "attemptId",
    "isStreaming",
    "runId",
})
_CLONE_TASK_ID_KEYS = frozenset({
    "_proactiveTaskId",
    "_taskId",
    "_translateTaskId",
    "taskId",
})
_TERMINAL_TOOL_STATUSES = frozenset({
    "abort",
    "aborted",
    "completed",
    "done",
    "error",
    "failed",
    "not-run",
    "not_run",
    "rejected",
    "skipped",
    "succeeded",
    "success",
    "unknown",
})


def settings_without_live_runtime(raw: Any) -> dict[str, Any]:
    """Return settings safe to hydrate as a non-executable conversation."""
    settings = _load(raw) or {}
    if not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation settings are malformed")
    result = dict(settings)
    result.pop("activeTaskId", None)
    result.pop("_activeAttemptId", None)
    return result


def _clone_projection_value(
    value: Any,
    *,
    turn_ids: Mapping[str, str],
    archive_ids: Mapping[str, str],
    task_ids: dict[str, str],
) -> Any:
    """Copy presentation data while severing executable identities."""
    if isinstance(value, Mapping):
        cloned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _CLONE_RUNTIME_KEYS:
                continue
            if key in {"_turnId", "turnId"} and isinstance(item, str):
                replacement = turn_ids.get(item)
                if replacement:
                    cloned[key] = replacement
                continue
            if key in {"archiveId", "_compactionArchiveId"} \
                    and isinstance(item, str):
                cloned[key] = archive_ids.get(item, item)
                continue
            if key in _CLONE_TASK_ID_KEYS and isinstance(item, str) and item:
                cloned[key] = task_ids.setdefault(
                    item, f"clone-task-{uuid.uuid4().hex}"
                )
                continue
            cloned[key] = _clone_projection_value(
                item,
                turn_ids=turn_ids,
                archive_ids=archive_ids,
                task_ids=task_ids,
            )
        return cloned
    if isinstance(value, list):
        return [
            _clone_projection_value(
                item,
                turn_ids=turn_ids,
                archive_ids=archive_ids,
                task_ids=task_ids,
            )
            for item in value
        ]
    return value


def _aborted_tool_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _TERMINAL_TOOL_STATUSES else "aborted"


def _terminalize_live_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze every live presentation carrier without inventing tool results."""
    frozen = dict(projection)
    raw_rounds = frozen.get("toolRounds")
    if isinstance(raw_rounds, list):
        rounds: list[Any] = []
        for source in raw_rounds:
            if not isinstance(source, Mapping):
                rounds.append(source)
                continue
            round_record = dict(source)
            round_record["status"] = _aborted_tool_status(
                round_record.get("status")
            )
            rounds.append(round_record)
        frozen["toolRounds"] = rounds

    raw_segments = frozen.get("segments")
    if isinstance(raw_segments, list):
        segments: list[Any] = []
        for source in raw_segments:
            if not isinstance(source, Mapping) or source.get("type") != "tool_use":
                segments.append(source)
                continue
            segment = dict(source)
            raw_result = segment.get("result")
            if isinstance(raw_result, Mapping):
                result = dict(raw_result)
                result["status"] = _aborted_tool_status(result.get("status"))
                segment["result"] = result
            segments.append(segment)
        frozen["segments"] = segments

    raw_trace = frozen.get("timingTrace")
    if isinstance(raw_trace, Mapping):
        trace = dict(raw_trace)
        trace["status"] = "aborted"
        trace["running"] = False
        frozen["timingTrace"] = trace

    return projection_with_stable_segments(
        frozen,
        actor="assistant",
        status="interrupted",
    )


def _locked_source_turns(
    session: Session, source_id: str, user_id: int,
) -> list[Mapping[str, Any]]:
    statement = (
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal"
    )
    if session.backend == "postgres":
        # The conversation advisory lock fences turn creation. Row locks fence
        # existing attempt-event writers, yielding one coherent revision set.
        statement += " FOR UPDATE"
    return session.fetch_all(statement, (source_id, user_id))


def conversation_clone(
    session: Session,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    user_id: int,
) -> Any:
    """Clone the latest durable source graph into an inert destination."""
    destination_id = _required_text(payload, "destination_conv_id", 256)
    lock_ids = sorted({source_id, destination_id})
    for lock_id in lock_ids:
        session.lock_key("conversation", f"{user_id}:{lock_id}")
    for lock_id in lock_ids:
        session.lock_key("turn_conversation", f"{user_id}:{lock_id}")

    source = session.fetch_one(
        "SELECT * FROM storage_conversations WHERE id=? AND user_id=?",
        (source_id, user_id),
    )
    if source is None:
        return {"cloned": False, "missing": True, "busy": False}
    destination_taken = (
        session.fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=?",
            (destination_id,),
        ) is not None
        or session.fetch_one(
            "SELECT 1 AS present FROM storage_conversation_trash "
            "WHERE conversation_id=?",
            (destination_id,),
        ) is not None
    )
    if destination_taken:
        raise StorageError("database_conflict", "Destination conversation exists")

    raw_title = payload.get("title")
    if raw_title is None:
        title = f"{str(source['title'] or 'Untitled')} (copy)"
    elif not isinstance(raw_title, str) or not raw_title.strip():
        raise StorageError("database_protocol_error", "Invalid clone title")
    else:
        title = raw_title.strip()

    now = int(time.time() * 1000)
    source_turns = _locked_source_turns(session, source_id, user_id)
    turn_ids = {
        str(turn["turn_id"]): str(uuid.uuid4()) for turn in source_turns
    }
    source_archives = session.fetch_all(
        "SELECT archive_id,task_id FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=? ORDER BY created_at_ms,archive_id",
        (source_id, user_id),
    )
    archive_ids = {
        str(row["archive_id"]): f"{time.time_ns():020d}_{uuid.uuid4().hex}"
        for row in source_archives
    }
    task_ids: dict[str, str] = {}
    settings = settings_without_live_runtime(source["settings_json"])
    settings["clonedFrom"] = source_id
    main_count = sum(
        1 for turn in source_turns if str(turn["lane_id"] or "main") == "main"
    )
    session.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        (
            destination_id,
            user_id,
            title[:500],
            _dump([]),
            now,
            now,
            _dump(settings),
            main_count,
            "",
        ),
    )

    for turn in source_turns:
        old_turn_id = str(turn["turn_id"])
        raw_projection = projection_from_turn_row(session, turn)
        if not isinstance(raw_projection, Mapping):
            raise StorageError("database_integrity", "Turn projection is malformed")
        projection = _clone_projection_value(
            raw_projection,
            turn_ids=turn_ids,
            archive_ids=archive_ids,
            task_ids=task_ids,
        )
        source_status = str(turn["status"] or "")
        is_live = source_status in _LIVE_TURN_STATUSES
        if is_live:
            projection = _terminalize_live_projection(projection)
            clone_status = "interrupted"
            settlement = {
                "outcome": "interrupted",
                "cause": "conversation_cloned_snapshot",
                "resumeOptions": [],
            }
        else:
            clone_status = source_status
            settlement = _load(turn["settlement_json"]) or {}
            if not isinstance(settlement, Mapping):
                raise StorageError(
                    "database_integrity", "Turn settlement is malformed"
                )
            settlement = dict(settlement)
        parent_id = turn["parent_turn_id"]
        mapped_parent = turn_ids.get(str(parent_id)) if parent_id else None
        session.execute(
            "INSERT INTO storage_conversation_turns("
            "turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,"
            "actor,kind,run_id,status,current_attempt_id,projection_json,"
            "projection_revision,settlement_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                turn_ids[old_turn_id],
                destination_id,
                user_id,
                str(turn["lane_id"] or "main"),
                mapped_parent,
                int(turn["ordinal"]),
                str(turn["actor"]),
                str(turn["kind"] or "reply"),
                "",
                clone_status,
                None,
                _dump(projection),
                1,
                _dump(settlement),
                int(turn["created_at"]),
                now if is_live else int(turn["updated_at"]),
            ),
        )
    _mark_conversation_search_projection_dirty(session, destination_id, user_id)

    for archive in source_archives:
        old_archive_id = str(archive["archive_id"])
        old_task_id = str(archive["task_id"] or "")
        new_task_id = (
            task_ids.setdefault(old_task_id, f"clone-task-{uuid.uuid4().hex}")
            if old_task_id else ""
        )
        session.execute(
            "INSERT INTO storage_compaction_archives("
            "archive_id,conversation_id,user_id,messages_json,summary,receipt_json,trigger,"
            "task_id,round_num,model,tokens_before,tokens_after,msgs_before,"
            "msgs_after,reason,payload_size,created_at_ms) "
            "SELECT ?,?,?,messages_json,summary,receipt_json,trigger,?,round_num,model,"
            "tokens_before,tokens_after,msgs_before,msgs_after,reason,"
            "payload_size,created_at_ms FROM storage_compaction_archives "
            "WHERE archive_id=? AND conversation_id=? AND user_id=?",
            (
                archive_ids[old_archive_id],
                destination_id,
                user_id,
                new_task_id,
                old_archive_id,
                source_id,
                user_id,
            ),
        )
    return {
        "cloned": True,
        "missing": False,
        "busy": False,
        "conversationId": destination_id,
        "turnCount": len(source_turns),
        "archiveCount": len(source_archives),
        "rev": 0,
    }

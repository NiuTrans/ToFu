"""Turn/attempt creation, projection update and event-record handlers."""
from __future__ import annotations
from typing import Any
from collections.abc import Mapping
from lib.storage_sidecar.adapters.base import Session
from lib.storage.errors import StorageError
from lib.conversation_sync.dispatch_contract import (
    ATTEMPT_DISPATCH_MODES,
)
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
from lib.storage_sidecar.turn_projection_write import (
    advance_unchanged_projection_revision,
    delete_turn_projection_checkpoint,
)
from lib.turn_projection_patch import (
    build_projection_patch,
    normalize_projection_document,
)
from lib.turn_source_queue_contract import (
    KIND_GOAL_CONTINUATION,
    KIND_REAL,
)
from lib.tool_round_identity import projection_history_with_execution_identity
import time
import uuid
from lib.storage_sidecar.operations_pkg._turns_core import _attempt_public, _projection_change, _turn_identity, _turn_public, _upsert_turn_search_row
from lib.storage_sidecar.operations_pkg._turns_events import _insert_attempt_event, _turn_event_append
from lib.storage_sidecar.operations_pkg._turns_lifecycle import _delete_turn_row_set, _turn_deletion_closure
from lib.storage_sidecar.operations_pkg._turns_read import _prune_turn_tombstones, _turn_revision
from lib.storage_sidecar.operations_pkg._queue_shared import (
    renumber_queue_positions,
)
from lib.storage_sidecar.operations_pkg._queue import _queue_item


def _attempt_dispatch_mode(payload: Mapping[str, Any]) -> str:
    dispatch_mode = str(payload.get("dispatch_mode") or "")
    if dispatch_mode not in ATTEMPT_DISPATCH_MODES:
        raise StorageError(
            "database_protocol_error", "Invalid conversation attempt dispatch mode"
        )
    return dispatch_mode


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
    dispatch_mode = _attempt_dispatch_mode(payload)
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
    raw_queue_binding = payload.get("queue_binding") or {}
    if not isinstance(raw_queue_binding, Mapping):
        raise StorageError("database_protocol_error", "Invalid queue binding")
    queue_id = str(raw_queue_binding.get("queueId") or "").strip()
    if queue_id and (len(queue_id) > 256 or input_actor != "human"):
        raise StorageError("database_protocol_error", "Invalid queue binding")
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
        queue_row = None
        if str(attempt.get("queue_state") or "") == "pending":
            queue_row = session.fetch_one(
                "SELECT * FROM storage_queue_items WHERE id=? AND user_id=?",
                (str(attempt.get("queue_id") or ""), user_id),
            )
        result = {
            "submittedTurn": (
                _turn_public(session, submitted) if submitted is not None else None),
            "turn": _turn_public(session, turn),
            "attempt": _attempt_public(attempt),
            "conversationRevision": _turn_revision(session, payload),
            "streamCursor": 1,
            "idempotentReplay": True,
            # A command can commit and lose its ACK before the route claims
            # dispatch.  Replaying that exact command must finish the launch;
            # claim_attempt_start remains the single-winner guard once a task
            # is already dispatching or bound.
            "_needsStart": (
                attempt["status"] == "pending" and not attempt["task_id"]
                and str(attempt.get("queue_state") or "") != "pending"
            ),
        }
        if queue_row is not None:
            result.update({
                "queued": True,
                "queueId": str(queue_row["id"]),
                "position": int(queue_row["position"]),
                "queueItem": _queue_item(queue_row),
            })
        return result
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
    if (input_actor == "human" and not queue_id) or bool(payload.get('require_lane_idle')):
        live = session.fetch_one(
            "SELECT t.* FROM storage_conversation_turns AS t "
            "JOIN storage_generation_attempts AS a "
            "ON a.attempt_id=t.current_attempt_id "
            "WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? "
            "AND a.status IN ('pending','running') AND a.queue_state='' "
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
    if input_actor == "human" or bool(payload.get("reject_if_human_queued")):
        # Queue and turn creation share this lock so a leased continuation
        # cannot race a newer durable human intent past the lane fence.
        session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    if bool(payload.get("reject_if_human_queued")):
        waiting_human = session.fetch_one(
            "SELECT id FROM storage_queue_items "
            "WHERE conv_id = ? AND user_id = ? AND kind = ? LIMIT 1",
            (conv_id, user_id, KIND_REAL),
        )
        if waiting_human is not None:
            raise StorageError(
                "turn_superseded_by_human",
                "A newer human turn superseded this Goal continuation.",
            )
    if input_actor == "human":
        # Direct acceptance is the other half of queue.enqueue's supersession
        # rule: a stale synthetic continuation must never run after this turn.
        deleted = session.execute(
            "DELETE FROM storage_queue_items "
            "WHERE conv_id = ? AND user_id = ? AND kind = ?",
            (conv_id, user_id, KIND_GOAL_CONTINUATION),
        )
        if deleted:
            renumber_queue_positions(session, conv_id, user_id)
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
    input_presentation_id = str(
        payload.get("input_presentation_id") or f"{command_id}:input"
    )
    output_presentation_id = str(
        payload.get("output_presentation_id") or f"{command_id}:output"
    )
    if len(input_presentation_id) > 512 or len(output_presentation_id) > 512:
        raise StorageError("database_protocol_error", "Invalid presentation identity")
    submitted_settlement = {
        "outcome": "completed",
        "cause": "submitted" if input_actor == "human" else "orchestration_generated",
        "resumeOptions": [],
    }
    session.execute(
        "INSERT INTO storage_conversation_turns "
        "(turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            input_turn_id,
            conv_id,
            user_id,
            input_presentation_id,
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
        "(turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            output_turn_id,
            conv_id,
            user_id,
            output_presentation_id,
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
        "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,"
        "dispatch_mode,queue_id,queue_state,status,base_projection_revision,resume_anchor_json,"
        "config_json,error_json,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            conv_id,
            output_turn_id,
            command_id,
            "",
            "generate",
            dispatch_mode,
            queue_id,
            "pending" if queue_id else "",
            "pending",
            0,
            _dump({}),
            _dump(payload.get("config") or {}),
            _dump({}),
            now,
        ),
    )
    queue_row = None
    if queue_id:
        existing_queue_id = session.fetch_one(
            "SELECT id FROM storage_queue_items WHERE id=?", (queue_id,)
        )
        if existing_queue_id is not None:
            raise StorageError("database_conflict", "Queue identity already exists")
        queue_rows = session.fetch_all(
            "SELECT id FROM storage_queue_items "
            "WHERE conv_id=? AND user_id=? ORDER BY priority,position",
            (conv_id, user_id),
        )
        queue_position = len(queue_rows) + 1
        queue_message = raw_queue_binding.get("message")
        if not isinstance(queue_message, Mapping):
            queue_message = {
                "text": str(input_projection.get("content") or ""),
                "_user_msg": dict(input_projection),
            }
        queue_kind = str(raw_queue_binding.get("kind") or KIND_REAL)
        if queue_kind not in {KIND_REAL, KIND_GOAL_CONTINUATION}:
            raise StorageError("database_protocol_error", "Invalid queued turn kind")
        queue_priority = _integer(
            raw_queue_binding, "priority", default=100, minimum=0, maximum=1000,
        )
        queue_created_at = _integer(
            raw_queue_binding, "createdAt", default=now, minimum=0,
        )
        session.execute(
            "INSERT INTO storage_queue_items "
            "(id,user_id,conv_id,payload_json,config_json,position,kind,priority,"
            "created_at_ms,input_turn_id,output_turn_id,attempt_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                queue_id, user_id, conv_id, _dump(dict(queue_message)),
                _dump(payload.get("config") or {}), queue_position, queue_kind,
                queue_priority, queue_created_at, input_turn_id, output_turn_id,
                attempt_id,
            ),
        )
        queue_row = session.fetch_one(
            "SELECT * FROM storage_queue_items WHERE id=? AND user_id=?",
            (queue_id, user_id),
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
    result = {
        "submittedTurn": _turn_public(session, input_row),
        "turn": _turn_public(session, output_row),
        "attempt": _attempt_public(attempt_row),
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": False,
        "_needsStart": not queue_id,
    }
    if queue_row is not None:
        result.update({
            "queued": True,
            "queueId": queue_id,
            "position": int(queue_row["position"]),
            "queueItem": _queue_item(queue_row),
        })
    return result


def _turn_queue_activate(session: Session, payload: Mapping[str, Any]) -> Any:
    """Activate the exact pending Attempt already owned by a queue row."""
    conv_id, user_id = _turn_identity(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    queue_row = session.fetch_one(
        "SELECT * FROM storage_queue_items "
        "WHERE id=? AND conv_id=? AND user_id=?",
        (queue_id, conv_id, user_id),
    )
    if queue_row is None or not str(queue_row.get("attempt_id") or ""):
        raise StorageError("database_not_found", "Queued turn not found")
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts "
        "WHERE attempt_id=? AND conversation_id=? AND queue_id=? "
        "AND queue_state='pending' AND status='pending' AND task_id=''",
        (queue_row["attempt_id"], conv_id, queue_id),
    )
    if attempt is None:
        raise StorageError("database_conflict", "Queued attempt is no longer pending")
    output_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns "
        "WHERE turn_id=? AND conversation_id=? AND user_id=?",
        (queue_row["output_turn_id"], conv_id, user_id),
    )
    input_row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns "
        "WHERE turn_id=? AND conversation_id=? AND user_id=?",
        (queue_row["input_turn_id"], conv_id, user_id),
    )
    if output_row is None or input_row is None:
        raise StorageError("database_integrity", "Queued turn pair is incomplete")
    live = session.fetch_one(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.current_attempt_id=a.attempt_id "
        "WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? "
        "AND a.status IN ('pending','running') AND a.queue_state='' LIMIT 1",
        (conv_id, user_id, output_row["lane_id"]),
    )
    if live is not None:
        raise StorageError(
            "turn_in_progress", "This lane already has a live generation attempt."
        )
    session.execute(
        "UPDATE storage_generation_attempts SET queue_id='',queue_state='' "
        "WHERE attempt_id=?", (attempt["attempt_id"],),
    )
    session.execute(
        "DELETE FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        (queue_id, conv_id, user_id),
    )
    renumber_queue_positions(session, conv_id, user_id)
    now = int(time.time() * 1000)
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    revision = int(conversation["rev"] or 0) + 1
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? "
        "WHERE id=? AND user_id=?", (revision, now, conv_id, user_id),
    )
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
        (attempt["attempt_id"],),
    )
    return {
        "submittedTurn": _turn_public(session, input_row),
        "turn": _turn_public(session, output_row),
        "attempt": _attempt_public(attempt),
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": False,
        "queued": False,
        "queueId": queue_id,
        "_needsStart": True,
    }


def _turn_queue_cancel(session: Session, payload: Mapping[str, Any]) -> Any:
    """Delete a pending queue row and its never-started Turn pair atomically."""
    conv_id, user_id = _turn_identity(payload)
    queue_id = _required_text(payload, "queue_id", 256)
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    session.lock_key("queue-conversation", f"{user_id}:{conv_id}")
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        raise StorageError("database_not_found", "Conversation not found")
    queue_row = session.fetch_one(
        "SELECT * FROM storage_queue_items "
        "WHERE id=? AND conv_id=? AND user_id=?",
        (queue_id, conv_id, user_id),
    )
    if queue_row is None:
        return {
            "conversationId": conv_id,
            "conversationRevision": int(conversation["rev"] or 0),
            "queueId": queue_id,
            "cancelled": False,
            "inputTurn": None,
            "deletedTurnIds": [],
        }
    input_turn_id = str(queue_row.get("input_turn_id") or "")
    output_turn_id = str(queue_row.get("output_turn_id") or "")
    attempt_id = str(queue_row.get("attempt_id") or "")
    if not input_turn_id or not output_turn_id or not attempt_id:
        raise StorageError(
            "database_conflict", "Legacy queue rows use the compatibility cancel adapter"
        )
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts "
        "WHERE attempt_id=? AND conversation_id=? AND queue_id=? "
        "AND queue_state='pending' AND status='pending' AND task_id=''",
        (attempt_id, conv_id, queue_id),
    )
    if attempt is None:
        raise StorageError("database_conflict", "Queued attempt is no longer cancellable")
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND turn_id IN (?,?)",
        (conv_id, user_id, input_turn_id, output_turn_id),
    )
    rows_by_id = {str(row["turn_id"]): row for row in rows}
    if set(rows_by_id) != {input_turn_id, output_turn_id}:
        raise StorageError("database_integrity", "Queued turn pair is incomplete")
    input_turn = _turn_public(session, rows_by_id[input_turn_id])
    session.execute(
        "DELETE FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        (queue_id, conv_id, user_id),
    )
    renumber_queue_positions(session, conv_id, user_id)
    now = int(time.time() * 1000)
    deleted_turn_ids = _delete_turn_row_set(
        session, conv_id, user_id, rows_by_id, now,
    )
    revision = int(conversation["rev"] or 0) + 1
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? "
        "WHERE id=? AND user_id=?", (revision, now, conv_id, user_id),
    )
    return {
        "conversationId": conv_id,
        "conversationRevision": revision,
        "queueId": queue_id,
        "cancelled": True,
        "inputTurn": input_turn,
        "deletedTurnIds": deleted_turn_ids,
    }


def _turn_steer_commit(session: Session, payload: Mapping[str, Any]) -> Any:
    """Persist one operator injection on the live assistant Turn before wakeup."""
    conv_id, user_id = _turn_identity(payload)
    attempt_id = _required_text(payload, "attempt_id", 128)
    command_id = _required_text(payload, "command_id", 256)
    text = _required_text(payload, "text", 1024 * 1024)
    session.lock_key("turn_conversation", f"{user_id}:{conv_id}")
    session.lock_key("turn_attempt", attempt_id)
    row = session.fetch_one(
        "SELECT t.* FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "AND t.conversation_id=a.conversation_id "
        "WHERE a.attempt_id=? AND a.conversation_id=? AND t.user_id=? "
        "AND a.status IN ('pending','running') AND a.queue_state='' "
        "AND a.task_id<>'' AND t.current_attempt_id=a.attempt_id "
        "AND t.status IN ('pending','running')",
        (attempt_id, conv_id, user_id),
    )
    if row is None:
        raise StorageError(
            "database_conflict", "The steer execution window is already closed"
        )
    previous_projection = projection_from_turn_row(session, row)
    if not isinstance(previous_projection, Mapping):
        previous_projection = {}
    next_projection = dict(previous_projection)
    existing_records = next_projection.get("_userSteerInjects")
    records = [
        dict(item) if isinstance(item, Mapping) else item
        for item in existing_records
    ] if isinstance(existing_records, list) else []
    block_id = f"injection:user-steer:{command_id}"
    if not any(
        isinstance(item, Mapping) and item.get("blockId") == block_id
        for item in records
    ):
        records.append({
            "blockId": block_id,
            "commandId": command_id,
            "count": 1,
            "previews": [{"text": text[:1200]}],
            "deliveryState": "pending",
        })
    next_projection["_userSteerInjects"] = records
    base_revision = int(row["projection_revision"] or 0)
    target_revision = base_revision + 1
    now = int(time.time() * 1000)
    changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,"
        "projection_revision=?,projection_checkpoint_revision=NULL,"
        "projection_materialized_revision=NULL,projection_patch_count=0,"
        "projection_patch_bytes=0,updated_at=? "
        "WHERE turn_id=? AND projection_revision=? AND current_attempt_id=?",
        (
            _dump(next_projection), target_revision, now,
            row["turn_id"], base_revision, attempt_id,
        ),
    )
    if not changed:
        raise StorageError(
            "database_conflict", "The steer execution window changed while committing"
        )
    delete_turn_projection_checkpoint(session, str(row["turn_id"]))
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1,updated_at_ms=? "
        "WHERE id=? AND user_id=?", (now, conv_id, user_id),
    )
    updated = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
        (row["turn_id"],),
    )
    _upsert_turn_search_row(session, updated)
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "steered": True,
        "injectionId": command_id,
        "blockId": block_id,
        "turn": _turn_public(session, updated),
        "conversationRevision": int(conversation["rev"] or 0),
        "_conversationSyncTurnPatch": _projection_change(
            turn_id=str(row["turn_id"]),
            before=previous_projection,
            after=next_projection,
            base_revision=base_revision,
            target_revision=target_revision,
            updated_at=now,
        ),
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

    if session.fetch_one(
        "SELECT turn_id FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    ) is not None:
        raise StorageError("database_conflict", "Turn already exists")
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
        "turn": _turn_public(session, turn_row),
        "attempt": attempt_public,
        "conversationRevision": revision,
    }


def _turn_attempt_claim(session: Session, payload: Mapping[str, Any]) -> Any:
    attempt_id = _required_text(payload, "attempt_id", 128)
    user_id = _integer(payload, "user_id", minimum=1)
    dispatch_owner_id = str(payload.get("dispatch_owner_id") or "")
    if dispatch_owner_id:
        dispatch_owner_id = _required_text(payload, "dispatch_owner_id", 64)
    legacy_dispatch_claim = f"@dispatching:{attempt_id}"
    dispatch_claim = (
        f"{legacy_dispatch_claim}:{dispatch_owner_id}"
        if dispatch_owner_id
        else legacy_dispatch_claim
    )
    session.lock_key("attempt_dispatch", attempt_id)
    attempt = session.fetch_one(
        "SELECT a.status, a.task_id, a.queue_state FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.attempt_id=? AND t.user_id=?",
        (attempt_id, user_id),
    )
    if (attempt is None or str(attempt["status"]) != "pending"
            or str(attempt.get("queue_state") or "")):
        return False
    existing_task_id = str(attempt["task_id"] or "")
    if existing_task_id == dispatch_claim:
        # The same process owner may safely replay an ambiguously acknowledged
        # claim.  This is the commit-to-bind recovery seam: returning False
        # here used to strand a durably pending attempt forever.
        # A rolling-upgrade predecessor has no process owner or in-process
        # dispatch lock, so retain its one-shot behavior until the application
        # process also restarts onto the new protocol.
        return bool(dispatch_owner_id)
    if existing_task_id:
        return False
    changed = session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=? "
        "AND status='pending' AND task_id='' AND queue_state=''",
        (dispatch_claim, attempt_id),
    )
    return bool(changed)


def _validated_resume_option_anchors(
    settlement: Mapping[str, Any],
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Decode stored retry options without truthiness-based shape repair.

    Older settlements may omit ``resumeOptions`` or use a bare operation
    string. Present values are nevertheless durable protocol authority: an
    invalid item/anchor or duplicate operation is ambiguous and must fail
    before an attempt mutates the Turn.
    """
    if "resumeOptions" not in settlement:
        raw_resume_options: Any = []
    else:
        raw_resume_options = settlement.get("resumeOptions")
    if not isinstance(raw_resume_options, list):
        raise StorageError(
            "database_protocol_error", "Invalid stored resume options")

    options: set[str] = set()
    anchors: dict[str, dict[str, Any]] = {}
    for item in raw_resume_options:
        if isinstance(item, str):
            candidate_operation = item
            raw_item_anchor: Any = {}
        elif isinstance(item, Mapping):
            candidate_operation = item.get("operation")
            raw_item_anchor = item.get("anchor", {})
        else:
            raise StorageError(
                "database_protocol_error", "Invalid stored resume option")
        if not isinstance(candidate_operation, str) or not candidate_operation:
            raise StorageError(
                "database_protocol_error", "Invalid stored resume operation")
        if not isinstance(raw_item_anchor, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid stored resume anchor")
        if candidate_operation in options:
            raise StorageError(
                "database_protocol_error", "Duplicate stored resume operation")
        options.add(candidate_operation)
        anchors[candidate_operation] = dict(raw_item_anchor)
    return options, anchors


def _turn_attempt_create(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    dispatch_mode = _attempt_dispatch_mode(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    command_id = _required_text(payload, "command_id", 256)
    operation = _required_text(payload, "operation", 64)
    expected_revision = _integer(payload, "expected_projection_revision", minimum=0)
    if operation not in {"continue", "checkpoint_resume", "regenerate",
                         "answer_guidance"}:
        raise StorageError("database_protocol_error", "Invalid attempt operation")
    raw_config = payload.get("config")
    raw_answer = (
        raw_config.get("_humanGuidanceAnswer")
        if isinstance(raw_config, Mapping) else None
    )
    if operation == "answer_guidance":
        # The late human answer becomes durable attempt config so a crashed
        # dispatch recovers with the exact same completed question round.
        if (not isinstance(raw_answer, Mapping)
                or not isinstance(raw_answer.get("guidanceId"), str)
                or not raw_answer["guidanceId"]
                or len(raw_answer["guidanceId"]) > 128
                or not isinstance(raw_answer.get("response"), str)
                or not raw_answer["response"]
                or len(raw_answer["response"]) > 32768):
            raise StorageError(
                "database_protocol_error",
                "answer_guidance requires a bounded _humanGuidanceAnswer config")
    elif raw_answer is not None:
        raise StorageError(
            "database_protocol_error",
            "_humanGuidanceAnswer is only valid for answer_guidance")

    target_actor = payload.get("target_actor")
    target_kind = payload.get("target_kind")
    if operation == "regenerate":
        if target_actor not in {"assistant", "planner"}:
            raise StorageError(
                "database_protocol_error", "Invalid regenerate target actor"
            )
        if target_kind not in {"reply", "plan", "flow_node"}:
            raise StorageError(
                "database_protocol_error", "Invalid regenerate target kind"
            )
        if (target_actor == "planner") != (target_kind == "plan"):
            raise StorageError(
                "database_protocol_error", "Inconsistent regenerate target identity"
            )
    elif target_actor is not None or target_kind is not None:
        raise StorageError(
            "database_protocol_error",
            "Only regenerate may migrate generated Turn identity",
        )
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
            "turn": _turn_public(session, turn_row),
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
    public = _turn_public(session, turn)
    if expected_revision != public["projectionRevision"]:
        raise StorageError(
            "turn_projection_stale",
            "The turn changed since this command was prepared.",
        )
    current_id = public.get("currentAttemptId")
    current = None
    if current_id:
        current = session.fetch_one(
            "SELECT status, task_id FROM storage_generation_attempts WHERE attempt_id=?",
            (current_id,),
        )
        if current is not None and current["status"] in ("pending", "running"):
            raise StorageError(
                "database_conflict", "This turn already has a live attempt."
            )
    settlement = public["settlement"]
    if not isinstance(settlement, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid stored turn settlement")
    options, anchors = _validated_resume_option_anchors(settlement)
    if operation != "regenerate" and operation not in options:
        raise StorageError(
            "database_conflict",
            f"{operation} is not available for the current settlement.",
        )
    requested_anchor = payload.get("resume_anchor")
    anchor = dict(anchors.get(operation, {}))
    if requested_anchor is not None and not isinstance(
            requested_anchor, Mapping):
        raise StorageError(
            "database_protocol_error", "Requested resume anchor must be an object")
    if requested_anchor is not None and dict(requested_anchor) != anchor:
        raise StorageError(
            "database_conflict", "The requested resume anchor is not current."
        )
    now = int(time.time() * 1000)
    projection = dict(public["projection"])
    if operation != "regenerate" and current_id:
        attempt_count_row = session.fetch_one(
            "SELECT COUNT(*) AS attempt_count "
            "FROM storage_generation_attempts "
            "WHERE conversation_id=? AND turn_id=?",
            (conv_id, turn_id),
        )
        attempt_count = int(attempt_count_row["attempt_count"] or 0)
        if attempt_count <= 1:
            # Freeze the first settled owner's identity before
            # current_attempt_id moves to its successor. This is the last
            # authority boundary that unambiguously knows both the old owner
            # and its whole checkpoint projection.
            projection = projection_history_with_execution_identity(
                projection,
                attempt_id=current_id,
                task_id=current["task_id"] if current is not None else "",
            )
        # A pre-migration Turn may already contain several unstamped attempts.
        # Attributing that entire history to the latest owner manufactures a
        # false identity and recreates llmRound collisions. Leave ambiguous
        # legacy rows unscoped; new writes are stamped at persistence time and
        # the renderer has an occurrence-aware compatibility path.
    rolled_back_fields: dict[str, Any] = {}
    if operation != "regenerate":
        # Rewind bookkeeping: a resume that restarts a terminal lane must not
        # erase the text the user already watched stream by. The interrupted
        # tail moves into ``projection.rolledBack`` (rendered as a collapsed
        # historical block); the lane itself restarts empty so the successor
        # attempt's tokens land exactly where the wire puts them. A
        # prefill-continue keeps both lanes seamless, so nothing rolls back
        # there.
        lane_continues = (
            operation in {"continue", "answer_guidance"}
            and bool(projection.get("content"))
        )
        content_tail = projection.get("content")
        thinking_tail = projection.get("thinking")
        if (operation == "checkpoint_resume"
                and isinstance(content_tail, str) and content_tail):
            rolled_back_fields["content"] = content_tail
        if (not lane_continues
                and isinstance(thinking_tail, str) and thinking_tail):
            rolled_back_fields["thinking"] = thinking_tail
        if rolled_back_fields:
            rolled_entry: dict[str, Any] = {
                "blockId": f"rolled-back:{current_id or turn_id}",
                "at": now,
                **rolled_back_fields,
            }
            if current_id:
                rolled_entry["attemptId"] = current_id
            existing_rolled = projection.get("rolledBack")
            rolled_lane = (
                [dict(item) for item in existing_rolled
                 if isinstance(item, Mapping)]
                if isinstance(existing_rolled, list) else []
            )
            rolled_lane.append(rolled_entry)
            projection["rolledBack"] = rolled_lane[-4:]
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
        previous_input_projection = projection_from_turn_row(session, parent)
        if not isinstance(previous_input_projection, Mapping):
            previous_input_projection = {}
        next_input_projection = dict(updated_input)
        parent_base_revision = int(parent["projection_revision"])
        parent_target_revision = parent_base_revision + 1
        changed_input = session.execute(
            "UPDATE storage_conversation_turns SET projection_json=?,"
            "projection_revision=?,projection_checkpoint_revision=NULL,"
            "projection_materialized_revision=NULL,"
            "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
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
        delete_turn_projection_checkpoint(session, str(parent_id))
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
        submitted = _turn_public(session, parent)
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
        checkpoint_content = anchor.get("content", "")
        checkpoint_thinking = anchor.get("thinking", "")
        checkpoint_segments = anchor.get("segments", [])
        if (not isinstance(checkpoint_content, str)
                or not isinstance(checkpoint_thinking, str)
                or not isinstance(checkpoint_segments, list)):
            raise StorageError(
                "database_protocol_error", "Invalid checkpoint projection anchor")
        # The anchor's content/thinking fields are still validated for
        # protocol compatibility with settlements written before the
        # rolledBack lane existed, but their values are never applied:
        # terminal lanes always restart empty and the interrupted tail lives
        # on in ``rolledBack``. Seeding the old tail showed text the resumed
        # model never generated, then wiped it.
        projection["content"] = ""
        projection["thinking"] = ""
        raw_kept = anchor.get("keptToolRounds", 0)
        if (isinstance(raw_kept, bool)
                or not isinstance(raw_kept, int)
                or raw_kept < 0):
            raise StorageError(
                "database_protocol_error",
                "Invalid checkpoint tool-round boundary",
            )
        rounds_value = projection.get("toolRounds", [])
        if not isinstance(rounds_value, list):
            raise StorageError(
                "database_protocol_error", "Invalid checkpoint tool-round projection")
        rounds = list(rounds_value)
        if raw_kept > len(rounds):
            raise StorageError(
                "database_protocol_error",
                "Checkpoint tool-round boundary exceeds the projection",
            )
        retained_positions = anchor.get("retainedToolRoundPositions")
        if retained_positions is None:
            # Compatibility for settlement anchors written before occurrence-
            # selective checkpointing shipped.
            projection["toolRounds"] = rounds[:raw_kept]
        else:
            if not isinstance(retained_positions, list):
                raise StorageError(
                    "database_protocol_error",
                    "Invalid checkpoint tool-round positions",
                )
            previous_position = -1
            selected_rounds = []
            for position in retained_positions:
                if (isinstance(position, bool)
                        or not isinstance(position, int)
                        or position <= previous_position
                        or position < 0
                        or position >= raw_kept
                        or position >= len(rounds)):
                    raise StorageError(
                        "database_protocol_error",
                        "Invalid checkpoint tool-round position",
                    )
                selected_rounds.append(rounds[position])
                previous_position = position
            # Retained rows are the pre-gap display+replay superset; the
            # replayable count is informational and only bounds-checked.
            replayable_count = anchor.get("replayableToolRounds", 0)
            if (isinstance(replayable_count, bool)
                    or not isinstance(replayable_count, int)
                    or replayable_count < 0
                    or replayable_count > len(selected_rounds)):
                raise StorageError(
                    "database_protocol_error",
                    "Checkpoint replayable tool-round count is inconsistent",
                )
            projection["toolRounds"] = selected_rounds
        projection["segments"] = list(checkpoint_segments)
    elif (operation in {"continue", "answer_guidance"}
            and rolled_back_fields.get("thinking")):
        # A replay-only continue leaves the prose lane untouched (already
        # empty) but restarts the thinking lane: the resumed model re-thinks,
        # and the interrupted tail is preserved in ``rolledBack`` above.
        projection["thinking"] = ""
    new_revision = public["projectionRevision"] + 1
    attempt_id = str(uuid.uuid4())
    session.execute(
        "INSERT INTO storage_generation_attempts "
        "(attempt_id,conversation_id,turn_id,command_id,task_id,operation,"
        "dispatch_mode,status,base_projection_revision,resume_anchor_json,"
        "config_json,error_json,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            conv_id,
            turn_id,
            command_id,
            "",
            operation,
            dispatch_mode,
            "pending",
            public["projectionRevision"],
            _dump(anchor),
            _dump(payload.get("config") or {}),
            _dump({}),
            now,
        ),
    )
    next_actor = str(target_actor) if operation == "regenerate" else str(turn["actor"])
    next_kind = str(target_kind) if operation == "regenerate" else str(turn["kind"])
    session.execute(
        "UPDATE storage_conversation_turns SET status=?,current_attempt_id=?,"
        "projection_json=?,projection_revision=?,"
        "projection_checkpoint_revision=NULL,"
        "projection_materialized_revision=NULL,projection_patch_count=0,"
        "projection_patch_bytes=0,settlement_json=?,actor=?,kind=?,updated_at=? "
        "WHERE turn_id=? AND projection_revision=?",
        (
            "pending",
            attempt_id,
            _dump(projection),
            new_revision,
            _dump({}),
            next_actor,
            next_kind,
            now,
            turn_id,
            public["projectionRevision"],
        ),
    )
    delete_turn_projection_checkpoint(session, turn_id)
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
                "actor": next_actor,
                "kind": next_kind,
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
        # One-shot replay bootstrap, same exemption class as the terminal
        # settlement: checkpoint_resume rebuilds whole projection lanes, so
        # its patch can exceed the streaming-event transport cap. Without
        # this the resume of a large turn is unwritable (413) while
        # regenerate keeps working.
        allow_oversize=True,
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
        "turn": _turn_public(session, turn),
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
    previous_projection = projection_from_turn_row(session, row)
    if not isinstance(previous_projection, Mapping):
        previous_projection = {}
    next_projection = dict(projection)
    now = int(time.time() * 1000)
    revision = expected + 1
    changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,"
        "projection_revision=?,projection_checkpoint_revision=NULL,"
        "projection_materialized_revision=NULL,"
        "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
        "WHERE turn_id=? AND projection_revision=?",
        (_dump(next_projection), revision, now, turn_id, expected),
    )
    if not changed:
        raise StorageError(
            "turn_projection_stale", "The turn changed while the edit was applied."
        )
    delete_turn_projection_checkpoint(session, turn_id)
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
        "turn": _turn_public(session, updated),
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


def _turn_perception_record(session: Session, payload: Mapping[str, Any]) -> Any:
    """Append one bounded browser timing receipt to its authoritative attempt."""
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    attempt_id = _required_text(payload, "attempt_id", 128)
    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        raise StorageError(
            "database_protocol_error", "Perception observation must be an object"
        )
    # Serialize with terminal turn.event.record so a receipt cannot be lost at
    # the exact moment the server trace is frozen.
    session.lock_key("attempt_events", attempt_id)
    row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts "
        "WHERE attempt_id=? AND conversation_id=? AND turn_id=?",
        (attempt_id, conv_id, turn_id),
    )
    if row is None or attempt is None:
        raise StorageError("database_not_found", "Turn attempt not found")
    task_id = str(attempt["task_id"] or "")
    if not task_id:
        raise StorageError(
            "database_conflict", "Turn attempt has no executor task identity"
        )
    from lib.tasks_pkg.turn_trace import (
        TRACE_CONTRACT_VERSION,
        append_client_trace_observation,
    )
    timing_trace = _load(attempt["timing_trace_json"]) or {}
    if not isinstance(timing_trace, Mapping):
        timing_trace = {}
    if not timing_trace:
        # Online schema upgrades can encounter an in-flight attempt whose
        # receipts still live in the old Turn projection.  Adopt them once,
        # only when the task identity proves they belong to this attempt.
        previous_projection = projection_from_turn_row(session, row)
        projection_trace = (
            previous_projection.get("timingTrace")
            if isinstance(previous_projection, Mapping) else None
        )
        if isinstance(projection_trace, Mapping) \
                and str(projection_trace.get("taskId") or "") == task_id \
                and isinstance(
                    projection_trace.get("clientObservations"), list
                ):
            # Only the old receipt lane is adopted. Server spans/status are
            # re-folded at terminal settlement and must not make this small
            # pre-terminal document masquerade as a frozen execution trace.
            timing_trace = {
                "version": TRACE_CONTRACT_VERSION,
                "taskId": task_id,
                "clientObservations": projection_trace[
                    "clientObservations"
                ],
                **({
                    "clientObservationDroppedCount": int(
                        projection_trace.get(
                            "clientObservationDroppedCount"
                        ) or 0
                    ),
                } if projection_trace.get(
                    "clientObservationDroppedCount"
                ) else {}),
            }
    try:
        timing_trace, applied = append_client_trace_observation(
            timing_trace,
            observation,
            task_id=task_id,
            attempt_id=attempt_id,
            recorded_at_ms=int(time.time() * 1000),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageError("database_protocol_error", str(exc)) from exc
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if applied:
        changed = session.execute(
            "UPDATE storage_generation_attempts SET timing_trace_json=? "
            "WHERE attempt_id=?",
            (_dump(timing_trace), attempt_id),
        )
        if changed != 1:
            raise StorageError(
                "database_conflict", "Attempt changed while perception was recorded"
            )
    return {
        "applied": bool(applied),
        **({"idempotentReplay": True} if not applied else {}),
        "conversationRevision": int(conversation["rev"]),
        "projectionRevision": int(row["projection_revision"] or 0),
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
        related.append(_turn_public(session, row))
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
    root_projection = projection_from_turn_row(session, root)
    if not isinstance(root_projection, Mapping):
        root_projection = {}
    base_revision = int(root["projection_revision"])
    bridge_patch = build_projection_patch(
        root_projection,
        root_projection,
        base_revision=base_revision,
        target_revision=base_revision + 1,
    )
    revision = advance_unchanged_projection_revision(
        session,
        row=root,
        projection=root_projection,
        bridge_patch=bridge_patch,
        now=now,
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
                "projectionPatch": bridge_patch,
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


def _turn_attempt_bind(session: Session, payload: Mapping[str, Any]) -> Any:
    """Bind scheduler identity while leaving the attempt durably pending."""
    attempt_id = _required_text(payload, "attempt_id", 128)
    task_id = _required_text(payload, "task_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    dispatch_owner_id = str(payload.get("dispatch_owner_id") or "")
    if dispatch_owner_id:
        dispatch_owner_id = _required_text(payload, "dispatch_owner_id", 64)
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
    if attempt["status"] not in ("pending", "running"):
        return None
    existing_task_id = str(attempt["task_id"] or "")
    legacy_dispatch_claim = f"@dispatching:{attempt_id}"
    dispatch_claim = (
        f"{legacy_dispatch_claim}:{dispatch_owner_id}"
        if dispatch_owner_id
        else legacy_dispatch_claim
    )
    if (existing_task_id
            and existing_task_id not in (
                legacy_dispatch_claim, dispatch_claim, task_id,
            )):
        raise StorageError(
            "database_conflict", "Generation attempt is already bound to a task"
        )
    if existing_task_id == task_id:
        return _attempt_public(attempt)
    now = int(time.time() * 1000)
    sync_event: dict[str, Any] | None = None
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? "
        "WHERE attempt_id=? AND status IN ('pending','running') "
        "AND task_id=?",
        (task_id, attempt_id, existing_task_id),
    )
    if attempt["status"] == "pending":
        # A pending turn never streamed, so it has no patch head or
        # checkpoint; a bare bump is the exact advance here.
        revision = int(turn["projection_revision"]) + 1
        changed = session.execute(
            "UPDATE storage_conversation_turns SET projection_revision=?, "
            "updated_at=? WHERE turn_id=? AND current_attempt_id=? "
            "AND status='pending' AND projection_revision=?",
            (
                revision,
                now,
                turn["turn_id"],
                attempt_id,
                int(turn["projection_revision"]),
            ),
        )
        if changed != 1:
            raise StorageError(
                "database_conflict", "Pending turn changed during task binding"
            )
    row = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    result = _attempt_public(row)
    if attempt["status"] == "pending":
        turn_projection = projection_from_turn_row(session, turn)
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
                    "status": "pending",
                    "dispatchState": "queued",
                    "attempts": [dict(result)],
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
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? "
            "WHERE id=? AND user_id=?",
            (now, turn["conversation_id"], turn["user_id"]),
        )
        sync_event = event_result["event"]
    result["_conversationSyncAttemptEvents"] = (
        [sync_event] if sync_event else []
    )
    return result


def _turn_attempt_start(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically publish the exact point a bound worker enters execution."""
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
            or turn["current_attempt_id"] != attempt_id
            or str(attempt["task_id"] or "") != task_id):
        return None
    if attempt["status"] == "running":
        return _attempt_public(attempt)
    if attempt["status"] != "pending" or turn["status"] != "pending":
        return None

    now = int(time.time() * 1000)
    attempt_changed = session.execute(
        "UPDATE storage_generation_attempts SET status='running', "
        "started_at=COALESCE(started_at,?) WHERE attempt_id=? "
        "AND task_id=? AND status='pending'",
        (now, attempt_id, task_id),
    )
    revision = int(turn["projection_revision"]) + 1
    turn_changed = session.execute(
        "UPDATE storage_conversation_turns SET status='running', "
        "projection_revision=?, updated_at=? WHERE turn_id=? "
        "AND current_attempt_id=? AND status='pending' "
        "AND projection_revision=?",
        (
            revision,
            now,
            turn["turn_id"],
            attempt_id,
            int(turn["projection_revision"]),
        ),
    )
    if attempt_changed != 1 or turn_changed != 1:
        raise StorageError(
            "database_conflict", "Pending attempt changed during worker entry"
        )
    row = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=?", (attempt_id,)
    )
    result = _attempt_public(row)
    turn_projection = projection_from_turn_row(session, turn)
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
                "dispatchState": "running",
                "attempts": [dict(result)],
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
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? "
        "WHERE id=? AND user_id=?",
        (now, turn["conversation_id"], turn["user_id"]),
    )
    result["_conversationSyncAttemptEvents"] = [event_result["event"]]
    return result

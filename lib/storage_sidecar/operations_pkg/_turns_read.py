"""Turn/attempt read, event-replay and visible-sync operation handlers."""
from __future__ import annotations
from typing import Any
from lib.turn_image_transport import (
    MAX_TURN_IMAGES,
    legacy_turn_image_payload,
)
from collections.abc import Mapping
from lib.storage_sidecar.adapters.base import Session
from lib.storage.errors import StorageError
from lib.conversation_sync.dispatch_contract import (
    CONVERSATION_EXECUTOR_DISPATCH_MODE,
)
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.turn_projection_patch import build_projection_patch
from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
from lib.storage_sidecar.turn_projection_write import (
    advance_unchanged_projection_revision,
    delete_turn_projection_checkpoint,
)
import time
import uuid
from lib.storage_sidecar.operations_pkg._turns_core import (
    _SYNC_PRIVATE_SETTING_KEYS,
    _attempt_public,
    _conversation_change_from_row,
    _conversation_sync_head,
    _stored_object,
    _turn_identity,
    _turn_public,
    _upsert_turn_search_row,
)
from lib.storage_sidecar.operations_pkg._turns_events import _insert_attempt_event, _turn_event_append


_CONVERSATION_CHANGE_DELETE_KEY_CHUNK = 256


def _turn_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    if row is None:
        return None
    return _turn_public(session, row)


def _turn_timing_trace_get(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read permanent timing evidence by task id with an explicit owner fence."""
    task_id = _required_text(payload, "task_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    row = session.fetch_one(
        "SELECT a.attempt_id,a.status AS attempt_status,a.timing_trace_json,"
        "t.turn_id,t.conversation_id,t.user_id,t.current_attempt_id,"
        "t.projection_json,t.projection_revision,"
        "t.projection_checkpoint_revision,"
        "t.projection_materialized_revision,t.projection_patch_count,"
        "t.projection_patch_bytes "
        "FROM storage_generation_attempts a "
        "JOIN storage_conversation_turns t "
        "ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id "
        "WHERE a.task_id=? AND t.user_id=? "
        "ORDER BY COALESCE(a.settled_at,a.started_at,a.created_at) DESC LIMIT 1",
        (task_id, user_id),
    )
    if row is None:
        return None
    timing_trace = _load(row["timing_trace_json"]) or {}
    if not isinstance(timing_trace, Mapping) or not timing_trace:
        # Compatibility fallback for terminal traces written before schema 46.
        projection = projection_from_turn_row(session, row)
        candidate = projection.get("timingTrace")
        timing_trace = (
            candidate if isinstance(candidate, Mapping)
            and str(candidate.get("taskId") or "") == task_id else None
        )
    if not isinstance(timing_trace, Mapping):
        return None
    return {
        "attemptId": str(row["attempt_id"]),
        "attemptStatus": str(row["attempt_status"]),
        "turnId": str(row["turn_id"]),
        "timingTrace": dict(timing_trace),
    }


def _turn_timing_trace_list(session: Session, payload: Mapping[str, Any]) -> Any:
    """Page compact durable trace identities for one owner conversation.

    This deliberately does not select ``timing_trace_json``: list discovery is
    metadata-only, while ``turn.timing_trace.get`` remains the bounded detail
    read. Filters precede the limit and the composite partial index keeps the
    work proportional to one page even when unrelated task-result history is
    large.
    """
    conv_id, user_id = _turn_identity(payload)
    limit = _integer(payload, "limit", default=30, minimum=1, maximum=100)
    before_created_at = payload.get("before_created_at")
    params: list[Any] = [conv_id, user_id]
    before_clause = ""
    if before_created_at is not None:
        before_created_at = _integer(
            payload,
            "before_created_at",
            minimum=0,
            maximum=2**63 - 1,
        )
        before_clause = "AND a.created_at<? "
        params.append(before_created_at)
    params.append(limit + 1)
    rows = session.fetch_all(
        "SELECT a.attempt_id,a.task_id,a.status,a.turn_id,"
        "a.created_at,a.settled_at "
        "FROM storage_generation_attempts a "
        "JOIN storage_conversation_turns t ON t.turn_id=a.turn_id "
        "AND t.conversation_id=a.conversation_id "
        "WHERE a.conversation_id=? AND t.user_id=? AND a.task_id<>'' "
        f"{before_clause}"
        "ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > limit
    return {
        "records": [
            {
                "attempt_id": str(row["attempt_id"]),
                "task_id": str(row["task_id"]),
                "status": str(row["status"]),
                "turn_id": str(row["turn_id"]),
                "created_at": int(row["created_at"] or 0),
                "settled_at": (
                    int(row["settled_at"])
                    if row["settled_at"] is not None else None
                ),
            }
            for row in rows[:limit]
        ],
        "has_more": has_more,
    }


def _turn_image_get(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read one immutable legacy inline image without exporting its Turn."""
    conv_id, user_id = _turn_identity(payload)
    turn_id = _required_text(payload, "turn_id", 128)
    expected_revision = _integer(
        payload,
        "projection_revision",
        minimum=1,
        maximum=2**63 - 1,
    )
    image_index = _integer(
        payload,
        "image_index",
        minimum=0,
        maximum=MAX_TURN_IMAGES - 1,
    )
    row = session.fetch_one(
        "SELECT turn_id,conversation_id,user_id,current_attempt_id,status,"
        "projection_json,projection_revision,projection_checkpoint_revision,"
        "projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes "
        "FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND turn_id=?",
        (conv_id, user_id, turn_id),
    )
    if row is None or str(row["status"]) not in {
        "completed", "interrupted", "truncated", "failed", "superseded",
    }:
        return None
    current_revision = int(row["projection_revision"] or 0)
    if current_revision != expected_revision:
        return {
            "stale": True,
            "projectionRevision": current_revision,
        }
    projection = projection_from_turn_row(session, row)
    images = projection.get("images")
    if not isinstance(images, list) or image_index >= len(images):
        return None
    image = legacy_turn_image_payload(images[image_index])
    if image is None:
        return None
    return {
        "stale": False,
        "projectionRevision": current_revision,
        "mediaType": image.media_type,
        "base64": image.base64_data,
    }


def _turn_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    lane_id = payload.get("lane_id")
    where = "conversation_id=? AND user_id=?"
    params: list[Any] = [conv_id, user_id]
    if lane_id:
        where += " AND lane_id=?"
        params.append(str(lane_id))
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns WHERE " + where
        + " ORDER BY ordinal, turn_id",
        tuple(params),
    )
    return [_turn_public(session, row) for row in rows]


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


def _attempt_dispatchable_list(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Return a bounded fleet batch that provably never reached dispatch.

    This is an internal serving-lifecycle query, not an owner-facing listing.
    Every result carries its explicit owner, and only attempts marked by the
    canonical command service are eligible. ``task_id=''`` is the protocol
    proof that no executor was bound or spawned.
    """
    created_before_ms = _integer(
        payload, "created_before_ms", minimum=0,
    )
    limit = _integer(payload, "limit", default=8, minimum=1, maximum=32)
    rows = session.fetch_all(
        "SELECT a.*, t.user_id AS owner_user_id "
        "FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t "
        "ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id "
        "WHERE a.status='pending' AND a.task_id='' AND a.queue_state='' "
        "AND a.dispatch_mode=? AND a.created_at<=? "
        "AND t.status='pending' AND t.current_attempt_id=a.attempt_id "
        "ORDER BY a.created_at, a.attempt_id LIMIT ?",
        (
            CONVERSATION_EXECUTOR_DISPATCH_MODE,
            created_before_ms,
            limit,
        ),
    )
    dispatchable = []
    for row in rows:
        owner_user_id = int(row["owner_user_id"])
        turn = session.fetch_one(
            "SELECT * FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? AND turn_id=? "
            "AND current_attempt_id=? AND status='pending'",
            (
                row["conversation_id"],
                owner_user_id,
                row["turn_id"],
                row["attempt_id"],
            ),
        )
        if turn is None:  # pragma: no cover - join/serialized snapshot invariant
            continue
        dispatchable.append({
            "userId": owner_user_id,
            "turn": _turn_public(session, turn),
            "attempt": _attempt_public(row),
            "config": _stored_object(row["config_json"], "attempt config"),
        })
    return dispatchable


def _turn_revision(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _turn_identity(payload)
    row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    return int(row["rev"]) if row is not None else 0


_DELTA_OVERLAP_MS = 5000


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
        "turns": [_turn_public(session, row) for row in rows],
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
        "SELECT turn_id,conversation_id,user_id,current_attempt_id,"
        "projection_json,projection_revision,projection_checkpoint_revision,"
        "projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes FROM "
        "storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    if turn is None or turn["current_attempt_id"] != attempt_id:
        return
    projection = projection_from_turn_row(session, turn)
    events[hydrate_index]["payload"]["projection"] = projection


def _turn_events_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    """Bounded TTL deletion of settled attempts' transport event streams.

    ``storage_attempt_events`` is the SSE transport log and, while a compact
    conversation-change reference exists, the exact source for that replay
    event. Every frame historically carried a full projection copy
    (2026-08-20: 281 GiB / 71% of the authority). The turn rows remain the
    permanent transcript authority. This op deletes old streams in bounded,
    resumable slices once neither replay contract needs them:

    • only attempts terminally settled before ``settled_before_ms`` are
      eligible — live attempts are never touched;
    • an attempt referenced by retained conversation sync is protected until
      that change row is pruned;
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
        "AND NOT EXISTS (SELECT 1 FROM storage_conversation_turns AS t "
        "WHERE t.current_attempt_id=a.attempt_id "
        "AND (t.projection_checkpoint_revision IS NOT NULL "
        "OR t.projection_materialized_revision IS NOT NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM storage_conversation_changes AS c "
        "WHERE c.attempt_id=a.attempt_id "
        "AND c.attempt_sequence IS NOT NULL) "
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


def _inherit_live_round_fields(
    projection: dict[str, Any], live_projection: Mapping[str, Any]
) -> None:
    """Restore full-fidelity round fields from the live root projection.

    Visible-sync messages carry the flow projection's bounded preview rounds
    (query brief + result snippet, no toolArgs).  The root turn's
    live-checkpointed projection still holds the same rounds at full fidelity
    under the same stable toolCallId, so the durable boundary can rebuild
    display-complete turns without growing the sync payload.  Fill-only: the
    flow projection's own values always win and unmatched rounds stay
    untouched.
    """
    rounds = projection.get("toolRounds")
    live_rounds = live_projection.get("toolRounds")
    if not isinstance(rounds, list) or not isinstance(live_rounds, list):
        return
    live_by_id = {
        str(item.get("toolCallId")): item
        for item in live_rounds
        if isinstance(item, Mapping) and item.get("toolCallId")
    }
    if not live_by_id:
        return
    for round_dict in rounds:
        if not isinstance(round_dict, dict):
            continue
        live = live_by_id.get(str(round_dict.get("toolCallId") or ""))
        if not isinstance(live, Mapping):
            continue
        for field in ("toolArgs", "toolContent", "tStart", "tEnd",
                      "attemptId", "taskId"):
            if (round_dict.get(field) in (None, "")
                    and live.get(field) not in (None, "")):
                round_dict[field] = live[field]


def _visible_shape(
    message: Mapping[str, Any],
    default_kind: str,
    live_projection: Mapping[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    from lib.orchestration_message_compat import normalize_flow_message
    from lib.turn_projection_patch import normalize_projection_document

    from lib.turn_projection_segments import projection_with_stable_segments
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
    projection.setdefault("toolRounds", [])
    if isinstance(live_projection, Mapping):
        _inherit_live_round_fields(projection, live_projection)
    # Persist the same assembled segment timeline a normal turn checkpoints.
    # Visible-sync messages arrive with toolRounds but no segments; leaving
    # the durable list empty made the settled surface fall back to content
    # plus a trailing rounds panel (tools never interleave, command cards
    # lose their data) until a serve-time repair rewrote the read.
    projection = projection_with_stable_segments(
        projection, actor=actor, status="completed")
    orchestration = projection.get("orchestration")
    phase = dict(orchestration) if isinstance(orchestration, Mapping) else {}
    phase.update({
        "iteration": message.get("_flowIteration") or message.get("_flowPlannerIteration"),
        "approved": message.get("_flowApproved"),
        "nextPhase": message.get("_flowNextPhase"),
        "stuck": message.get("_isStuck"),
        "flowNodeId": message.get("_flowNodeId"),
        "flowRunId": message.get("_flowRunId"),
    })
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
    root_projection_before = projection_from_turn_row(session, root)
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
            message,
            str(payload.get("default_kind") or "flow_node"),
            live_projection=root_projection_before,
        )
        if index == 0:
            turn_id = root_id
            visible_ids.append(turn_id)
            previous_id = turn_id
            if not is_flow_turn_kind(root["kind"]):
                updated_root = session.execute(
                    "UPDATE storage_conversation_turns SET actor=?,kind=?,run_id=?,"
                    "projection_json=?,projection_revision=?,"
                    "projection_checkpoint_revision=NULL,"
                    "projection_materialized_revision=NULL,"
                    "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
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
                delete_turn_projection_checkpoint(session, root_id)
                root = session.fetch_one(
                    "SELECT * FROM storage_conversation_turns WHERE turn_id=?",
                    (root_id,),
                )
                changed = True
            _upsert_turn_search_row(session, root)
            related.append(_turn_public(session, root))
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
        related.append(_turn_public(session, existing))
        previous_id = turn_id
    if changed:
        root = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=?", (root_id,)
        )
        revision = int(root["projection_revision"] or 0)
        if revision == root_base_revision:
            # The projection is unchanged; advance the revision head-
            # consistently and reuse the hydrated projection. Hydrating the
            # advanced row before the bridge event lands below would fail
            # closed on the not-yet-appended patch.
            root_public = _turn_public(session, root)
            revision = advance_unchanged_projection_revision(
                session,
                row=root,
                projection=root_projection_before,
                bridge_patch=build_projection_patch(
                    root_projection_before,
                    root_projection_before,
                    base_revision=root_base_revision,
                    target_revision=root_base_revision + 1,
                ),
                now=now,
            )
            root_public["projectionRevision"] = revision
            root_public["updatedAt"] = now
            related[0] = root_public
            root_projection_after = dict(root_projection_before)
        elif revision != root_base_revision + 1:
            raise StorageError(
                "database_conflict",
                "The visible root turn revision advanced unexpectedly.",
            )
        else:
            related[0] = _turn_public(session, root)
            root_projection_after = projection_from_turn_row(session, root)
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


def _turn_sync_snapshot(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read the authoritative rows and replay head in one read transaction."""
    conv_id, user_id = _turn_identity(payload)
    turn_limit = _integer(
        payload, "turn_limit", default=0, minimum=0, maximum=256
    )
    include_artifact_hint = payload.get("include_artifact_hint", False)
    if not isinstance(include_artifact_hint, bool):
        raise StorageError(
            "database_protocol_error", "Invalid artifact hint selector"
        )
    conversation = session.fetch_one(
        "SELECT rev,settings_json FROM storage_conversations "
        "WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        return None
    sync_sequence = _conversation_sync_head(session, conv_id, user_id)
    turn_window: dict[str, Any] | None = None
    turns: list[Mapping[str, Any]]
    if turn_limit:
        lane_counts = session.fetch_all(
            "SELECT lane_id,COUNT(*) AS total "
            "FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? GROUP BY lane_id",
            (conv_id, user_id),
        )
        # A bounded main-lane snapshot preserves the complete topology of a
        # linear conversation. Branch topology remains on the full path until
        # lane-directory paging exists; hiding a durable branch is not safe.
        linear = not lane_counts or (
            len(lane_counts) == 1 and str(lane_counts[0]["lane_id"]) == "main"
        )
        if linear:
            descending_turns = session.fetch_all(
                "SELECT * FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? AND lane_id='main' "
                "ORDER BY ordinal DESC LIMIT ?",
                (conv_id, user_id, turn_limit + 1),
            )
            has_more = len(descending_turns) > turn_limit
            turns = list(reversed(descending_turns[:turn_limit]))
            total_turns = int(lane_counts[0]["total"] or 0) if lane_counts else 0
            turn_window = {
                "laneId": "main",
                "nextBeforeOrdinal": (
                    int(turns[0]["ordinal"]) if has_more and turns else None
                ),
                "hasMore": has_more,
                "totalTurns": total_turns,
            }
        else:
            turns = session.fetch_all(
                "SELECT * FROM storage_conversation_turns "
                "WHERE conversation_id=? AND user_id=? ORDER BY ordinal",
                (conv_id, user_id),
            )
    else:
        turns = session.fetch_all(
            "SELECT * FROM storage_conversation_turns "
            "WHERE conversation_id=? AND user_id=? ORDER BY ordinal",
            (conv_id, user_id),
        )
    if turn_window is not None:
        turn_ids = [str(row["turn_id"]) for row in turns]
        attempts: list[Mapping[str, Any]] = []
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            attempts = session.fetch_all(
                "SELECT a.* FROM storage_generation_attempts a "
                "JOIN storage_conversation_turns t "
                "ON t.current_attempt_id=a.attempt_id "
                "WHERE t.conversation_id=? AND t.user_id=? "
                f"AND t.turn_id IN ({placeholders}) "
                "ORDER BY a.created_at,a.attempt_id",
                (conv_id, user_id, *turn_ids),
            )
    else:
        attempts = session.fetch_all(
            "SELECT a.* FROM storage_generation_attempts a "
            "JOIN storage_conversation_turns t ON t.turn_id=a.turn_id "
            "WHERE a.conversation_id=? AND t.user_id=? "
            "ORDER BY a.created_at,a.attempt_id",
            (conv_id, user_id),
        )
    queue_rows = session.fetch_all(
        "SELECT id,payload_json,position,kind,priority,created_at_ms,"
        "input_turn_id,output_turn_id,attempt_id "
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
            "hasAttachments": bool(queue_payload.get("attachments")),
            "hasRefs": bool(queue_payload.get("convRefs")),
            "hasQuotes": bool(queue_payload.get("replyQuotes")),
        }
        source_message_id = str(
            user_message.get("_msgId") or queue_payload.get("_msgId") or ""
        )
        if source_message_id:
            item["sourceMessageId"] = source_message_id
        for public_key, stored_key in (
            ("inputTurnId", "input_turn_id"),
            ("outputTurnId", "output_turn_id"),
            ("attemptId", "attempt_id"),
        ):
            stored_identifier = row[stored_key]
            if stored_identifier:
                item[public_key] = str(stored_identifier)
        if queue_payload.get("_peerMessage"):
            item.update({
                "isPeerMessage": True,
                "fromConv": str(queue_payload.get("_fromConv") or ""),
                "isPeerHuman": bool(queue_payload.get("_peerHuman")),
            })
        if queue_payload.get("_steerFallback"):
            item["steerFallback"] = True
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
    result = {
        "conversationId": conv_id,
        "conversationRevision": int(conversation["rev"] or 0),
        "syncSequence": sync_sequence,
        "settings": public_settings,
        "turns": [_turn_public(session, row) for row in turns],
        "attempts": [_attempt_public(row) for row in attempts],
        "queueItems": queue_items,
    }
    if turn_window is not None:
        result["turnWindow"] = turn_window
    if include_artifact_hint:
        result["hasArtifacts"] = session.fetch_one(
            "SELECT 1 AS present FROM chat_artifacts "
            "WHERE conv_id=? AND deleted_at=0 LIMIT 1",
            (conv_id,),
        ) is not None
    return result


def _turn_sync_page(session: Session, payload: Mapping[str, Any]) -> Any:
    """Read one bounded owner/lane history page at a single replay head.

    Pages are exclusive on ``before_ordinal`` and returned oldest-first so the
    browser can merge them with the same lane/ordinal reducer used by live
    snapshots. ``LIMIT + 1`` proves ``hasMore`` without loading the remaining
    history. Only each paged Turn's current attempt is included; historical
    attempt logs have their own bounded endpoint.
    """
    conv_id, user_id = _turn_identity(payload)
    lane_id = _required_text(payload, "lane_id", 128)
    before_ordinal = _integer(
        payload,
        "before_ordinal",
        default=2**63 - 1,
        minimum=0,
        maximum=2**63 - 1,
    )
    limit = _integer(payload, "limit", default=64, minimum=1, maximum=256)
    expected_sync_sequence = _integer(
        payload, "sync_sequence", minimum=0
    )
    conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if conversation is None:
        return None
    sync_sequence = _conversation_sync_head(session, conv_id, user_id)
    if sync_sequence != expected_sync_sequence:
        return {
            "stale": True,
            "syncSequence": sync_sequence,
        }
    total_row = session.fetch_one(
        "SELECT COUNT(*) AS total FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id=?",
        (conv_id, user_id, lane_id),
    )
    descending_rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal<? "
        "ORDER BY ordinal DESC LIMIT ?",
        (conv_id, user_id, lane_id, before_ordinal, limit + 1),
    )
    has_more = len(descending_rows) > limit
    page_rows = list(reversed(descending_rows[:limit]))
    turn_ids = [str(row["turn_id"]) for row in page_rows]
    attempts: list[Mapping[str, Any]] = []
    if turn_ids:
        placeholders = ",".join("?" for _ in turn_ids)
        attempts = session.fetch_all(
            "SELECT a.* FROM storage_generation_attempts a "
            "JOIN storage_conversation_turns t "
            "ON t.current_attempt_id=a.attempt_id "
            "WHERE t.conversation_id=? AND t.user_id=? "
            f"AND t.turn_id IN ({placeholders}) "
            "ORDER BY a.created_at,a.attempt_id",
            (conv_id, user_id, *turn_ids),
        )
    next_before_ordinal = (
        int(page_rows[0]["ordinal"]) if page_rows else None
    )
    return {
        "conversationId": conv_id,
        "conversationRevision": int(conversation["rev"] or 0),
        "syncSequence": sync_sequence,
        "laneId": lane_id,
        "beforeOrdinal": before_ordinal,
        "nextBeforeOrdinal": next_before_ordinal,
        "hasMore": has_more,
        "totalTurns": int(total_row["total"] or 0) if total_row else 0,
        "turns": [_turn_public(session, row) for row in page_rows],
        "attempts": [_attempt_public(row) for row in attempts],
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
        "SELECT c.conversation_id,c.user_id,c.sync_sequence,c.change_type,"
        "c.turn_id,c.attempt_id,c.attempt_sequence,c.event_json,c.created_at,"
        "e.payload_json AS attempt_event_json "
        "FROM storage_conversation_changes AS c "
        "LEFT JOIN storage_attempt_events AS e "
        "ON c.attempt_sequence IS NOT NULL "
        "AND e.attempt_id=c.attempt_id AND e.sequence=c.attempt_sequence "
        "AND e.conversation_id=c.conversation_id AND e.turn_id=c.turn_id "
        "WHERE c.conversation_id=? AND c.user_id=? AND c.sync_sequence>? "
        "ORDER BY c.sync_sequence LIMIT ?",
        (conv_id, user_id, after, limit),
    )
    if head > after and (
        not rows or any(
            int(row["sync_sequence"] or 0) != after + index
            for index, row in enumerate(rows, start=1)
        )
    ):
        return {
            "head": head,
            "events": [],
            "resetRequired": True,
            "resetReason": "cursor_expired",
        }
    events = [_conversation_change_from_row(row) for row in rows]
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
    Selected composite keys are deleted in 256-row statements, staying below
    SQLite's classic host-parameter ceiling without one statement per event.
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
    for offset in range(
        0, len(selected), _CONVERSATION_CHANGE_DELETE_KEY_CHUNK
    ):
        chunk = selected[
            offset:offset + _CONVERSATION_CHANGE_DELETE_KEY_CHUNK
        ]
        key_marks = ",".join("(?,?,?)" for _ in chunk)
        keys = tuple(
            value
            for row in chunk
            for value in (
                row["conversation_id"],
                int(row["user_id"]),
                int(row["sync_sequence"]),
            )
        )
        deleted += session.execute(
            "DELETE FROM storage_conversation_changes "
            "WHERE (conversation_id,user_id,sync_sequence) IN ("
            + key_marks + ")",
            keys,
        )
    return {"deletedRows": deleted, "remaining": bool(overflow)}

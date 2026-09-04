"""Attempt event append/record transport handlers."""
from __future__ import annotations
from typing import Any
from collections.abc import Mapping
from lib.storage_sidecar.adapters.base import Session
from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._records import (
    _append_event_row,
)
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.turn_projection_cache import (
    PROJECTION_CACHE_CHARGE_MULTIPLIER,
    TurnProjectionCache,
    projection_cache_key,
)
from lib.storage_sidecar.turn_projection_head import (
    fold_turn_projection_head,
    load_turn_projection_base,
    projection_head_state,
)
from lib.storage_sidecar.turn_projection_write import (
    write_turn_projection_revision,
)
from lib.turn_projection_patch import (
    ProjectionPatchError,
    apply_projection_patch,
    build_projection_patch,
)
from lib.turn_projection_segments import projection_with_stable_segments
import time
from lib.storage_sidecar.operations_pkg._turns_core import _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES, _attempt_public, _observe_attempt_event_payload, _observe_projection_blob_write_deferral, _observe_projection_blob_write_skip, _observe_projection_checkpoint_materialization, _upsert_turn_search_row


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
    allow_oversize: bool = False,
) -> int:
    """Insert one measured attempt event and enforce the transport budget."""
    encoded_envelope = _dump(envelope)
    payload_bytes = len(encoded_envelope)
    if (
        not allow_oversize
        and event_type != "terminal_settlement"
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
        "SELECT turn_id,conversation_id,user_id,lane_id,parent_turn_id,"
        "ordinal,actor,kind,run_id,status,current_attempt_id,"
        "projection_revision,projection_checkpoint_revision,"
        "projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes,"
        "settlement_json,created_at,updated_at "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (attempt["turn_id"],),
    )
    if (turn is None or int(turn["user_id"]) != user_id
            or turn["current_attempt_id"] != attempt_id):
        return {"applied": False}
    now = int(payload.get("now") or time.time() * 1000)
    old_revision = int(turn["projection_revision"])
    new_revision = old_revision + 1
    projection_head = projection_head_state(turn)
    terminal = bool(payload.get("terminal"))
    status = str(payload.get("status") or ("running" if not terminal else "failed"))
    attempt_started_by_event = False
    if not terminal and attempt["status"] == "pending":
        task_id = _required_text(payload, "task_id", 256)
        if not str(attempt["task_id"] or "") or str(attempt["task_id"]) != task_id:
            return {"applied": False}
        started = session.execute(
            "UPDATE storage_generation_attempts SET status='running', "
            "started_at=COALESCE(started_at,?) WHERE attempt_id=? "
            "AND task_id=? AND status='pending'",
            (now, attempt_id, task_id),
        )
        if started != 1:
            return {"applied": False}
        attempt_started_by_event = True
    cache = getattr(session, "turn_projection_cache", None)
    cache = cache if isinstance(cache, TurnProjectionCache) else None
    cache_key = projection_cache_key(
        session.backend,
        user_id,
        str(attempt["conversation_id"]),
        str(turn["turn_id"]),
        attempt_id,
    )
    cache_entry = (
        cache.get(cache_key, revision=old_revision)
        if cache is not None else None
    )
    stored_payload_bytes: int | None = None
    if cache_entry is not None:
        stored_projection = cache_entry.projection
        stored_projection_is_mapping = True
        stored_projection_matches_previous = (
            cache_entry.stored_matches_projection)
        stored_payload_bytes = cache_entry.stored_payload_bytes
        previous_has_stable_segments = cache_entry.stable_segments
        cache_charge_bytes = cache_entry.charge_bytes
    else:
        try:
            projection_base = load_turn_projection_base(
                session, turn, state=projection_head, require_mapping=False)
        except StorageError as exc:
            if exc.code != "database_conflict":
                raise
            if attempt_started_by_event:
                raise StorageError(
                    "database_conflict",
                    "Turn changed while its first worker event was recorded",
                ) from exc
            return {"applied": False}
        materialized_projection = projection_base.projection
        stored_projection_is_mapping = isinstance(
            materialized_projection, Mapping)
        if projection_head.active and not stored_projection_is_mapping:
            raise StorageError(
                "database_integrity",
                "Turn projection patch head has a non-object base",
            )
        stored_projection = (
            fold_turn_projection_head(
                session,
                turn,
                materialized_projection,
                state=projection_head,
            )
            if projection_head.active else materialized_projection
        )
        stored_projection_is_mapping = isinstance(stored_projection, Mapping)
        stored_projection_matches_previous = (
            stored_projection_is_mapping and not projection_head.active)
        stored_payload_bytes = projection_base.payload_bytes
        previous_has_stable_segments = False
        cache_charge_bytes = max(
            1,
            (stored_payload_bytes + projection_head.patch_bytes)
            * PROJECTION_CACHE_CHARGE_MULTIPLIER,
        )
    previous_projection = (
        stored_projection if stored_projection_is_mapping else {})

    def current_stored_payload_bytes() -> int:
        nonlocal stored_payload_bytes
        if stored_payload_bytes is not None:
            return stored_payload_bytes
        projection_base = load_turn_projection_base(
            session, turn, state=projection_head, require_mapping=False)
        stored_payload_bytes = projection_base.payload_bytes
        return stored_payload_bytes

    projection_patch = payload.get("projection_patch")
    projection_segments_stable = payload.get("projection_segments_stable", False)
    if not isinstance(projection_segments_stable, bool):
        raise StorageError(
            "database_protocol_error",
            "Projection stable-segment evidence must be a boolean",
        )
    if projection_segments_stable and projection_patch is None:
        raise StorageError(
            "database_protocol_error",
            "Projection stable-segment evidence requires a projection patch",
        )
    replay_projection_patch: dict[str, Any] | None = None
    normalization_changed = False
    if projection_patch is not None:
        # Application-side patches are based on the public stable projection
        # returned by ``turn.get``. Normalize the locked storage document at
        # the same boundary before applying the delta; otherwise a historical
        # pre-normalization row could silently keep stale segment mirrors that
        # were absent from the patch because both public endpoints had already
        # repaired them.
        if not previous_has_stable_segments:
            normalized_projection = projection_with_stable_segments(
                previous_projection,
                actor=str(turn["actor"] or "assistant"),
                status=str(turn["status"] or "running"),
            )
            stored_projection_matches_previous = (
                stored_projection_matches_previous
                and normalized_projection == previous_projection
            )
            normalization_changed = normalized_projection != previous_projection
            previous_projection = normalized_projection
        previous_has_stable_segments = True
        if bool(payload.get("slim")):
            raise StorageError(
                "database_protocol_error",
                "Slim turn events cannot carry a projection patch",
            )
        if not isinstance(projection_patch, Mapping):
            raise StorageError(
                "database_protocol_error", "Projection patch must be an object")
        base_revision = projection_patch.get("baseRevision")
        target_revision = projection_patch.get("targetRevision")
        if (not isinstance(base_revision, int) or isinstance(base_revision, bool)
                or not isinstance(target_revision, int)
                or isinstance(target_revision, bool)):
            raise StorageError(
                "database_protocol_error",
                "Projection patch revisions must be integers",
            )
        if base_revision != old_revision:
            raise StorageError(
                "turn_projection_stale",
                "Projection patch base revision is stale",
            )
        if target_revision != new_revision:
            raise StorageError(
                "database_protocol_error",
                "Projection patch must advance exactly one revision",
            )
        try:
            projection = apply_projection_patch(
                previous_projection, projection_patch)
        except ProjectionPatchError as exc:
            raise StorageError(
                "database_protocol_error", "Projection patch is invalid") from exc
        replay_projection_patch = dict(projection_patch)
    else:
        projection = payload.get("projection") or {}
    settlement = payload.get("settlement") or {}
    event_type = (
        "terminal_settlement"
        if terminal
        else str(payload.get("event_type") or "projection_updated")
    )
    if terminal and isinstance(projection, Mapping):
        # The same attempt lock is used by perception receipt writes.  Whichever
        # transaction wins first is therefore preserved: pre-terminal browser
        # receipts are overlaid here, while post-terminal receipts extend the
        # already-frozen attempt snapshot.
        from lib.tasks_pkg.turn_trace import merge_client_trace_evidence
        attempt_trace = _load(attempt["timing_trace_json"]) or {}
        timing_trace_missing = object()
        patch_target_timing_trace = projection.get(
            "timingTrace", timing_trace_missing)
        merged_trace = merge_client_trace_evidence(
            projection.get("timingTrace"),
            attempt_trace,
            task_id=str(attempt["task_id"] or payload.get("task_id") or ""),
        )
        projection = dict(projection)
        if merged_trace:
            projection["timingTrace"] = merged_trace
        else:
            projection.pop("timingTrace", None)
        if projection.get(
                "timingTrace", timing_trace_missing
        ) != patch_target_timing_trace:
            replay_projection_patch = None
    # DELTA-class writes advance only cumulative content/thinking. Build their
    # exact replay patch from those two roots instead of diffing the multi-MiB
    # projection. Structural patches are already validated above. Both shapes
    # may append to the same bounded durable head; terminal settlement, a hidden
    # normalization delta, or a full head forces one materialization.
    slim = bool(payload.get("slim"))
    projection_is_mapping = isinstance(projection, Mapping)
    if slim:
        content = str(payload.get("content") or "")
        thinking = str(payload.get("thinking") or "")
        next_projection = dict(previous_projection)
        next_projection["content"] = content
        next_projection["thinking"] = thinking
        previous_text_projection = {
            field: previous_projection[field]
            for field in ("content", "thinking")
            if field in previous_projection
        }
        replay_projection_patch = build_projection_patch(
            previous_text_projection,
            {"content": content, "thinking": thinking},
            base_revision=old_revision,
            target_revision=new_revision,
        )
        text_changed = (
            content != str(previous_projection.get("content") or "")
            or thinking != str(previous_projection.get("thinking") or "")
        )
        next_has_stable_segments = (
            previous_has_stable_segments
            and not text_changed
            and status == str(turn["status"] or "")
        )
    else:
        next_projection = dict(projection) if projection_is_mapping else {}
        # Only the authenticated application producer can attest that its
        # patch target already crossed ``projection_with_stable_segments``.
        # Missing evidence is backward-compatible and fail-safe: retain the
        # baseline, but normalize it once before the next structural patch.
        next_has_stable_segments = (
            projection_patch is not None and projection_segments_stable)
    if replay_projection_patch is None:
        replay_projection_patch = build_projection_patch(
            previous_projection,
            next_projection,
            base_revision=old_revision,
            target_revision=new_revision,
        )
    replay_patch_is_exact = (
        not normalization_changed
        and (projection_head.active or stored_projection_matches_previous)
    )
    projection_write = write_turn_projection_revision(
        session,
        turn_id=str(turn["turn_id"]),
        conversation_id=str(turn["conversation_id"]),
        user_id=user_id,
        attempt_id=attempt_id,
        status=status,
        old_revision=old_revision,
        new_revision=new_revision,
        now=now,
        terminal=terminal,
        slim=slim,
        settlement=settlement,
        projection_value=projection,
        previous_projection=previous_projection,
        next_projection=next_projection,
        projection_is_mapping=projection_is_mapping,
        physical_matches_previous=stored_projection_matches_previous,
        current_head=projection_head,
        replay_projection_patch=replay_projection_patch,
        replay_patch_is_exact=replay_patch_is_exact,
        cache_charge_bytes=cache_charge_bytes,
        stored_payload_bytes=stored_payload_bytes,
        current_stored_payload_bytes=current_stored_payload_bytes,
    )
    changed = projection_write.changed
    projection_bytes = projection_write.projection_bytes
    next_cache_charge_bytes = projection_write.cache_charge_bytes
    next_stored_payload_bytes = projection_write.stored_payload_bytes
    next_stored_matches_projection = (
        projection_write.stored_matches_projection)
    projection_blob_write_skipped = projection_write.blob_write_skipped
    projection_blob_write_skipped_bytes = (
        projection_write.blob_write_skipped_bytes)
    if not changed:
        if cache is not None:
            cache.discard(cache_key)
        if attempt_started_by_event:
            raise StorageError(
                "database_conflict",
                "Turn changed while its first worker event was recorded",
            )
        return {"applied": False}
    if terminal:
        terminal_trace = (
            next_projection.get("timingTrace")
            if isinstance(next_projection, Mapping)
            and isinstance(next_projection.get("timingTrace"), Mapping)
            else {}
        )
        session.execute(
            "UPDATE storage_generation_attempts SET status=?, error_json=?, "
            "timing_trace_json=?, settled_at=? WHERE attempt_id=? "
            "AND status IN ('pending','running')",
            (
                status,
                _dump(payload.get("error") or {}),
                _dump(terminal_trace),
                now,
                attempt_id,
            ),
        )
    event_payload = dict(payload.get("event_payload") or {})
    # Compact durable transport (2026-08-23 wire-amplification root fix): the
    # full projection already landed transactionally on the turn row.  Store
    # the exact revision-to-revision patch for replay, including terminal
    # frames, rather than a second full copy.  Legacy readers still receive a
    # hydrated full projection on the requested page tail; patch-aware SSE
    # readers opt out of that expansion.
    event_payload.pop("projection", None)
    event_payload["projectionPatch"] = (
        replay_projection_patch
        if replay_projection_patch is not None
        else build_projection_patch(
            previous_projection,
            next_projection,
            base_revision=old_revision,
            target_revision=new_revision,
        )
    )
    event_payload["status"] = status
    event_payload["projectionBytes"] = projection_bytes
    if attempt_started_by_event:
        started_attempt = session.fetch_one(
            "SELECT * FROM storage_generation_attempts WHERE attempt_id=?",
            (attempt_id,),
        )
        event_payload["attempts"] = [_attempt_public(started_attempt)]
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
    if cache is not None:
        if terminal or (not slim and not projection_is_mapping):
            cache.discard(cache_key)
        else:
            cache.remember(
                cache_key,
                revision=new_revision,
                projection=next_projection,
                charge_bytes=next_cache_charge_bytes,
                stored_payload_bytes=next_stored_payload_bytes,
                stored_matches_projection=next_stored_matches_projection,
                stable_segments=next_has_stable_segments,
            )
    if projection_blob_write_skipped:
        _observe_projection_blob_write_skip(
            event_type, projection_blob_write_skipped_bytes
        )
    if projection_write.blob_write_deferred:
        _observe_projection_blob_write_deferral(
            event_type, int(projection_write.stored_payload_bytes or 0))
    if projection_write.checkpoint_materialized:
        _observe_projection_checkpoint_materialization(
            event_type,
            projection_write.checkpoint_materialized_bytes,
            projection_write.inline_projection_released_bytes,
        )
    return {
        "applied": True,
        "status": status,
        "projection_revision": new_revision,
        "task_event": task_event_result,
        "_conversationSyncAttemptEvents": [event_result["event"]],
    }

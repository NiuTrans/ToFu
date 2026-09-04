"""Choose and execute one bounded live Turn projection storage write.

Responsibility
--------------
Keep ``turn.event.record`` policy separate from SQL mechanics. A revision may
append its already-durable replay patch to the bounded projection head, omit an
unchanged BLOB assignment, use the text-only JSON mutation, materialize a live
checkpoint outside the hot Turn row, or materialize the terminal document.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _dump
from lib.storage_sidecar.turn_projection_cache import (
    PROJECTION_CACHE_CHARGE_MULTIPLIER,
    projection_text_bytes,
)
from lib.storage_sidecar.turn_projection_head import (
    TurnProjectionHeadState,
    plan_projection_head_append,
    projection_head_state,
)


PROJECTION_INLINE_LIVE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TurnProjectionWriteResult:
    """Storage/cache evidence produced by one projection revision write."""

    changed: int
    projection_bytes: int
    cache_charge_bytes: int
    stored_payload_bytes: int | None
    stored_matches_projection: bool
    blob_write_skipped: bool
    blob_write_skipped_bytes: int
    blob_write_deferred: bool
    head: TurnProjectionHeadState
    checkpoint_materialized: bool = False
    checkpoint_materialized_bytes: int = 0
    inline_projection_released_bytes: int = 0


def _update_projection_row(
    session: Session,
    *,
    turn_id: str,
    attempt_id: str,
    status: str,
    old_revision: int,
    new_revision: int,
    now: int,
    head: TurnProjectionHeadState,
    projection_assignment: str = "",
    projection_args: tuple[Any, ...] = (),
    settlement: Mapping[str, Any] | None = None,
) -> int:
    settlement_assignment = (
        ",settlement_json=?" if settlement is not None else "")
    settlement_args = ((_dump(settlement),) if settlement is not None else ())
    return session.execute(
        "UPDATE storage_conversation_turns SET status=?,"
        + projection_assignment
        + "projection_revision=?,projection_checkpoint_revision=?,"
        "projection_materialized_revision=?,"
        "projection_patch_count=?,projection_patch_bytes=?"
        + settlement_assignment
        + ",updated_at=? WHERE turn_id=? AND current_attempt_id=? "
        "AND projection_revision=?",
        (
            status,
        ) + projection_args + (
            new_revision,
            head.checkpoint_revision,
            head.materialized_revision,
            head.patch_count,
            head.patch_bytes,
        ) + settlement_args + (
            now,
            turn_id,
            attempt_id,
            old_revision,
        ),
    )


def _upsert_projection_checkpoint(
    session: Session,
    *,
    turn_id: str,
    conversation_id: str,
    user_id: int,
    attempt_id: str,
    revision: int,
    projection_json: bytes,
    now: int,
) -> None:
    changed = session.execute(
        "INSERT INTO storage_turn_projection_checkpoints("
        "turn_id,conversation_id,user_id,attempt_id,projection_revision,"
        "projection_json,projection_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(turn_id) DO UPDATE SET conversation_id=excluded.conversation_id,"
        "user_id=excluded.user_id,attempt_id=excluded.attempt_id,"
        "projection_revision=excluded.projection_revision,"
        "projection_json=excluded.projection_json,"
        "projection_bytes=excluded.projection_bytes,updated_at=excluded.updated_at",
        (
            turn_id,
            conversation_id,
            user_id,
            attempt_id,
            revision,
            projection_json,
            len(projection_json),
            now,
        ),
    )
    if changed != 1:
        raise StorageError(
            "database_integrity",
            "Turn projection checkpoint upsert changed no row",
        )


def delete_turn_projection_checkpoint(session: Session, turn_id: str) -> int:
    """Delete one reconstructible live checkpoint after full materialization."""
    return session.execute(
        "DELETE FROM storage_turn_projection_checkpoints WHERE turn_id=?",
        (str(turn_id),),
    )


def advance_unchanged_projection_revision(
    session: Session,
    *,
    row: Mapping[str, Any],
    projection: Mapping[str, Any],
    bridge_patch: Mapping[str, Any],
    now: int,
) -> int:
    """Advance one revision across an unchanged projection, head-consistently.

    The caller records ``bridge_patch`` as the new revision's attempt event
    immediately after this update.  A bare ``projection_revision`` bump would
    orphan a live patch head (``current - materialized`` no longer equals
    ``patch_count``) or strand a revision-fenced checkpoint, so the next
    hydration would fail closed.  An inline materialized row may bump bare; a
    checkpoint fence starts a one-patch bridge head; an active head appends;
    an exhausted head budget parks the projection in a checkpoint, mirroring
    the streaming write path.  Returns the new revision.
    """
    head = projection_head_state(row)
    old_revision = int(row["projection_revision"] or 0)
    new_revision = old_revision + 1
    turn_id = str(row["turn_id"])
    attempt_id = str(row["current_attempt_id"] or "")
    next_head = plan_projection_head_append(
        head,
        current_revision=old_revision,
        patch_bytes=len(_dump(bridge_patch)),
        exact_patch=True,
        projection_changed=False,
    )
    if next_head is not None:
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=str(row["status"] or ""),
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=next_head,
        )
    elif head.active or head.checkpoint_active:
        checkpoint_projection = _dump(dict(projection))
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=str(row["status"] or ""),
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=TurnProjectionHeadState(
                None, 0, 0, checkpoint_revision=new_revision),
            projection_assignment=(
                "" if head.checkpoint_active else "projection_json=?,"),
            projection_args=(
                () if head.checkpoint_active else (_dump({}),)),
        )
        if changed == 1:
            _upsert_projection_checkpoint(
                session,
                turn_id=turn_id,
                conversation_id=str(row["conversation_id"]),
                user_id=int(row["user_id"]),
                attempt_id=attempt_id,
                revision=new_revision,
                projection_json=checkpoint_projection,
                now=now,
            )
    else:
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=str(row["status"] or ""),
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=head,
        )
    if changed != 1:
        raise StorageError(
            "database_conflict",
            "The turn changed during projection revision advance",
        )
    return new_revision


def write_turn_projection_revision(
    session: Session,
    *,
    turn_id: str,
    conversation_id: str,
    user_id: int,
    attempt_id: str,
    status: str,
    old_revision: int,
    new_revision: int,
    now: int,
    terminal: bool,
    slim: bool,
    settlement: Mapping[str, Any],
    projection_value: Any,
    previous_projection: Mapping[str, Any],
    next_projection: Mapping[str, Any],
    projection_is_mapping: bool,
    physical_matches_previous: bool,
    current_head: TurnProjectionHeadState,
    replay_projection_patch: Mapping[str, Any] | None,
    replay_patch_is_exact: bool,
    cache_charge_bytes: int,
    stored_payload_bytes: int | None,
    current_stored_payload_bytes: Callable[[], int],
) -> TurnProjectionWriteResult:
    """Execute one CAS update while minimizing full projection materialization."""
    projection_changed = (
        not projection_is_mapping or next_projection != previous_projection)
    patch_bytes = (
        len(_dump(replay_projection_patch))
        if replay_projection_patch is not None else 0
    )
    next_head = plan_projection_head_append(
        current_head,
        current_revision=old_revision,
        patch_bytes=patch_bytes,
        exact_patch=(
            replay_patch_is_exact and not terminal and projection_is_mapping),
        projection_changed=projection_changed,
    )
    write_settlement = terminal or not slim
    settlement_value = settlement if write_settlement else None
    materialized = TurnProjectionHeadState(
        None, 0, 0, current_head.checkpoint_revision)
    fully_materialized = TurnProjectionHeadState(None, 0, 0)
    inline_payload_bytes = int(stored_payload_bytes or 0)
    externalize_large_inline_projection = (
        not current_head.checkpoint_active
        and inline_payload_bytes > PROJECTION_INLINE_LIVE_MAX_BYTES
    )
    checkpoint_required = (
        not terminal
        and projection_is_mapping
        and (
            externalize_large_inline_projection
            or (
                next_head is None
                and (projection_changed or current_head.active)
            )
        )
    )

    if checkpoint_required:
        projection_json = _dump(next_projection)
        projection_bytes = len(projection_json)
        checkpoint_state = TurnProjectionHeadState(
            None, 0, 0, checkpoint_revision=new_revision)
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=status,
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=checkpoint_state,
            projection_assignment=(
                "" if current_head.checkpoint_active else "projection_json=?,"
            ),
            projection_args=(
                () if current_head.checkpoint_active else (_dump({}),)
            ),
            settlement=settlement_value,
        )
        if changed == 1:
            _upsert_projection_checkpoint(
                session,
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_id=user_id,
                attempt_id=attempt_id,
                revision=new_revision,
                projection_json=projection_json,
                now=now,
            )
        return TurnProjectionWriteResult(
            changed=changed,
            projection_bytes=projection_bytes,
            cache_charge_bytes=max(
                1, projection_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER),
            stored_payload_bytes=projection_bytes,
            stored_matches_projection=True,
            blob_write_skipped=False,
            blob_write_skipped_bytes=0,
            blob_write_deferred=False,
            head=checkpoint_state,
            checkpoint_materialized=changed == 1,
            checkpoint_materialized_bytes=projection_bytes if changed == 1 else 0,
            inline_projection_released_bytes=(
                inline_payload_bytes
                if changed == 1 and not current_head.checkpoint_active else 0
            ),
        )

    if next_head is not None:
        logical_projection_unchanged = (
            projection_is_mapping and not projection_changed)
        next_charge = max(
            1,
            cache_charge_bytes
            + patch_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER,
        )
        # ``projectionBytes`` is durable event evidence, not physical-write
        # accounting. A deferred structural target is deliberately not encoded;
        # report conservative baseline-plus-patches evidence instead. Slim
        # frames retain their established two-text-field evidence.
        projection_bytes = (
            len(_dump({
                "content": str(next_projection.get("content") or ""),
                "thinking": str(next_projection.get("thinking") or ""),
            }))
            if slim else max(1, next_charge // PROJECTION_CACHE_CHARGE_MULTIPLIER)
        )
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=status,
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=next_head,
            settlement=settlement_value,
        )
        return TurnProjectionWriteResult(
            changed=changed,
            projection_bytes=projection_bytes,
            cache_charge_bytes=next_charge,
            stored_payload_bytes=stored_payload_bytes,
            stored_matches_projection=False,
            blob_write_skipped=logical_projection_unchanged,
            blob_write_skipped_bytes=(
                int(stored_payload_bytes or 0)
                if logical_projection_unchanged else 0
            ),
            blob_write_deferred=not logical_projection_unchanged,
            head=next_head,
        )

    projection_unchanged_on_materialized_row = (
        not current_head.active
        and physical_matches_previous
        and projection_is_mapping
        and not projection_changed
    )
    if projection_unchanged_on_materialized_row:
        physical_bytes = current_stored_payload_bytes()
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=status,
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=materialized,
            settlement=settlement_value,
        )
        return TurnProjectionWriteResult(
            changed=changed,
            projection_bytes=physical_bytes,
            cache_charge_bytes=cache_charge_bytes,
            stored_payload_bytes=physical_bytes,
            stored_matches_projection=True,
            blob_write_skipped=True,
            blob_write_skipped_bytes=physical_bytes,
            blob_write_deferred=False,
            head=materialized,
        )

    if (
        slim
        and not current_head.active
        and not current_head.checkpoint_active
        and physical_matches_previous
    ):
        content = str(next_projection.get("content") or "")
        thinking = str(next_projection.get("thinking") or "")
        if session.backend == "postgres":
            projection_expression = (
                "jsonb_set(jsonb_set(projection_json, '{content}', "
                "to_jsonb(?::text), true), '{thinking}', "
                "to_jsonb(?::text), true)"
            )
        else:
            projection_expression = (
                "json_set(projection_json, '$.content', ?, '$.thinking', ?)"
            )
        changed = _update_projection_row(
            session,
            turn_id=turn_id,
            attempt_id=attempt_id,
            status=status,
            old_revision=old_revision,
            new_revision=new_revision,
            now=now,
            head=materialized,
            projection_assignment=f"projection_json={projection_expression},",
            projection_args=(content, thinking),
            settlement=settlement_value,
        )
        next_charge = max(
            1,
            cache_charge_bytes + PROJECTION_CACHE_CHARGE_MULTIPLIER * (
                projection_text_bytes(next_projection)
                - projection_text_bytes(previous_projection)
            ),
        )
        return TurnProjectionWriteResult(
            changed=changed,
            projection_bytes=len(_dump({
                "content": content,
                "thinking": thinking,
            })),
            cache_charge_bytes=next_charge,
            stored_payload_bytes=None,
            stored_matches_projection=True,
            blob_write_skipped=False,
            blob_write_skipped_bytes=0,
            blob_write_deferred=False,
            head=materialized,
        )

    materialized_projection = next_projection if slim else projection_value
    projection_json = _dump(materialized_projection)
    projection_bytes = len(projection_json)
    changed = _update_projection_row(
        session,
        turn_id=turn_id,
        attempt_id=attempt_id,
        status=status,
        old_revision=old_revision,
        new_revision=new_revision,
        now=now,
        head=fully_materialized,
        projection_assignment="projection_json=?,",
        projection_args=(projection_json,),
        settlement=settlement_value,
    )
    if changed == 1 and current_head.checkpoint_active:
        delete_turn_projection_checkpoint(session, turn_id)
    return TurnProjectionWriteResult(
        changed=changed,
        projection_bytes=projection_bytes,
        cache_charge_bytes=max(
            1, projection_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER),
        stored_payload_bytes=projection_bytes,
        stored_matches_projection=projection_is_mapping,
        blob_write_skipped=False,
        blob_write_skipped_bytes=0,
        blob_write_deferred=False,
        head=fully_materialized,
    )


__all__ = [
    "PROJECTION_INLINE_LIVE_MAX_BYTES",
    "TurnProjectionWriteResult",
    "advance_unchanged_projection_revision",
    "delete_turn_projection_checkpoint",
    "write_turn_projection_revision",
]

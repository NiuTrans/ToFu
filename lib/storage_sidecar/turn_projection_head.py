"""Bounded durable patch heads for live Turn projections.

Responsibility
--------------
Reconstruct the current projection from one inline or vertically isolated
checkpoint document and the exact revision patches already stored in
``storage_attempt_events``. The Turn row owns only bounded checkpoint/head
metadata; this module never creates a second patch log and never repairs a
malformed chain silently.

Entry points are :func:`projection_head_state`,
:func:`fold_turn_projection_head`, :func:`projection_from_turn_row`, and
:func:`discard_projection_cache_for_row`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _dump, _load
from lib.storage_sidecar.turn_projection_cache import (
    PROJECTION_CACHE_CHARGE_MULTIPLIER,
    TurnProjectionCache,
    projection_cache_key,
)
from lib.turn_projection_patch import ProjectionPatchError, apply_projection_patch


PROJECTION_HEAD_MAX_PATCHES = 64
PROJECTION_HEAD_MAX_PATCH_BYTES = 1024 * 1024
_PROJECTION_HEAD_QUERY_ROW_LIMIT = PROJECTION_HEAD_MAX_PATCHES * 4 + 1
_MISSING = object()


@dataclass(frozen=True, slots=True)
class TurnProjectionHeadState:
    """Small authoritative description of one materialized+patch chain."""

    materialized_revision: int | None
    patch_count: int
    patch_bytes: int
    checkpoint_revision: int | None = None

    @property
    def active(self) -> bool:
        return self.materialized_revision is not None

    @property
    def checkpoint_active(self) -> bool:
        return self.checkpoint_revision is not None


@dataclass(frozen=True, slots=True)
class TurnProjectionBase:
    """Decoded materialized base plus exact storage accounting evidence."""

    projection: Any
    payload_bytes: int
    from_checkpoint: bool


def plan_projection_head_append(
    state: TurnProjectionHeadState,
    *,
    current_revision: int,
    patch_bytes: int,
    exact_patch: bool,
    projection_changed: bool,
) -> TurnProjectionHeadState | None:
    """Plan one bounded append, or require a full materialization.

    An unchanged projection with no existing head needs no chain: its physical
    document remains valid at the advanced revision. Once a head exists, even
    an empty patch is retained so every revision stays reconstructible.
    """
    revision = _nonnegative_integer(current_revision, "projection revision")
    encoded_bytes = _nonnegative_integer(patch_bytes, "projection patch bytes")
    if not exact_patch or not encoded_bytes:
        return None
    # An inline materialized row may advance across an unchanged projection
    # without retaining an empty patch: the row itself remains the current
    # base. An external checkpoint is revision-fenced, however. Its first
    # unchanged revision must start a one-patch head so the Turn row never
    # advances past the checkpoint revision with no reconstructible bridge.
    if (
        not state.active
        and not state.checkpoint_active
        and not projection_changed
    ):
        return None
    if state.active:
        materialized_revision = state.materialized_revision
        if (
            materialized_revision is None
            or revision - materialized_revision != state.patch_count
        ):
            raise StorageError(
                "database_integrity",
                "Turn projection patch head is not revision-contiguous",
            )
        next_count = state.patch_count + 1
        next_bytes = state.patch_bytes + encoded_bytes
    else:
        materialized_revision = revision
        next_count = 1
        next_bytes = encoded_bytes
    if (
        next_count > PROJECTION_HEAD_MAX_PATCHES
        or next_bytes > PROJECTION_HEAD_MAX_PATCH_BYTES
    ):
        return None
    return TurnProjectionHeadState(
        materialized_revision=materialized_revision,
        patch_count=next_count,
        patch_bytes=next_bytes,
        checkpoint_revision=state.checkpoint_revision,
    )


def _row_value(
    row: Mapping[str, Any], field: str, default: Any = _MISSING,
) -> Any:
    try:
        return row[field]
    except (IndexError, KeyError, TypeError):
        if default is _MISSING:
            raise StorageError(
                "database_integrity", f"Turn row is missing {field}") from None
        return default


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError(
            "database_integrity", f"Turn {field} must be a nonnegative integer")
    return value


def projection_head_state(row: Mapping[str, Any]) -> TurnProjectionHeadState:
    """Decode and validate bounded head metadata from a Turn row."""
    raw_checkpoint = _row_value(
        row, "projection_checkpoint_revision", None)
    checkpoint_revision = (
        None
        if raw_checkpoint is None
        else _nonnegative_integer(
            raw_checkpoint, "projection checkpoint revision")
    )
    raw_materialized = _row_value(
        row, "projection_materialized_revision", None)
    materialized_revision = (
        None
        if raw_materialized is None
        else _nonnegative_integer(
            raw_materialized, "projection materialized revision")
    )
    patch_count = _nonnegative_integer(
        _row_value(row, "projection_patch_count", 0),
        "projection patch count",
    )
    patch_bytes = _nonnegative_integer(
        _row_value(row, "projection_patch_bytes", 0),
        "projection patch bytes",
    )
    if materialized_revision is None:
        if patch_count or patch_bytes:
            raise StorageError(
                "database_integrity",
                "Materialized Turn has residual projection patch metadata",
            )
    elif (
        patch_count < 1
        or patch_count > PROJECTION_HEAD_MAX_PATCHES
        or patch_bytes < 1
        or patch_bytes > PROJECTION_HEAD_MAX_PATCH_BYTES
    ):
        raise StorageError(
            "database_integrity", "Turn projection patch head exceeds its budget")
    if checkpoint_revision is not None:
        current_revision = _nonnegative_integer(
            _row_value(row, "projection_revision"), "projection revision")
        expected_checkpoint_revision = (
            materialized_revision
            if materialized_revision is not None else current_revision
        )
        if checkpoint_revision != expected_checkpoint_revision:
            raise StorageError(
                "database_integrity",
                "Turn projection checkpoint revision is inconsistent",
            )
    return TurnProjectionHeadState(
        materialized_revision=materialized_revision,
        patch_count=patch_count,
        patch_bytes=patch_bytes,
        checkpoint_revision=checkpoint_revision,
    )


def _stored_payload_bytes(raw: Any, decoded: Any) -> int:
    if isinstance(raw, bytes):
        return len(raw)
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    return len(_dump(decoded))


def load_turn_projection_base(
    session: Session,
    row: Mapping[str, Any],
    *,
    state: TurnProjectionHeadState | None = None,
    require_mapping: bool = True,
) -> TurnProjectionBase:
    """Load exactly one inline/checkpoint base with owner and revision fences."""
    head = state or projection_head_state(row)
    if head.checkpoint_active:
        checkpoint = session.fetch_one(
            "SELECT projection_json,projection_bytes FROM "
            "storage_turn_projection_checkpoints WHERE turn_id=? "
            "AND conversation_id=? AND user_id=? AND attempt_id=? "
            "AND projection_revision=?",
            (
                str(_row_value(row, "turn_id")),
                str(_row_value(row, "conversation_id")),
                _nonnegative_integer(_row_value(row, "user_id"), "owner"),
                str(_row_value(row, "current_attempt_id", "") or ""),
                head.checkpoint_revision,
            ),
        )
        if checkpoint is None:
            raise StorageError(
                "database_integrity", "Turn projection checkpoint is missing")
        raw_projection = checkpoint["projection_json"]
        declared_bytes = _nonnegative_integer(
            checkpoint["projection_bytes"], "projection checkpoint bytes")
        if declared_bytes < 1:
            raise StorageError(
                "database_integrity", "Turn projection checkpoint is empty")
        decoded = _load(raw_projection)
        if not isinstance(decoded, Mapping):
            raise StorageError(
                "database_integrity",
                "Stored turn projection checkpoint must be an object",
            )
        if isinstance(raw_projection, bytes):
            actual_bytes = len(raw_projection)
        elif isinstance(raw_projection, str):
            actual_bytes = len(raw_projection.encode("utf-8"))
        else:
            actual_bytes = declared_bytes
        if actual_bytes != declared_bytes:
            raise StorageError(
                "database_integrity",
                "Turn projection checkpoint byte evidence is inconsistent",
            )
        return TurnProjectionBase(
            dict(decoded), declared_bytes, from_checkpoint=True)

    try:
        raw_projection = row["projection_json"]
    except (IndexError, KeyError, TypeError):
        attempt_id = str(_row_value(row, "current_attempt_id", "") or "")
        inline = session.fetch_one(
            "SELECT projection_json FROM storage_conversation_turns "
            "WHERE turn_id=? AND conversation_id=? AND user_id=? "
            "AND projection_revision=?"
            + (" AND current_attempt_id=?" if attempt_id else ""),
            (
                str(_row_value(row, "turn_id")),
                str(_row_value(row, "conversation_id")),
                _nonnegative_integer(_row_value(row, "user_id"), "owner"),
                _nonnegative_integer(
                    _row_value(row, "projection_revision"),
                    "projection revision",
                ),
            ) + ((attempt_id,) if attempt_id else ()),
        )
        if inline is None:
            raise StorageError(
                "database_conflict", "Turn projection changed during hydration")
        raw_projection = inline["projection_json"]
    decoded = _load(raw_projection)
    if require_mapping and not isinstance(decoded, Mapping):
        raise StorageError(
            "database_integrity", "Stored turn projection must be an object")
    return TurnProjectionBase(
        dict(decoded) if isinstance(decoded, Mapping) else decoded,
        _stored_payload_bytes(raw_projection, decoded),
        from_checkpoint=False,
    )


def _patch_from_event(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    envelope = _load(row["payload_json"])
    if not isinstance(envelope, Mapping):
        raise StorageError(
            "database_integrity", "Turn projection event envelope is malformed")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return None
    patch = payload.get("projectionPatch")
    return patch if isinstance(patch, Mapping) else None


def fold_turn_projection_head(
    session: Session,
    row: Mapping[str, Any],
    base_projection: Mapping[str, Any],
    *,
    state: TurnProjectionHeadState | None = None,
) -> dict[str, Any]:
    """Fold one exact, bounded durable chain or fail closed."""
    head = state or projection_head_state(row)
    if not head.active:
        return dict(base_projection)
    current_revision = _nonnegative_integer(
        _row_value(row, "projection_revision"), "projection revision")
    materialized_revision = head.materialized_revision
    if (
        materialized_revision is None
        or materialized_revision >= current_revision
        or current_revision - materialized_revision != head.patch_count
    ):
        raise StorageError(
            "database_integrity", "Turn projection patch revisions are inconsistent")
    attempt_id = str(_row_value(row, "current_attempt_id", "") or "")
    if not attempt_id:
        raise StorageError(
            "database_integrity", "Turn projection patch head has no attempt")
    event_rows = session.fetch_all(
        "SELECT projection_revision,payload_json FROM storage_attempt_events "
        "WHERE attempt_id=? AND projection_revision>? "
        "AND projection_revision<=? "
        "ORDER BY sequence LIMIT ?",
        (
            attempt_id,
            materialized_revision,
            current_revision,
            _PROJECTION_HEAD_QUERY_ROW_LIMIT,
        ),
    )
    if len(event_rows) >= _PROJECTION_HEAD_QUERY_ROW_LIMIT:
        raise StorageError(
            "database_integrity", "Turn projection patch event range is ambiguous")
    patches: dict[int, Mapping[str, Any]] = {}
    for event_row in event_rows:
        revision = _nonnegative_integer(
            event_row["projection_revision"], "event projection revision")
        patch = _patch_from_event(event_row)
        if patch is None:
            continue
        if revision in patches:
            raise StorageError(
                "database_integrity", "Turn projection revision has duplicate patches")
        patches[revision] = patch
    projection = dict(base_projection)
    for revision in range(materialized_revision + 1, current_revision + 1):
        patch = patches.get(revision)
        if patch is None:
            raise StorageError(
                "database_integrity", "Turn projection patch chain has a gap")
        if (
            patch.get("baseRevision") != revision - 1
            or patch.get("targetRevision") != revision
        ):
            raise StorageError(
                "database_integrity", "Turn projection patch chain is misbased")
        try:
            projection = apply_projection_patch(projection, patch)
        except ProjectionPatchError as exc:
            raise StorageError(
                "database_integrity", "Turn projection patch chain is invalid") from exc
    return projection


def projection_from_turn_row(
    session: Session, row: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the current projection, using a revision-exact cache when live."""
    state = projection_head_state(row)
    revision = _nonnegative_integer(
        _row_value(row, "projection_revision"), "projection revision")
    attempt_id = str(_row_value(row, "current_attempt_id", "") or "")
    cache = getattr(session, "turn_projection_cache", None)
    cache = cache if isinstance(cache, TurnProjectionCache) else None
    cache_key = None
    cache_eligible = (
        state.active or state.checkpoint_active
        or str(_row_value(row, "status", "") or "") in ("pending", "running")
    )
    if attempt_id and cache is not None and cache_eligible:
        cache_key = projection_cache_key(
            session.backend,
            _nonnegative_integer(_row_value(row, "user_id"), "owner"),
            str(_row_value(row, "conversation_id")),
            str(_row_value(row, "turn_id")),
            attempt_id,
        )
        entry = cache.get(cache_key, revision=revision)
        if entry is not None:
            return entry.projection
    base = load_turn_projection_base(session, row, state=state)
    projection = fold_turn_projection_head(
        session, row, base.projection, state=state)
    if cache is not None and cache_key is not None:
        cache.remember(
            cache_key,
            revision=revision,
            projection=projection,
            charge_bytes=max(
                1,
                (base.payload_bytes + state.patch_bytes)
                * PROJECTION_CACHE_CHARGE_MULTIPLIER,
            ),
            stored_payload_bytes=base.payload_bytes,
            stored_matches_projection=not state.active,
            stable_segments=False,
        )
    return projection


def discard_projection_cache_for_row(
    session: Session, row: Mapping[str, Any],
) -> bool:
    """Release one row's reconstructible live baseline after materialization."""
    attempt_id = str(_row_value(row, "current_attempt_id", "") or "")
    cache = getattr(session, "turn_projection_cache", None)
    if not attempt_id or not isinstance(cache, TurnProjectionCache):
        return False
    return cache.discard(projection_cache_key(
        session.backend,
        _nonnegative_integer(_row_value(row, "user_id"), "owner"),
        str(_row_value(row, "conversation_id")),
        str(_row_value(row, "turn_id")),
        attempt_id,
    ))


__all__ = [
    "PROJECTION_HEAD_MAX_PATCH_BYTES",
    "PROJECTION_HEAD_MAX_PATCHES",
    "TurnProjectionBase",
    "TurnProjectionHeadState",
    "discard_projection_cache_for_row",
    "fold_turn_projection_head",
    "load_turn_projection_base",
    "plan_projection_head_append",
    "projection_from_turn_row",
    "projection_head_state",
]

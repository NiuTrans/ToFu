"""Turn branch lane handlers."""
from __future__ import annotations
from typing import Any
from collections.abc import Mapping
from lib.storage_sidecar.adapters.base import Session
from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _required_text,
)
from lib.storage_sidecar.turn_projection_head import projection_from_turn_row
from lib.storage_sidecar.turn_projection_write import (
    delete_turn_projection_checkpoint,
)
import time
import uuid
from lib.storage_sidecar.operations_pkg._turns_core import _projection_change, _turn_identity, _turn_public, _upsert_turn_search_row
from lib.storage_sidecar.operations_pkg._turns_lifecycle import _delete_turn_row_set, _turn_deletion_closure, _turn_row_is_live
from lib.storage_sidecar.operations_pkg._turns_read import _prune_turn_tombstones


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
    public = _turn_public(session, parent)
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
    changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,"
        "projection_revision=?,projection_checkpoint_revision=NULL,"
        "projection_materialized_revision=NULL,"
        "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
        "WHERE turn_id=? AND projection_revision=?",
        (_dump(projection), revision, now, parent_id, expected),
    )
    if changed != 1:
        raise StorageError(
            "database_conflict", "The parent turn changed during branch creation")
    delete_turn_projection_checkpoint(session, parent_id)
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
        "turn": _turn_public(session, updated),
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
    projection = projection_from_turn_row(session, parent)
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
        "UPDATE storage_conversation_turns SET projection_json=?,"
        "projection_revision=?,projection_checkpoint_revision=NULL,"
        "projection_materialized_revision=NULL,"
        "projection_patch_count=0,projection_patch_bytes=0,updated_at=? "
        "WHERE turn_id=?",
        (_dump(projection), revision, now, parent_id),
    )
    delete_turn_projection_checkpoint(session, parent_id)
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
        "turn": _turn_public(session, updated),
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

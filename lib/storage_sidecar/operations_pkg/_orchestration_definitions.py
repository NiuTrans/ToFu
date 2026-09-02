"""Owner-scoped orchestration-definition operations with atomic CAS."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.orchestration.definition_contract_registry import MAX_DEFINITION_VERSION
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._runs import _json_text


_DEFINITION_COLUMNS = (
    "id, user_id, tenant_id, name, definition_json, "
    "created_at_ms, updated_at_ms"
)


def _workflow_owner(payload: Mapping[str, Any]) -> tuple[int, str]:
    tenant_id = payload.get("tenant_id", "")
    if not isinstance(tenant_id, str) or len(tenant_id) > 256:
        raise StorageError(
            "database_protocol_error", "Invalid tenant_id in orchestration request")
    return _integer(payload, "user_id", minimum=1), tenant_id.strip()


def _definition_document(row: Mapping[str, Any]) -> dict[str, Any]:
    definition = _load(row["definition_json"])
    if not isinstance(definition, dict):
        raise StorageError(
            "database_integrity", "Orchestration definition is not an object")
    return {
        "id": str(row["id"]),
        "name": str(row["name"] or ""),
        "definition": definition,
        "createdAt": int(row["created_at_ms"]),
        "updatedAt": int(row["updated_at_ms"]),
    }


def _orchestration_definition_list(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    user_id, tenant_id = _workflow_owner(payload)
    rows = session.fetch_all(
        f"SELECT {_DEFINITION_COLUMNS} FROM orchestration_definitions "
        "WHERE user_id = ? AND tenant_id = ? "
        "ORDER BY updated_at_ms DESC, id",
        (user_id, tenant_id),
    )
    return [_definition_document(row) for row in rows]


def _orchestration_definition_get(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    user_id, tenant_id = _workflow_owner(payload)
    row = session.fetch_one(
        f"SELECT {_DEFINITION_COLUMNS} FROM orchestration_definitions "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (
            _required_text(payload, "orchestration_id", 200),
            user_id,
            tenant_id,
        ),
    )
    return None if row is None else _definition_document(row)


def _orchestration_definition_create(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    orchestration_id = _required_text(payload, "orchestration_id", 200)
    user_id, tenant_id = _workflow_owner(payload)
    definition = payload.get("definition")
    if not isinstance(definition, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration definition")
    now_ms = _integer(payload, "now_ms", minimum=0)
    session.lock_key("orchestration.definition", orchestration_id)
    if session.fetch_one(
        "SELECT 1 AS present FROM orchestration_definitions WHERE id = ?",
        (orchestration_id,),
    ):
        raise StorageError(
            "database_conflict", "Orchestration definition id already exists")
    session.execute(
        "INSERT INTO orchestration_definitions("
        "id, user_id, tenant_id, name, definition_json, "
        "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            orchestration_id,
            user_id,
            tenant_id,
            str(definition.get("name") or ""),
            _json_text(dict(definition)),
            now_ms,
            now_ms,
        ),
    )
    return _orchestration_definition_get(session, payload)


def _orchestration_definition_update(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    orchestration_id = _required_text(payload, "orchestration_id", 200)
    user_id, tenant_id = _workflow_owner(payload)
    definition = payload.get("definition")
    if not isinstance(definition, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration definition")
    expected = _integer(
        payload, "expected_updated_at", minimum=0,
        maximum=MAX_DEFINITION_VERSION,
    )
    session.lock_key("orchestration.definition", orchestration_id)
    current = session.fetch_one(
        "SELECT updated_at_ms FROM orchestration_definitions "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (orchestration_id, user_id, tenant_id),
    )
    if current is None:
        return {
            "entry": None,
            "conflict": False,
            "current_updated_at": None,
            "deleted": False,
        }
    current_updated_at = int(current["updated_at_ms"])
    if expected != current_updated_at:
        return {
            "entry": None,
            "conflict": True,
            "current_updated_at": current_updated_at,
            "deleted": False,
        }
    updated_at = max(
        _integer(payload, "now_ms", minimum=0), current_updated_at + 1)
    session.execute(
        "UPDATE orchestration_definitions SET name = ?, definition_json = ?, "
        "updated_at_ms = ? WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (
            str(definition.get("name") or ""),
            _json_text(dict(definition)),
            updated_at,
            orchestration_id,
            user_id,
            tenant_id,
        ),
    )
    entry = _orchestration_definition_get(session, payload)
    return {
        "entry": entry,
        "conflict": False,
        "current_updated_at": updated_at,
        "deleted": False,
    }


def _orchestration_definition_delete(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    orchestration_id = _required_text(payload, "orchestration_id", 200)
    user_id, tenant_id = _workflow_owner(payload)
    expected = _integer(
        payload, "expected_updated_at", minimum=0,
        maximum=MAX_DEFINITION_VERSION,
    )
    session.lock_key("orchestration.definition", orchestration_id)
    current = session.fetch_one(
        "SELECT updated_at_ms FROM orchestration_definitions "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (orchestration_id, user_id, tenant_id),
    )
    if current is None:
        return {
            "entry": None,
            "conflict": False,
            "current_updated_at": None,
            "deleted": False,
        }
    current_updated_at = int(current["updated_at_ms"])
    if expected != current_updated_at:
        return {
            "entry": None,
            "conflict": True,
            "current_updated_at": current_updated_at,
            "deleted": False,
        }
    session.execute(
        "DELETE FROM orchestration_definitions "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (orchestration_id, user_id, tenant_id),
    )
    return {
        "entry": None,
        "conflict": False,
        "current_updated_at": current_updated_at,
        "deleted": True,
    }


__all__ = [
    "_DEFINITION_COLUMNS",
    "_definition_document",
    "_orchestration_definition_create",
    "_orchestration_definition_delete",
    "_orchestration_definition_get",
    "_orchestration_definition_list",
    "_orchestration_definition_update",
    "_workflow_owner",
]

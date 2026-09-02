"""Backend-neutral Sidecar operations for owner-scoped BYO providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _load,
    _number,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._runs import _json_text


_PROVIDER_COLUMNS = (
    "id, owner_user_id, tenant_id, name, base_url, api_key_ciphertext, "
    "key_hint, models_json, extra_headers_json, thinking_format, disabled, "
    "created_at, updated_at, last_used_at"
)
_MAX_PROVIDERS_PER_OWNER = 32


def _optional_text(payload: Mapping[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in provider request")
    return value.strip()


def _owner_boundary(payload: Mapping[str, Any]) -> tuple[int, str]:
    return (
        _integer(payload, "owner_user_id", minimum=1),
        _optional_text(payload, "tenant_id", 256),
    )


def _json_array(payload: Mapping[str, Any], key: str, *, maximum: int) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in provider request")
    return list(value)


def _json_object(payload: Mapping[str, Any], key: str, *, maximum: int) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in provider request")
    return dict(value)


def _provider_document(
    row: Mapping[str, Any], *, include_ciphertext: bool,
) -> dict[str, Any]:
    models = _load(row["models_json"])
    headers = _load(row["extra_headers_json"])
    if not isinstance(models, list) or not isinstance(headers, dict):
        raise StorageError(
            "database_integrity", "BYO provider documents are invalid")
    document: dict[str, Any] = {
        "id": str(row["id"]),
        "owner_user_id": int(row["owner_user_id"]),
        "tenant_id": str(row["tenant_id"] or ""),
        "name": str(row["name"]),
        "base_url": str(row["base_url"]),
        "key_hint": str(row["key_hint"] or ""),
        "models": models,
        "extra_headers": headers,
        "thinking_format": str(row["thinking_format"] or ""),
        "disabled": bool(row["disabled"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "last_used_at": (
            None if row["last_used_at"] is None
            else float(row["last_used_at"])
        ),
    }
    if include_ciphertext:
        document["api_key_ciphertext"] = str(row["api_key_ciphertext"] or "")
    return document


def _provider_list(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    rows = session.fetch_all(
        f"SELECT {_PROVIDER_COLUMNS} FROM byo_providers "
        "WHERE owner_user_id = ? AND tenant_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (owner_user_id, tenant_id),
    )
    return [
        _provider_document(row, include_ciphertext=False) for row in rows
    ]


def _provider_get(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    row = session.fetch_one(
        f"SELECT {_PROVIDER_COLUMNS} FROM byo_providers "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        (
            _required_text(payload, "provider_id", 128),
            owner_user_id,
            tenant_id,
        ),
    )
    return (
        None if row is None
        else _provider_document(row, include_ciphertext=True)
    )


def _provider_create(session: Session, payload: Mapping[str, Any]) -> Any:
    provider_id = _required_text(payload, "provider_id", 128)
    owner_user_id, tenant_id = _owner_boundary(payload)
    name = _required_text(payload, "name", 80).strip()
    base_url = _required_text(payload, "base_url", 500).strip()
    ciphertext = _optional_text(payload, "api_key_ciphertext", 32768)
    key_hint = _optional_text(payload, "key_hint", 64)
    models = _json_array(payload, "models", maximum=64)
    headers = _json_object(payload, "extra_headers", maximum=16)
    thinking_format = _optional_text(payload, "thinking_format", 64)
    created_at = _number(
        payload, "created_at", minimum=0, maximum=32_503_680_000)

    boundary_key = f"{tenant_id}:{owner_user_id}"
    session.lock_key("provider.owner", boundary_key)
    count = session.fetch_one(
        "SELECT COUNT(*) AS n FROM byo_providers "
        "WHERE owner_user_id = ? AND tenant_id = ?",
        (owner_user_id, tenant_id),
    )
    if int((count or {}).get("n") or 0) >= _MAX_PROVIDERS_PER_OWNER:
        raise StorageError(
            "database_conflict",
            f"provider quota reached ({_MAX_PROVIDERS_PER_OWNER} per owner)",
        )
    if session.fetch_one(
        "SELECT 1 AS present FROM byo_providers WHERE id = ?", (provider_id,),
    ):
        raise StorageError("database_conflict", "Provider already exists")
    session.execute(
        "INSERT INTO byo_providers("
        "id, owner_user_id, tenant_id, name, base_url, api_key_ciphertext, "
        "key_hint, models_json, extra_headers_json, thinking_format, disabled, "
        "created_at, updated_at, last_used_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)",
        (
            provider_id,
            owner_user_id,
            tenant_id,
            name,
            base_url,
            ciphertext,
            key_hint,
            _json_text(models),
            _json_text(headers),
            thinking_format,
            created_at,
            created_at,
        ),
    )
    return _provider_get(session, payload)


def _provider_update(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    provider_id = _required_text(payload, "provider_id", 128)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise StorageError(
            "database_protocol_error", "Provider updates must be an object")
    allowed = {
        "name",
        "base_url",
        "api_key_ciphertext",
        "key_hint",
        "models",
        "extra_headers",
        "thinking_format",
        "disabled",
    }
    if any(key not in allowed for key in updates):
        raise StorageError(
            "database_protocol_error", "Provider update contains unknown fields")
    assignments: list[str] = []
    parameters: list[Any] = []
    for key, value in updates.items():
        if key == "name":
            column = key
            normalized = _required_text({key: value}, key, 80).strip()
        elif key == "base_url":
            column = key
            normalized = _required_text({key: value}, key, 500).strip()
        elif key == "api_key_ciphertext":
            column = key
            normalized = _optional_text({key: value}, key, 32768)
        elif key == "key_hint":
            column = key
            normalized = _optional_text({key: value}, key, 64)
        elif key == "models":
            column = "models_json"
            normalized = _json_text(
                _json_array({key: value}, key, maximum=64))
        elif key == "extra_headers":
            column = "extra_headers_json"
            normalized = _json_text(
                _json_object({key: value}, key, maximum=16))
        elif key == "thinking_format":
            column = key
            normalized = _optional_text({key: value}, key, 64)
        else:
            column = key
            if not isinstance(value, bool):
                raise StorageError(
                    "database_protocol_error",
                    "Provider disabled update must be boolean",
                )
            normalized = int(value)
        assignments.append(f"{column} = ?")
        parameters.append(normalized)
    assignments.append("updated_at = ?")
    parameters.append(
        _number(payload, "updated_at", minimum=0, maximum=32_503_680_000))
    parameters.extend((provider_id, owner_user_id, tenant_id))
    session.execute(
        "UPDATE byo_providers SET " + ", ".join(assignments) +
        " WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        tuple(parameters),
    )
    return _provider_get(session, payload)


def _provider_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    provider_id = _required_text(payload, "provider_id", 128)
    deleted = session.execute(
        "DELETE FROM byo_providers "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        (provider_id, owner_user_id, tenant_id),
    )
    return {"deleted": bool(deleted)}


def _provider_touch(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    provider_id = _required_text(payload, "provider_id", 128)
    touched = session.execute(
        "UPDATE byo_providers SET last_used_at = ? "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        (
            _number(payload, "used_at", minimum=0, maximum=32_503_680_000),
            provider_id,
            owner_user_id,
            tenant_id,
        ),
    )
    return {"touched": bool(touched)}


__all__ = [
    "_MAX_PROVIDERS_PER_OWNER",
    "_PROVIDER_COLUMNS",
    "_provider_create",
    "_provider_delete",
    "_provider_document",
    "_provider_get",
    "_provider_list",
    "_provider_touch",
    "_provider_update",
]

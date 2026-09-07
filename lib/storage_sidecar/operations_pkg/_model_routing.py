"""Backend-neutral persistence for owner-scoped model-routing v2 aggregates."""

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


_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_SECRETS_PER_OWNER = 1024
_MAX_CIPHERTEXT_LENGTH = 32768


def _optional_text(payload: Mapping[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in model-routing request")
    return value.strip()


def _owner_boundary(payload: Mapping[str, Any]) -> tuple[int, str]:
    return (
        _integer(payload, "owner_user_id", minimum=1),
        _optional_text(payload, "tenant_id", 256),
    )


def _document_payload(payload: Mapping[str, Any], key: str) -> tuple[dict[str, Any], str]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in model-routing request")
    document = dict(value)
    encoded = _json_text(document)
    if len(encoded.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise StorageError(
            "database_protocol_error",
            f"Model-routing document exceeds {_MAX_DOCUMENT_BYTES} bytes",
        )
    return document, encoded


def _decoded_object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = _load(value)
    if not isinstance(decoded, dict):
        raise StorageError(
            "database_integrity", f"Stored model-routing {field} is invalid")
    return decoded


def _authority_document(row: Mapping[str, Any]) -> dict[str, Any]:
    document = _decoded_object(row["document_json"], "document")
    if document is None:
        raise StorageError("database_integrity", "Stored model-routing document is empty")
    return {
        "owner_user_id": int(row["owner_user_id"]),
        "tenant_id": str(row["tenant_id"] or ""),
        "revision": int(row["revision"]),
        "document": document,
        "updated_at": float(row["updated_at"]),
    }


def _model_routing_get(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    row = session.fetch_one(
        "SELECT owner_user_id, tenant_id, revision, document_json, updated_at "
        "FROM storage_model_routing_authorities "
        "WHERE owner_user_id = ? AND tenant_id = ?",
        (owner_user_id, tenant_id),
    )
    return None if row is None else _authority_document(row)


def _model_routing_commit(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    expected_revision = _integer(payload, "expected_revision", minimum=0)
    document, encoded = _document_payload(payload, "document")
    next_revision = expected_revision + 1
    if document.get("contract_version") != "tofu.model-routing/v2":
        raise StorageError(
            "database_protocol_error", "Model-routing contract_version is invalid")
    if document.get("revision") != next_revision:
        raise StorageError(
            "database_protocol_error",
            "Model-routing document revision must equal expected_revision + 1",
        )
    updated_at = _number(
        payload, "updated_at", minimum=0, maximum=32_503_680_000)
    migration_receipt = payload.get("migration_receipt")
    if migration_receipt is not None and not isinstance(migration_receipt, Mapping):
        raise StorageError(
            "database_protocol_error", "migration_receipt must be an object")

    boundary_key = f"{tenant_id}:{owner_user_id}"
    session.lock_key("model_routing.owner", boundary_key)
    current = session.fetch_one(
        "SELECT revision, document_json FROM storage_model_routing_authorities "
        "WHERE owner_user_id = ? AND tenant_id = ?",
        (owner_user_id, tenant_id),
    )
    current_revision = int(current["revision"]) if current is not None else 0
    if current_revision != expected_revision:
        raise StorageError(
            "database_conflict",
            f"Model-routing revision changed from {expected_revision} to {current_revision}",
        )

    if current is None:
        session.execute(
            "INSERT INTO storage_model_routing_authorities("
            "owner_user_id, tenant_id, revision, document_json, backup_json, "
            "migration_receipt_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner_user_id,
                tenant_id,
                next_revision,
                encoded,
                None,
                _json_text(dict(migration_receipt)) if migration_receipt is not None else None,
                updated_at,
            ),
        )
    else:
        if migration_receipt is None:
            session.execute(
                "UPDATE storage_model_routing_authorities SET revision = ?, "
                "document_json = ?, updated_at = ? "
                "WHERE owner_user_id = ? AND tenant_id = ?",
                (next_revision, encoded, updated_at, owner_user_id, tenant_id),
            )
        else:
            session.execute(
                "UPDATE storage_model_routing_authorities SET revision = ?, "
                "document_json = ?, backup_json = ?, migration_receipt_json = ?, "
                "updated_at = ? WHERE owner_user_id = ? AND tenant_id = ?",
                (
                    next_revision,
                    encoded,
                    current["document_json"],
                    _json_text(dict(migration_receipt)),
                    updated_at,
                    owner_user_id,
                    tenant_id,
                ),
            )
    # Command receipts are intentionally much smaller than the aggregate
    # budget.  Returning the full document here made a valid multi-provider
    # migration roll back while encoding its exactly-once receipt.  The
    # caller already supplied the normalized document, so only acknowledge
    # the committed revision at this mutation boundary.
    return {
        "owner_user_id": owner_user_id,
        "tenant_id": tenant_id,
        "revision": next_revision,
        "updated_at": updated_at,
    }


def _model_routing_migration_receipt(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    row = session.fetch_one(
        "SELECT revision, backup_json, migration_receipt_json, updated_at "
        "FROM storage_model_routing_authorities "
        "WHERE owner_user_id = ? AND tenant_id = ?",
        (owner_user_id, tenant_id),
    )
    if row is None or row["migration_receipt_json"] is None:
        return None
    return {
        "revision": int(row["revision"]),
        "backup": _decoded_object(row["backup_json"], "migration backup"),
        "receipt": _decoded_object(
            row["migration_receipt_json"], "migration receipt"),
        "updated_at": float(row["updated_at"]),
    }


def _model_routing_migration_receipt_put(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Persist a failed/rejected receipt without switching route authority."""
    owner_user_id, tenant_id = _owner_boundary(payload)
    receipt = payload.get("migration_receipt")
    if not isinstance(receipt, Mapping):
        raise StorageError(
            "database_protocol_error", "migration_receipt must be an object")
    encoded_receipt = _json_text(dict(receipt))
    if len(encoded_receipt.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise StorageError(
            "database_protocol_error", "migration_receipt exceeds its resource budget")
    updated_at = _number(
        payload, "updated_at", minimum=0, maximum=32_503_680_000)
    boundary_key = f"{tenant_id}:{owner_user_id}"
    session.lock_key("model_routing.owner", boundary_key)
    current = session.fetch_one(
        "SELECT revision FROM storage_model_routing_authorities "
        "WHERE owner_user_id = ? AND tenant_id = ?",
        (owner_user_id, tenant_id),
    )
    if current is None:
        document, encoded_document = _document_payload(payload, "document")
        if (document.get("contract_version") != "tofu.model-routing/v2"
                or document.get("revision") != 0):
            raise StorageError(
                "database_protocol_error",
                "initial model-routing receipt document must be empty revision 0",
            )
        session.execute(
            "INSERT INTO storage_model_routing_authorities("
            "owner_user_id, tenant_id, revision, document_json, backup_json, "
            "migration_receipt_json, updated_at) VALUES (?, ?, 0, ?, NULL, ?, ?)",
            (owner_user_id, tenant_id, encoded_document, encoded_receipt, updated_at),
        )
    else:
        session.execute(
            "UPDATE storage_model_routing_authorities SET "
            "migration_receipt_json = ?, updated_at = ? "
            "WHERE owner_user_id = ? AND tenant_id = ?",
            (encoded_receipt, updated_at, owner_user_id, tenant_id),
        )
    # The redacted backup may legitimately approach the aggregate budget.
    # Keep the command response receipt-small; readers can fetch the complete
    # recovery receipt through ``model_routing.migration_receipt``.
    return {
        "owner_user_id": owner_user_id,
        "tenant_id": tenant_id,
        "revision": int(current["revision"]) if current is not None else 0,
        "status": str(receipt.get("status") or ""),
        "updated_at": updated_at,
    }


def _model_routing_secret_put(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    secret_reference = _required_text(payload, "secret_reference", 256)
    ciphertext = _required_text(payload, "ciphertext", _MAX_CIPHERTEXT_LENGTH)
    key_hint = _optional_text(payload, "key_hint", 64)
    updated_at = _number(
        payload, "updated_at", minimum=0, maximum=32_503_680_000)
    boundary_key = f"{tenant_id}:{owner_user_id}"
    session.lock_key("model_routing.secrets", boundary_key)
    current = session.fetch_one(
        "SELECT created_at FROM storage_model_routing_secrets WHERE "
        "owner_user_id = ? AND tenant_id = ? AND secret_reference = ?",
        (owner_user_id, tenant_id, secret_reference),
    )
    if current is None:
        count = session.fetch_one(
            "SELECT COUNT(*) AS n FROM storage_model_routing_secrets "
            "WHERE owner_user_id = ? AND tenant_id = ?",
            (owner_user_id, tenant_id),
        )
        if int((count or {}).get("n") or 0) >= _MAX_SECRETS_PER_OWNER:
            raise StorageError(
                "database_conflict",
                f"model-routing secret quota reached ({_MAX_SECRETS_PER_OWNER})",
            )
        session.execute(
            "INSERT INTO storage_model_routing_secrets("
            "owner_user_id, tenant_id, secret_reference, ciphertext, key_hint, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner_user_id, tenant_id, secret_reference, ciphertext,
                key_hint, updated_at, updated_at,
            ),
        )
    else:
        session.execute(
            "UPDATE storage_model_routing_secrets SET ciphertext = ?, "
            "key_hint = ?, updated_at = ? WHERE owner_user_id = ? "
            "AND tenant_id = ? AND secret_reference = ?",
            (
                ciphertext, key_hint, updated_at, owner_user_id, tenant_id,
                secret_reference,
            ),
        )
    return {
        "secret_reference": secret_reference,
        "key_hint": key_hint,
        "updated_at": updated_at,
    }


def _model_routing_secret_get(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    secret_reference = _required_text(payload, "secret_reference", 256)
    row = session.fetch_one(
        "SELECT secret_reference, ciphertext, key_hint, created_at, updated_at "
        "FROM storage_model_routing_secrets WHERE owner_user_id = ? "
        "AND tenant_id = ? AND secret_reference = ?",
        (owner_user_id, tenant_id, secret_reference),
    )
    if row is None:
        return None
    return {
        "secret_reference": str(row["secret_reference"]),
        "ciphertext": str(row["ciphertext"]),
        "key_hint": str(row["key_hint"] or ""),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _model_routing_secret_list(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    rows = session.fetch_all(
        "SELECT secret_reference, key_hint, created_at, updated_at "
        "FROM storage_model_routing_secrets WHERE owner_user_id = ? "
        "AND tenant_id = ? ORDER BY secret_reference",
        (owner_user_id, tenant_id),
    )
    return [
        {
            "secret_reference": str(row["secret_reference"]),
            "key_hint": str(row["key_hint"] or ""),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }
        for row in rows
    ]


def _model_routing_secret_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    secret_reference = _required_text(payload, "secret_reference", 256)
    existing = session.fetch_one(
        "SELECT 1 AS present FROM storage_model_routing_secrets WHERE "
        "owner_user_id = ? AND tenant_id = ? AND secret_reference = ?",
        (owner_user_id, tenant_id, secret_reference),
    )
    if existing is not None:
        session.execute(
            "DELETE FROM storage_model_routing_secrets WHERE owner_user_id = ? "
            "AND tenant_id = ? AND secret_reference = ?",
            (owner_user_id, tenant_id, secret_reference),
        )
    return {"deleted": existing is not None, "secret_reference": secret_reference}


def _model_routing_secret_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    active_raw = payload.get("active_secret_references")
    if not isinstance(active_raw, list) or len(active_raw) > _MAX_SECRETS_PER_OWNER:
        raise StorageError(
            "database_protocol_error", "active_secret_references must be a bounded array")
    active = {
        str(value).strip() for value in active_raw
        if isinstance(value, str) and value.strip()
    }
    before = _number(
        payload, "updated_before", minimum=0, maximum=32_503_680_000)
    rows = session.fetch_all(
        "SELECT secret_reference FROM storage_model_routing_secrets WHERE "
        "owner_user_id = ? AND tenant_id = ? AND updated_at < ? "
        "ORDER BY updated_at, secret_reference LIMIT 256",
        (owner_user_id, tenant_id, before),
    )
    removed: list[str] = []
    for row in rows:
        secret_reference = str(row["secret_reference"])
        if secret_reference in active:
            continue
        session.execute(
            "DELETE FROM storage_model_routing_secrets WHERE owner_user_id = ? "
            "AND tenant_id = ? AND secret_reference = ?",
            (owner_user_id, tenant_id, secret_reference),
        )
        removed.append(secret_reference)
    return {"removed": removed, "count": len(removed), "limit": 256}


__all__ = [name for name in globals() if name.startswith("_model_routing")]

"""Transactional bearer-credential operations for the identity domain.

The Sidecar stores only SHA-256 token hashes.  Public account identifiers and
numeric repository owners remain separate fields; every owner-scoped lookup
requires both the owner and tenant boundary explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
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


_SECRET_HASH = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_COLUMNS = (
    "id, owner_user_id, account_user_id, tenant_id, name, prefix, scopes, "
    "rate_limit_rpm, rate_limit_tpd, created_at, last_used_at, expires_at, "
    "disabled, revoked_at, metadata"
)


def _optional_text(
    payload: Mapping[str, Any], key: str, maximum: int,
) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in credential request")
    return value.strip()


def _owner_boundary(payload: Mapping[str, Any]) -> tuple[int, str]:
    return (
        _integer(payload, "owner_user_id", minimum=1),
        _optional_text(payload, "tenant_id", 256),
    )


def _scopes(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 128
        or any(
            not isinstance(scope, str)
            or not scope
            or len(scope) > 128
            for scope in value
        )
    ):
        raise StorageError(
            "database_protocol_error", "Invalid scopes in credential request")
    return sorted(set(value))


def _credential_document(row: Mapping[str, Any]) -> dict[str, Any]:
    scopes = _load(row["scopes"])
    metadata = _load(row["metadata"])
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        raise StorageError(
            "database_integrity", "Credential scopes are invalid")
    if not isinstance(metadata, dict):
        raise StorageError(
            "database_integrity", "Credential metadata is invalid")
    return {
        "id": row["id"],
        "owner_user_id": int(row["owner_user_id"]),
        "account_user_id": row["account_user_id"] or "",
        "tenant_id": row["tenant_id"] or "",
        "name": row["name"],
        "prefix": row["prefix"],
        "scopes": scopes,
        "rate_limit_rpm": int(row["rate_limit_rpm"] or 0),
        "rate_limit_tpd": int(row["rate_limit_tpd"] or 0),
        "created_at": float(row["created_at"]),
        "last_used_at": (
            None if row["last_used_at"] is None
            else float(row["last_used_at"])
        ),
        "expires_at": (
            None if row["expires_at"] is None else float(row["expires_at"])
        ),
        "disabled": bool(row["disabled"]),
        "revoked_at": (
            None if row["revoked_at"] is None
            else float(row["revoked_at"])
        ),
        "metadata": metadata,
    }


def _credential_account_guard(
    session: Session,
    *,
    account_user_id: str,
    owner_user_id: int,
) -> None:
    if not account_user_id:
        return
    account = session.fetch_one(
        "SELECT owner_user_id, status FROM tenant_users WHERE id = ?",
        (account_user_id,),
    )
    if (
        account is None
        or int(account["owner_user_id"]) != owner_user_id
        or account["status"] != "active"
    ):
        raise StorageError(
            "database_conflict",
            "Credential account is missing, inactive, or owned by another principal",
        )


def _credential_create(session: Session, payload: Mapping[str, Any]) -> Any:
    credential_id = _required_text(payload, "credential_id", 128)
    owner_user_id, tenant_id = _owner_boundary(payload)
    account_user_id = _optional_text(payload, "account_user_id", 256)
    name = _required_text(payload, "name", 80).strip()
    prefix = _required_text(payload, "prefix", 32)
    secret_hash = _required_text(payload, "secret_hash", 64)
    if _SECRET_HASH.fullmatch(secret_hash) is None:
        raise StorageError(
            "database_protocol_error", "Invalid secret_hash in credential request")
    scopes = _scopes(payload.get("scopes"))
    if not scopes:
        raise StorageError(
            "database_protocol_error", "Credential requires at least one scope")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid metadata in credential request")
    expires_at = payload.get("expires_at")
    if expires_at is not None:
        expires_at = _number(
            payload, "expires_at", minimum=0, maximum=32_503_680_000)
    _credential_account_guard(
        session,
        account_user_id=account_user_id,
        owner_user_id=owner_user_id,
    )
    session.lock_key("identity.credential", credential_id)
    session.lock_key("identity.credential.secret", secret_hash)
    if session.fetch_one(
        "SELECT id FROM auth_credentials WHERE id = ? OR secret_hash = ?",
        (credential_id, secret_hash),
    ):
        raise StorageError("database_conflict", "Credential already exists")
    session.execute(
        "INSERT INTO auth_credentials("
        "id, owner_user_id, account_user_id, tenant_id, name, prefix, "
        "secret_hash, scopes, rate_limit_rpm, rate_limit_tpd, created_at, "
        "last_used_at, expires_at, disabled, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)",
        (
            credential_id,
            owner_user_id,
            account_user_id,
            tenant_id,
            name,
            prefix,
            secret_hash,
            _json_text(scopes),
            _integer(payload, "rate_limit_rpm", minimum=0),
            _integer(payload, "rate_limit_tpd", minimum=0),
            _number(payload, "created_at", minimum=0, maximum=32_503_680_000),
            expires_at,
            _json_text(dict(metadata)),
        ),
    )
    row = session.fetch_one(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials WHERE id = ?",
        (credential_id,),
    )
    if row is None:
        raise StorageError(
            "database_integrity", "Credential insert was not visible")
    return _credential_document(row)


def _credential_create_if_owner_empty(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Atomically create the first credential for one owner boundary."""
    owner_user_id, tenant_id = _owner_boundary(payload)
    boundary_key = f"{tenant_id}:{owner_user_id}"
    session.lock_key("identity.credential.bootstrap", boundary_key)
    existing = session.fetch_one(
        "SELECT 1 AS present FROM auth_credentials "
        "WHERE owner_user_id = ? AND tenant_id = ? "
        "AND revoked_at IS NULL LIMIT 1",
        (owner_user_id, tenant_id),
    )
    if existing is not None:
        return None
    return _credential_create(session, payload)


def _credential_list(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    rows = session.fetch_all(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials "
        "WHERE owner_user_id = ? AND tenant_id = ? "
        "AND revoked_at IS NULL "
        "ORDER BY created_at DESC, id DESC",
        (owner_user_id, tenant_id),
    )
    return [_credential_document(row) for row in rows]


def _credential_get(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    row = session.fetch_one(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ? "
        "AND revoked_at IS NULL",
        (
            _required_text(payload, "credential_id", 128),
            owner_user_id,
            tenant_id,
        ),
    )
    return None if row is None else _credential_document(row)


def _credential_authenticate(session: Session, payload: Mapping[str, Any]) -> Any:
    """Legacy atomic validate-and-touch operation kept for wire compatibility."""
    row, now = _validated_credential_row(session, payload)
    if row is None:
        return None
    session.execute(
        "UPDATE auth_credentials SET last_used_at = ? WHERE id = ?",
        (now, row["id"]),
    )
    document = _credential_document(row)
    document["last_used_at"] = now
    return document


def _validated_credential_row(
    session: Session, payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, float]:
    secret_hash = _required_text(payload, "secret_hash", 64)
    if _SECRET_HASH.fullmatch(secret_hash) is None:
        raise StorageError(
            "database_protocol_error", "Invalid secret_hash in credential request")
    now = _number(payload, "now", minimum=0, maximum=32_503_680_000)
    row = session.fetch_one(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials "
        "WHERE secret_hash = ? AND disabled = 0 "
        "AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (secret_hash, now),
    )
    if row is None:
        return None, now
    account_user_id = row["account_user_id"] or ""
    if account_user_id:
        account = session.fetch_one(
            "SELECT owner_user_id, status FROM tenant_users WHERE id = ?",
            (account_user_id,),
        )
        if (
            account is None
            or account["status"] != "active"
            or int(account["owner_user_id"]) != int(row["owner_user_id"])
        ):
            return None, now
    return row, now


def _credential_validate(session: Session, payload: Mapping[str, Any]) -> Any:
    """Validate current authority without entering the SQLite writer lane."""
    row, _now = _validated_credential_row(session, payload)
    return None if row is None else _credential_document(row)


def _credential_touch(session: Session, payload: Mapping[str, Any]) -> Any:
    """Conditionally advance audit metadata inside an explicit owner boundary.

    This operation grants no authority.  Callers must first use
    ``credential.validate``; the repeated active/account checks here merely
    prevent an audit update from racing revocation or account suspension.
    """
    credential_id = _required_text(payload, "credential_id", 128)
    owner_user_id, tenant_id = _owner_boundary(payload)
    used_at = _number(
        payload, "used_at", minimum=0, maximum=32_503_680_000)
    touch_if_before = _number(
        payload, "touch_if_before", minimum=0, maximum=32_503_680_000)
    if touch_if_before > used_at:
        raise StorageError(
            "database_protocol_error",
            "Credential touch boundary cannot exceed used_at",
        )
    row = session.fetch_one(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ? "
        "AND disabled = 0 AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (credential_id, owner_user_id, tenant_id, used_at),
    )
    if row is None:
        return {"touched": False}
    account_user_id = row["account_user_id"] or ""
    if account_user_id:
        account = session.fetch_one(
            "SELECT owner_user_id, status FROM tenant_users WHERE id = ?",
            (account_user_id,),
        )
        if (
            account is None
            or account["status"] != "active"
            or int(account["owner_user_id"]) != owner_user_id
        ):
            return {"touched": False}
    changed = session.execute(
        "UPDATE auth_credentials SET last_used_at = ? "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ? "
        "AND disabled = 0 AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "AND (last_used_at IS NULL OR last_used_at < ?)",
        (
            used_at,
            credential_id,
            owner_user_id,
            tenant_id,
            used_at,
            touch_if_before,
        ),
    )
    return {"touched": changed > 0}


def _credential_identify(session: Session, payload: Mapping[str, Any]) -> Any:
    """Identify a known token hash without granting authority or touching it.

    This read exists for owner-scoped recovery UX at authentication boundaries.
    It deliberately sees disabled and expired tombstones, but never returns the
    stored hash and must never be treated as authentication.
    """
    secret_hash = _required_text(payload, "secret_hash", 64)
    if _SECRET_HASH.fullmatch(secret_hash) is None:
        raise StorageError(
            "database_protocol_error", "Invalid secret_hash in credential request")
    row = session.fetch_one(
        f"SELECT {_CREDENTIAL_COLUMNS} FROM auth_credentials "
        "WHERE secret_hash = ?",
        (secret_hash,),
    )
    return None if row is None else _credential_document(row)


def _credential_update(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    credential_id = _required_text(payload, "credential_id", 128)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise StorageError(
            "database_protocol_error", "Credential updates must be an object")
    allowed = {
        "name",
        "scopes",
        "rate_limit_rpm",
        "rate_limit_tpd",
        "expires_at",
        "disabled",
        "metadata",
    }
    if any(key not in allowed for key in updates):
        raise StorageError(
            "database_protocol_error", "Credential update contains unknown fields")
    assignments: list[str] = []
    parameters: list[Any] = []
    for key, value in updates.items():
        if key == "name":
            normalized = _required_text({"name": value}, "name", 80).strip()
        elif key == "scopes":
            normalized = _json_text(_scopes(value))
        elif key in {"rate_limit_rpm", "rate_limit_tpd"}:
            normalized = _integer({key: value}, key, minimum=0)
        elif key == "expires_at":
            normalized = (
                None
                if value is None
                else _number(
                    {"expires_at": value},
                    "expires_at",
                    minimum=0,
                    maximum=32_503_680_000,
                )
            )
        elif key == "disabled":
            if not isinstance(value, bool):
                raise StorageError(
                    "database_protocol_error",
                    "Credential disabled update must be boolean",
                )
            normalized = int(value)
        else:
            if not isinstance(value, Mapping):
                raise StorageError(
                    "database_protocol_error",
                    "Credential metadata update must be an object",
                )
            normalized = _json_text(dict(value))
        assignments.append(f"{key} = ?")
        parameters.append(normalized)
    parameters.extend((credential_id, owner_user_id, tenant_id))
    session.execute(
        "UPDATE auth_credentials SET " + ", ".join(assignments) +
        " WHERE id = ? AND owner_user_id = ? AND tenant_id = ? "
        "AND revoked_at IS NULL",
        tuple(parameters),
    )
    return _credential_get(session, payload)


def _credential_revoke(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    credential_id = _required_text(payload, "credential_id", 128)
    row = session.fetch_one(
        "SELECT revoked_at, metadata FROM auth_credentials "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        (credential_id, owner_user_id, tenant_id),
    )
    if row is None or row["revoked_at"] is not None:
        return {"revoked": False, "metadata": {}}
    metadata = _load(row["metadata"])
    if not isinstance(metadata, dict):
        raise StorageError(
            "database_integrity", "Credential metadata is invalid")
    session.execute(
        "UPDATE auth_credentials SET disabled = 1, revoked_at = ? "
        "WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        (
            _number(payload, "revoked_at", minimum=0,
                    maximum=32_503_680_000),
            credential_id,
            owner_user_id,
            tenant_id,
        ),
    )
    return {"revoked": True, "metadata": metadata}


def _credential_exists(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_user_id, tenant_id = _owner_boundary(payload)
    row = session.fetch_one(
        "SELECT 1 AS present FROM auth_credentials "
        "WHERE owner_user_id = ? AND tenant_id = ? "
        "AND revoked_at IS NULL LIMIT 1",
        (owner_user_id, tenant_id),
    )
    return {"exists": row is not None}


__all__ = [
    "_CREDENTIAL_COLUMNS",
    "_credential_authenticate",
    "_credential_create",
    "_credential_create_if_owner_empty",
    "_credential_document",
    "_credential_exists",
    "_credential_get",
    "_credential_identify",
    "_credential_list",
    "_credential_revoke",
    "_credential_touch",
    "_credential_update",
    "_credential_validate",
]

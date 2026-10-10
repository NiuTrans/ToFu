"""Tenant user account operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._runs import (
    _json_text,
    _optional_text,
)

_TENANT_USER_ROLES = {"user", "admin"}


_TENANT_USER_STATUSES = {"active", "suspended", "deleted"}


_TENANT_USER_COLUMNS = (
    "id, owner_user_id, email, display_name, role, status, created_at, "
    "last_login_at, email_verified, metadata"
)


def _tenant_user_document(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _load(row["metadata"])
    if not isinstance(metadata, dict):
        raise StorageError("database_integrity", "Tenant user metadata is invalid")
    return {
        "id": row["id"],
        "owner_user_id": int(row["owner_user_id"]),
        "email": row["email"],
        "display_name": row["display_name"] or "",
        "role": row["role"],
        "status": row["status"],
        "created_at": int(row["created_at"]),
        "last_login_at": int(row["last_login_at"] or 0),
        "email_verified": bool(row["email_verified"]),
        "metadata": metadata,
    }


def _allocate_owner_user_id(session: Session) -> int:
    """Allocate one durable repository owner; owner 1 is personal mode."""
    sequence_name = "owner_user_id"
    session.lock_key("identity.owner.allocate", sequence_name)
    sequence = session.fetch_one(
        "SELECT next_value FROM storage_identity_sequences "
        "WHERE sequence_name = ?",
        (sequence_name,),
    )
    if sequence is None:
        current = session.fetch_one(
            "SELECT COALESCE(MAX(owner_user_id), 1) AS maximum "
            "FROM tenant_users"
        )
        owner_user_id = max(2, int(current["maximum"] or 1) + 1)
        session.execute(
            "INSERT INTO storage_identity_sequences(sequence_name, next_value) "
            "VALUES (?, ?)",
            (sequence_name, owner_user_id + 1),
        )
        return owner_user_id
    owner_user_id = int(sequence["next_value"])
    if owner_user_id < 2:
        raise StorageError(
            "database_integrity", "Owner identity sequence is invalid")
    session.execute(
        "UPDATE storage_identity_sequences SET next_value = ? "
        "WHERE sequence_name = ?",
        (owner_user_id + 1, sequence_name),
    )
    return owner_user_id


def _tenant_user_role(payload: Mapping[str, Any]) -> str:
    role = _required_text(payload, "role", 32)
    if role not in _TENANT_USER_ROLES:
        raise StorageError("database_protocol_error", "Invalid tenant user role")
    return role


def _tenant_user_status(payload: Mapping[str, Any], *, optional=False) -> str:
    status = payload.get("status", "")
    if optional and status == "":
        return ""
    if not isinstance(status, str) or status not in _TENANT_USER_STATUSES:
        raise StorageError("database_protocol_error", "Invalid tenant user status")
    return status


def _tenant_user_create(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, "user_id", 256)
    email = _required_text(payload, "email", 320).strip().lower()
    role = _tenant_user_role(payload)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise StorageError("database_protocol_error", "Invalid tenant user metadata")
    session.lock_key("tenant.user.email", email)
    if session.fetch_one("SELECT id FROM tenant_users WHERE email = ?", (email,)):
        raise StorageError("database_conflict", "Tenant user email already exists")
    owner_user_id = _allocate_owner_user_id(session)
    session.execute(
        "INSERT INTO tenant_users("
        "id, owner_user_id, email, password_hash, display_name, role, status, "
        "created_at, last_login_at, email_verified, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            owner_user_id,
            email,
            _optional_text(payload, "password_hash", maximum=512, scope="tenant user"),
            _optional_text(payload, "display_name", maximum=256, scope="tenant user"),
            role,
            "active",
            _integer(payload, "created_at", minimum=0),
            0,
            0,
            _json_text(dict(metadata)),
        ),
    )
    row = session.fetch_one(
        f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        raise StorageError("database_integrity", "Tenant user insert was not visible")
    return _tenant_user_document(row)


def _tenant_user_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = payload.get("user_id", "")
    email = payload.get("email", "")
    if bool(user_id) == bool(email):
        raise StorageError(
            "database_protocol_error", "Exactly one tenant user selector is required"
        )
    if user_id:
        value = _required_text(payload, "user_id", 256)
        predicate = "id = ?"
    else:
        value = _required_text(payload, "email", 320).strip().lower()
        predicate = "email = ?"
    row = session.fetch_one(
        f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE {predicate}",
        (value,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_list(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=1000)
    offset = _integer(payload, "offset", default=0, minimum=0, maximum=10_000_000)
    status = _tenant_user_status(payload, optional=True)
    if status:
        rows = session.fetch_all(
            f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users "
            "WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        rows = session.fetch_all(
            f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [_tenant_user_document(row) for row in rows]


def _tenant_user_set_status(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, "user_id", 256)
    count = session.execute(
        "UPDATE tenant_users SET status = ? WHERE id = ?",
        (_tenant_user_status(payload), user_id),
    )
    if not count:
        return None
    row = session.fetch_one(
        f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?",
        (user_id,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_set_role(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _required_text(payload, "user_id", 256)
    count = session.execute(
        "UPDATE tenant_users SET role = ? WHERE id = ?",
        (_tenant_user_role(payload), user_id),
    )
    if not count:
        return None
    row = session.fetch_one(
        f"SELECT {_TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?",
        (user_id,),
    )
    return None if row is None else _tenant_user_document(row)


def _tenant_user_authentication(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    email = _required_text(payload, "email", 320).strip().lower()
    row = session.fetch_one(
        f"SELECT {_TENANT_USER_COLUMNS}, password_hash FROM tenant_users "
        "WHERE email = ?",
        (email,),
    )
    if row is None:
        return None
    return {
        "user": _tenant_user_document(row),
        "password_hash": row["password_hash"] or "",
    }


def _tenant_user_record_login(session: Session, payload: Mapping[str, Any]) -> Any:
    count = session.execute(
        "UPDATE tenant_users SET last_login_at = ? WHERE id = ?",
        (
            _integer(payload, "last_login_at", minimum=0),
            _required_text(payload, "user_id", 256),
        ),
    )
    return {"updated": bool(count)}

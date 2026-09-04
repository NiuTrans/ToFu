"""Owner-scoped durable desktop-device preferences.

The live device registry remains ephemeral bridge transport.  This slice owns
only the user's durable egress-agent selection, with an explicit row for the
unset state so legacy-file import has a persistent completion marker.
"""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _integer


_MAX_AGENT_ID_CHARS = 128


def _owner_user_id(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "owner_user_id", minimum=1)


def _agent_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("agent_id")
    if not isinstance(value, str) or len(value) > _MAX_AGENT_ID_CHARS:
        raise StorageError(
            "database_protocol_error", "Invalid agent_id in storage request"
        )
    return value


def _desktop_egress_agent_get(
    session: Session, payload: Mapping[str, Any],
) -> dict[str, Any]:
    row = session.fetch_one(
        "SELECT agent_id,updated_at_ms FROM storage_desktop_egress_preferences "
        "WHERE owner_user_id=?",
        (_owner_user_id(payload),),
    )
    if row is None:
        return {"present": False, "agent_id": "", "updated_at_ms": 0}
    return {
        "present": True,
        "agent_id": str(row["agent_id"] or ""),
        "updated_at_ms": int(row["updated_at_ms"] or 0),
    }


def _write_preference(
    session: Session, *, owner_user_id: int, agent_id: str, initialize: bool,
) -> dict[str, Any]:
    session.lock_key("desktop.egress_agent", str(owner_user_id))
    now_ms = int(time.time() * 1000)
    if initialize:
        session.execute(
            "INSERT INTO storage_desktop_egress_preferences("
            "owner_user_id,agent_id,updated_at_ms) VALUES (?,?,?) "
            "ON CONFLICT(owner_user_id) DO NOTHING",
            (owner_user_id, agent_id, now_ms),
        )
    else:
        session.execute(
            "INSERT INTO storage_desktop_egress_preferences("
            "owner_user_id,agent_id,updated_at_ms) VALUES (?,?,?) "
            "ON CONFLICT(owner_user_id) DO UPDATE SET "
            "agent_id=excluded.agent_id,updated_at_ms=excluded.updated_at_ms",
            (owner_user_id, agent_id, now_ms),
        )
    # Initialization deliberately returns the winning row.  A concurrent
    # explicit POST must never be overwritten by a late legacy-file import.
    return _desktop_egress_agent_get(
        session, {"owner_user_id": owner_user_id})


def _desktop_egress_agent_initialize(
    session: Session, payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _write_preference(
        session,
        owner_user_id=_owner_user_id(payload),
        agent_id=_agent_id(payload),
        initialize=True,
    )


def _desktop_egress_agent_set(
    session: Session, payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _write_preference(
        session,
        owner_user_id=_owner_user_id(payload),
        agent_id=_agent_id(payload),
        initialize=False,
    )


__all__ = [name for name in globals() if name.startswith("_desktop_egress_")]

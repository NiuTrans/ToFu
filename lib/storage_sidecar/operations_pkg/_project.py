"""Owner-scoped project aggregate operations.

Project records use the generic durable record table internally, while this
module owns their semantic key construction. Callers provide an explicit
owner and normalized project path; they never construct storage namespaces or
composite keys themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _integer, _required_text
from lib.storage_sidecar.operations_pkg._records import (
    _record_delete,
    _record_get,
    _record_list,
    _record_put,
)


_CHARTER_NAMESPACE = "project_charter"
_RECENT_PROJECT_NAMESPACE = "recent_projects"


def _project_record_key(payload: Mapping[str, Any]) -> str:
    user_id = _integer(payload, "user_id", minimum=1)
    project_path = _required_text(payload, "project_path", 4096)
    return f"{user_id}:{project_path}"


def _project_charter_get(session: Session, payload: Mapping[str, Any]) -> Any:
    return _record_get(
        session,
        {"namespace": _CHARTER_NAMESPACE, "key": _project_record_key(payload)},
    )


def _project_charter_put(session: Session, payload: Mapping[str, Any]) -> Any:
    record_payload = {
        "namespace": _CHARTER_NAMESPACE,
        "key": _project_record_key(payload),
        "value": payload.get("value"),
    }
    if "expected_version" in payload:
        record_payload["expected_version"] = payload.get("expected_version")
    return _record_put(session, record_payload)


def _project_charter_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    record_payload = {
        "namespace": _CHARTER_NAMESPACE,
        "key": _project_record_key(payload),
    }
    if "expected_version" in payload:
        record_payload["expected_version"] = payload.get("expected_version")
    return _record_delete(session, record_payload)


def _recent_project_prefix(payload: Mapping[str, Any]) -> str:
    return f'{_integer(payload, "user_id", minimum=1)}:'


def _recent_project_key(payload: Mapping[str, Any]) -> str:
    return _recent_project_prefix(payload) + _required_text(
        payload, "project_path", 4096)


def _project_recent_list(session: Session, payload: Mapping[str, Any]) -> Any:
    rows = _record_list(
        session,
        {
            "namespace": _RECENT_PROJECT_NAMESPACE,
            "prefix": _recent_project_prefix(payload),
            "limit": 1000,
        },
    )
    values = [row.get("value") for row in rows]
    projects = [dict(value) for value in values if isinstance(value, Mapping)]
    projects.sort(key=lambda item: int(item.get("last_used") or 0), reverse=True)
    return projects


def _project_recent_touch(session: Session, payload: Mapping[str, Any]) -> Any:
    key = _recent_project_key(payload)
    session.lock_key(_RECENT_PROJECT_NAMESPACE, key)
    current = _record_get(
        session, {"namespace": _RECENT_PROJECT_NAMESPACE, "key": key})
    value = dict((current or {}).get("value") or {})
    value.update(
        {
            "path": _required_text(payload, "project_path", 4096),
            "count": int(value.get("count") or 0) + 1,
            "last_used": _integer(payload, "last_used", minimum=0),
        }
    )
    _record_put(
        session,
        {"namespace": _RECENT_PROJECT_NAMESPACE, "key": key, "value": value},
    )
    return value


def _project_recent_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_prefix = _recent_project_prefix(payload)
    session.lock_key(_RECENT_PROJECT_NAMESPACE, owner_prefix)
    deleted = session.execute(
        "DELETE FROM storage_records WHERE namespace = ? AND record_key LIKE ?",
        (_RECENT_PROJECT_NAMESPACE, owner_prefix + "%"),
    )
    return {"deleted": int(deleted or 0)}

"""Owner-scoped project aggregate operations.

Project records use the generic durable record table internally, while this
module owns their semantic key construction. Callers provide an explicit
owner and normalized project path; they never construct storage namespaces or
composite keys themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.project_recent_contract import (
    PROJECT_RELINK_CONVERSATION_LIMIT,
    RECENT_PROJECT_PATH_MAX_CHARS,
    RECENT_PROJECT_TOUCH_BATCH_LIMIT,
)
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._records import (
    _record_delete,
    _record_get,
    _record_list,
    _record_put,
)


_RECENT_PROJECT_NAMESPACE = "recent_projects"
# Keep the rename transaction bounded by the same complete-history ceiling as
# the metadata-only conversation catalog.  Above this explicit ceiling the
# caller must migrate in an offline maintenance window instead of monopolizing
# the personal-computer writer lane.
_PROJECT_RELINK_CONVERSATION_LIMIT = PROJECT_RELINK_CONVERSATION_LIMIT


def _replace_project_path_references(
    settings: Mapping[str, Any], old_path: str, new_path: str
) -> dict[str, Any] | None:
    """Return rewritten conversation settings, or ``None`` when unchanged.

    Only exact path values in the declared project settings plane move.  A
    substring in an unrelated setting must never be treated as project
    identity.  Lists are de-duplicated after replacement because an existing
    ``new_path`` and the renamed ``old_path`` now identify the same root.
    """
    rewritten = dict(settings)
    changed = False
    if rewritten.get("projectPath") == old_path:
        rewritten["projectPath"] = new_path
        changed = True
    for key in ("projectPaths", "readOnlyPaths"):
        value = rewritten.get(key)
        if not isinstance(value, list) or old_path not in value:
            continue
        deduplicated: list[Any] = []
        for item in value:
            candidate = new_path if item == old_path else item
            if candidate not in deduplicated:
                deduplicated.append(candidate)
        rewritten[key] = deduplicated
        changed = True
    return rewritten if changed else None


def _conversation_project_relink(
    session: Session, user_id: int, old_path: str, new_path: str
) -> tuple[int, int]:
    """Move bounded active and recoverable conversation pins atomically."""
    # Match the canonical JSON string value anywhere in the document, then
    # decode and require an exact value in the project settings plane.  CAST
    # keeps the query backend-neutral for SQLite JSONDOC and PostgreSQL JSONB.
    json_literal = _dump(old_path).decode("utf-8")
    escaped_literal = (
        json_literal.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    )
    candidates: list[tuple[str, str, str, Any]] = []
    for table, id_column in (
        ("storage_conversations", "id"),
        ("storage_conversation_trash", "conversation_id"),
    ):
        remaining = _PROJECT_RELINK_CONVERSATION_LIMIT + 1 - len(candidates)
        if remaining <= 0:
            break
        rows = session.fetch_all(
            f"SELECT {id_column} AS conversation_id,settings_json "
            f"FROM {table} "
            "WHERE user_id=? AND CAST(settings_json AS TEXT) LIKE ? ESCAPE '!' "
            f"ORDER BY {id_column} LIMIT ?",
            (user_id, f"%{escaped_literal}%", remaining),
        )
        candidates.extend(
            (
                table,
                id_column,
                str(row["conversation_id"]),
                row["settings_json"],
            )
            for row in rows
        )
    if len(candidates) > _PROJECT_RELINK_CONVERSATION_LIMIT:
        raise StorageError(
            "database_conflict",
            "Too many conversations reference the old project path",
        )

    updates: dict[str, list[tuple[Any, ...]]] = {
        "storage_conversations": [],
        "storage_conversation_trash": [],
    }
    for table, id_column, conv_id, scanned_settings in candidates:
        stored_settings = scanned_settings
        if session.backend == "postgres":
            # PostgreSQL has concurrent writers. Re-read after taking the same
            # semantic lock as conversation settings/restore/purge so a stale
            # candidate snapshot cannot overwrite a newer mutation.
            session.lock_key("conversation", f"{user_id}:{conv_id}")
            row = session.fetch_one(
                f"SELECT settings_json FROM {table} "
                f"WHERE {id_column}=? AND user_id=?",
                (conv_id, user_id),
            )
            if row is None:
                continue
            stored_settings = row["settings_json"]
        # SQLite's one physical writer already serializes the whole command;
        # using the settings returned by the bounded candidate scan removes
        # thousands of redundant round trips on personal-computer storage.
        settings = _load(stored_settings) or {}
        if not isinstance(settings, Mapping):
            raise StorageError(
                "database_integrity", "Conversation settings are malformed"
            )
        rewritten = _replace_project_path_references(
            settings, old_path, new_path
        )
        if rewritten is None:
            continue
        updates[table].append((_dump(rewritten), conv_id, user_id))

    # Release the scanned documents before the writer duplicates changed
    # settings into SQLite/Psycopg's bounded batch buffers.
    candidates.clear()

    moved: dict[str, int] = {}
    for table, id_column in (
        ("storage_conversations", "id"),
        ("storage_conversation_trash", "conversation_id"),
    ):
        parameters = updates[table]
        if not parameters:
            moved[table] = 0
            continue
        moved[table] = session.execute_many_exact(
            f"UPDATE {table} SET settings_json=? "
            f"WHERE {id_column}=? AND user_id=?",
            parameters,
        )
    return moved["storage_conversations"], moved["storage_conversation_trash"]


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


def _project_recent_touch_many(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    raw_paths = payload.get("project_paths")
    if (
        not isinstance(raw_paths, list)
        or not raw_paths
        or len(raw_paths) > RECENT_PROJECT_TOUCH_BATCH_LIMIT
    ):
        raise StorageError(
            "database_protocol_error",
            "Invalid project_paths in storage request",
        )
    paths: list[str] = []
    for candidate in raw_paths:
        path = _required_text(
            {"project_path": candidate},
            "project_path",
            RECENT_PROJECT_PATH_MAX_CHARS,
        )
        if path not in paths:
            paths.append(path)
    touch_payload = {
        "user_id": _integer(payload, "user_id", minimum=1),
        "last_used": _integer(payload, "last_used", minimum=0),
    }
    # Stable lock order prevents reversed multi-root requests from deadlocking
    # on PostgreSQL. The response stays receipt-small even for 4 KiB paths.
    for path in sorted(paths):
        _project_recent_touch(
            session,
            {**touch_payload, "project_path": path},
        )
    return {"touched": len(paths)}


def _project_recent_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    owner_prefix = _recent_project_prefix(payload)
    session.lock_key(_RECENT_PROJECT_NAMESPACE, owner_prefix)
    deleted = session.execute(
        "DELETE FROM storage_records WHERE namespace = ? AND record_key LIKE ?",
        (_RECENT_PROJECT_NAMESPACE, owner_prefix + "%"),
    )
    return {"deleted": int(deleted or 0)}


def _project_relink(session: Session, payload: Mapping[str, Any]) -> Any:
    """Re-key every owner-scoped aggregate from ``old_path`` to ``new_path``.

    A directory rename/move does not change project identity: the recent
    entry, conversation project pins, and the complete Project Brain
    event/projection authority follow.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    old_path = _required_text(payload, "old_path", 4096)
    new_path = _required_text(payload, "new_path", 4096)
    if old_path == new_path:
        raise StorageError(
            "database_protocol_error", "old_path and new_path must differ")
    old_key = f"{user_id}:{old_path}"
    new_key = f"{user_id}:{new_path}"
    # Sorted lock order inside each namespace group; no other operation
    # locks across these groups, so a deadlock cycle cannot form.
    for namespace, key in sorted({
        (_RECENT_PROJECT_NAMESPACE, old_key),
        (_RECENT_PROJECT_NAMESPACE, new_key),
    }):
        session.lock_key(namespace, key)

    current = _record_get(
        session, {"namespace": _RECENT_PROJECT_NAMESPACE, "key": old_key})
    value = (current or {}).get("value")
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_not_found", "Old path is not in recent projects")
    new_value = {**dict(value), "path": new_path}
    existing = _record_get(
        session, {"namespace": _RECENT_PROJECT_NAMESPACE, "key": new_key})
    existing_value = (existing or {}).get("value")
    if isinstance(existing_value, Mapping):
        new_value = {
            **new_value,
            "count": int(new_value.get("count") or 0)
            + int(existing_value.get("count") or 0),
            "last_used": max(
                int(new_value.get("last_used") or 0),
                int(existing_value.get("last_used") or 0),
            ),
        }
    _record_put(
        session,
        {"namespace": _RECENT_PROJECT_NAMESPACE, "key": new_key,
         "value": new_value},
    )
    _record_delete(
        session, {"namespace": _RECENT_PROJECT_NAMESPACE, "key": old_key})

    from lib.storage_sidecar.operations_pkg._project_brain import (
        _project_brain_relink_scope,
    )
    brain_moved = _project_brain_relink_scope(
        session, user_id, old_path, new_path)
    conversations_moved, trashed_conversations_moved = _conversation_project_relink(
        session, user_id, old_path, new_path
    )
    return {
        "project": new_value,
        "projectBrainMoved": bool(brain_moved),
        "conversationsMoved": conversations_moved,
        "trashedConversationsMoved": trashed_conversations_moved,
    }

"""Conversation document and transcript operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any
import uuid


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._turns import (
    _mark_conversation_search_projection_dirty,
    _turn_public,
)

_CONVERSATION_METADATA = frozenset(
    {
        "title",
        "created_at",
        "updated_at",
        "settings",
        "search_text",
    }
)

_LIVE_TURN_STATUSES = frozenset({"pending", "running"})
_CLONE_RUNTIME_KEYS = frozenset({
    "_activeAttemptId",
    "_attemptId",
    "_authoritativeActiveTaskIds",
    "_commandPending",
    "_needsStart",
    "_streamCursor",
    "_streaming",
    "activeTaskId",
    "approvalRequired",
    "isStreaming",
})
_CLONE_TASK_ID_KEYS = frozenset({
    "_proactiveTaskId",
    "_taskId",
    "_translateTaskId",
    "taskId",
})


def _settings_without_live_runtime(raw: Any) -> dict[str, Any]:
    settings = _load(raw) or {}
    if not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation settings are malformed")
    result = dict(settings)
    result.pop("activeTaskId", None)
    result.pop("_activeAttemptId", None)
    return result


def _clone_projection_value(
    value: Any,
    *,
    turn_ids: Mapping[str, str],
    archive_ids: Mapping[str, str],
    task_ids: dict[str, str],
) -> Any:
    """Copy presentation data while severing every executable identity.

    A duplicated conversation is independent history, not another handle to
    the source task/attempt/project mutations.  Stable content fields remain;
    runtime latches disappear; turn/archive references are remapped; historical
    task identifiers become inert clone-local identifiers.
    """
    if isinstance(value, Mapping):
        cloned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _CLONE_RUNTIME_KEYS:
                continue
            if key in {"_turnId", "turnId"} and isinstance(item, str):
                replacement = turn_ids.get(item)
                if replacement:
                    cloned[key] = replacement
                continue
            if key in {"archiveId", "_compactionArchiveId"} \
                    and isinstance(item, str):
                cloned[key] = archive_ids.get(item, item)
                continue
            if key in _CLONE_TASK_ID_KEYS and isinstance(item, str) and item:
                cloned[key] = task_ids.setdefault(
                    item, f"clone-task-{uuid.uuid4().hex}"
                )
                continue
            cloned[key] = _clone_projection_value(
                item,
                turn_ids=turn_ids,
                archive_ids=archive_ids,
                task_ids=task_ids,
            )
        return cloned
    if isinstance(value, list):
        return [
            _clone_projection_value(
                item,
                turn_ids=turn_ids,
                archive_ids=archive_ids,
                task_ids=task_ids,
            )
            for item in value
        ]
    return value


def _conversation_identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    conv_id = _required_text(payload, "conv_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    return conv_id, user_id


def _conversation_document(
    row: Mapping[str, Any],
    *,
    include_messages: bool = True,
    messages: list[dict[str, Any]] | None = None,
    settings_keys: list[str] | None = None,
) -> dict[str, Any]:
    messages = list(messages or []) if include_messages else []
    settings = _load(row["settings_json"]) or {}
    if not isinstance(messages, list) or not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation document is malformed")
    if settings_keys is not None:
        settings = {key: settings[key] for key in settings_keys if key in settings}
    return {
        "metadata": {
            "id": str(row["id"]),
            "user_id": int(row["user_id"]),
            "title": str(row["title"] or ""),
            "created_at": int(row["created_at_ms"]),
            "updated_at": int(row["updated_at_ms"]),
            "settings": settings,
            "msg_count": int(row["msg_count"]),
            # Absent on metadata-only projections — the search corpus is
            # as heavy as the archive itself (hundreds of MiB fleet-wide).
            "search_text": str(row.get("search_text") or ""),
            "rev": int(row["rev"]),
        },
        "messages": messages if include_messages else [],
        "source": "sidecar",
    }


def _turn_actor_to_legacy_role(actor: str) -> str:
    """Mirror of the client's ``messageRole`` (turn-projection.ts)."""
    return "user" if actor in ("human", "critic", "virtual_user") else "assistant"


def _with_legacy_compaction_fields(projection: dict[str, Any]) -> dict[str, Any]:
    """Project canonical compaction metadata onto the read-only v1 overlay."""
    compaction = projection.get("compaction")
    if not isinstance(compaction, Mapping) \
            or compaction.get("blockId") != "compaction":
        return projection
    result = dict(projection)
    result["_isCompactionSummary"] = True
    archive_id = compaction.get("archiveId")
    if archive_id not in (None, ""):
        result["_compactionArchiveId"] = str(archive_id)
    estimated = compaction.get("estimatedPromptTokens")
    if isinstance(estimated, (int, float)) and not isinstance(estimated, bool):
        result["_estimatedPromptTokens"] = max(0, int(estimated))
    folded = compaction.get("foldedToolRounds")
    if isinstance(folded, (int, float)) and not isinstance(folded, bool):
        result["_foldedToolRounds"] = max(0, int(folded))
    marker_names = {
        "archiveId": "archiveId",
        "conversationId": "convId",
        "trigger": "trigger",
        "timestamp": "ts",
        "tokensBefore": "tokensBefore",
        "tokensAfter": "tokensAfter",
        "messagesBefore": "msgsBefore",
        "messagesAfter": "msgsAfter",
        "reductionPercent": "reductionPct",
        "foldedToolRounds": "foldedToolRounds",
    }
    marker = {
        legacy_name: compaction[canonical_name]
        for canonical_name, legacy_name in marker_names.items()
        if compaction.get(canonical_name) not in (None, "")
    }
    if marker:
        result["_compactions"] = [marker]
    return result


def _turn_to_legacy_message(turn: Mapping[str, Any]) -> dict[str, Any]:
    """One canonical turn row → one legacy v1 message document.

    Mirrors the client's own ``turnToLegacyMessage``
    (frontend/src/core/turn-projection.ts) field-for-field, so a v1 body read
    and a v2 hydration agree on the same conversation byte-for-byte — the v1
    view is a DERIVED VIEW of the turn authority, never a second copy.
    ``_commandPending`` is client-runtime-only and stays None server-side.
    """
    projection = dict(turn.get("projection") or {})
    projection.pop("role", None)
    projection = _with_legacy_compaction_fields(projection)
    created_at = int(turn.get("createdAt") or 0)
    return {
        **projection,
        "role": _turn_actor_to_legacy_role(str(turn.get("actor") or "")),
        "_turnId": turn.get("turnId"),
        "_attemptId": turn.get("currentAttemptId"),
        "_turnActor": turn.get("actor"),
        "_turnKind": turn.get("kind"),
        "_turnLaneId": turn.get("laneId") or "main",
        "_turnStatus": turn.get("status"),
        "_turnSettlement": turn.get("settlement") or {},
        "_commandPending": None,
        "_projectionRevision": int(turn.get("projectionRevision") or 0),
        "timestamp": projection.get("timestamp") or created_at,
    }


def _derive_turn_messages(session: Session, conv_id: str, user_id: Any) -> list:
    """Project canonical main-lane turns into the HTTP message view."""
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main' "
        "ORDER BY ordinal",
        (conv_id, user_id),
    )
    return [_turn_to_legacy_message(_turn_public(row)) for row in rows]


def _conversation_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _conversation_identity(payload)
    row = session.fetch_one(
        "SELECT id, user_id, title, created_at_ms, "
        "updated_at_ms, settings_json, msg_count, search_text, rev "
        "FROM storage_conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    if row is None:
        return None
    _backfill_turn_message_counts(session, [row])
    include_messages = bool(payload.get("derive_messages", True))
    messages = (
        _derive_turn_messages(session, conv_id, user_id)
        if include_messages else []
    )
    return _conversation_document(
        row, include_messages=include_messages, messages=messages)


def _backfill_turn_message_counts(session: Session, rows: list[dict]) -> None:
    """Project exact main-lane counts from the sole transcript authority."""
    if not rows:
        return
    identities = [
        (str(row["id"]), int(row["user_id"])) for row in rows
    ]
    where = " OR ".join(
        "(conversation_id=? AND user_id=?)" for _ in identities)
    params = tuple(value for identity in identities for value in identity)
    counts = session.fetch_all(
        "SELECT conversation_id AS cid, user_id, count(*) AS n "
        "FROM storage_conversation_turns "
        "WHERE (" + where + ") AND lane_id = 'main' "
        "GROUP BY conversation_id,user_id",
        params,
    )
    by_identity = {
        (str(item["cid"]), int(item["user_id"])): int(item["n"])
        for item in counts
    }
    for row in rows:
        row["msg_count"] = by_identity.get(
            (str(row["id"]), int(row["user_id"])), 0)


def _conversation_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    project_path = (
        _required_text(payload, "project_path", 4096)
        if "project_path" in payload
        else None
    )
    ids = payload.get("ids")
    if ids is not None:
        if not isinstance(ids, list):
            raise StorageError(
                "database_protocol_error", "Invalid conversation id filter"
            )
        ids = list(dict.fromkeys(str(item) for item in ids if item))
        if not ids:
            return []
    updated_at_gte = payload.get("updated_at_gte")
    updated_at_gt = payload.get("updated_at_gt")
    created_at_lt = payload.get("created_at_lt")
    for value in (updated_at_gte, updated_at_gt, created_at_lt):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise StorageError(
                "database_protocol_error", "Invalid conversation time filter"
            )
    order_by = payload.get("order_by", "updated_at_desc")
    order_sql = {
        "updated_at_desc": "updated_at_ms DESC, id DESC",
        "id_asc": "id ASC",
    }.get(order_by)
    if order_sql is None:
        raise StorageError("database_protocol_error", "Invalid conversation ordering")
    include_messages = payload.get("include_messages", True)
    if not isinstance(include_messages, bool):
        raise StorageError(
            "database_protocol_error", "Invalid conversation message projection"
        )
    settings_keys = payload.get("settings_keys")
    if settings_keys is not None:
        if not isinstance(settings_keys, list) or not all(
            isinstance(key, str) and key for key in settings_keys
        ):
            raise StorageError(
                "database_protocol_error", "Invalid conversation settings projection"
            )
        settings_keys = list(dict.fromkeys(settings_keys))[:64]
    metadata_only = not include_messages
    default_limit = 1000
    for value in (
        updated_at_gte,
        updated_at_gt,
        created_at_lt,
        ids,
        project_path,
    ):
        if value is not None:
            metadata_only = False
            break
    if metadata_only:
        # The sidebar poll is metadata-only (every consumer reads
        # ``document['metadata']``). Without a bound it recovers the complete
        # personal history after a browser cache loss, and the projection
        # below skips the GiB-scale transcript archive entirely.
        default_limit = 10000
    limit = _integer(payload, "limit", default=default_limit, minimum=0, maximum=10000)
    where: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if project_path is not None:
        project_path_expression = (
            "settings_json ->> 'projectPath'"
            if session.backend == "postgres"
            else "json_extract(settings_json, '$.projectPath')"
        )
        where.append(f"{project_path_expression} = ?")
        params.append(project_path)
    if ids is not None:
        where.append("id IN (%s)" % ",".join("?" for _ in ids))
        params.extend(ids)
    if updated_at_gte is not None:
        where.append("updated_at_ms >= ?")
        params.append(updated_at_gte)
    if updated_at_gt is not None:
        where.append("updated_at_ms > ?")
        params.append(updated_at_gt)
    if created_at_lt is not None:
        where.append("created_at_ms < ?")
        params.append(created_at_lt)
    if not where:
        where.append("1 = 1")
    # Metadata-only listings never touch turn projections or search fragments.
    projection = (
        "id, user_id, title, created_at_ms, "
        "updated_at_ms, settings_json, msg_count, search_text, rev "
        if include_messages
        else "id, user_id, title, created_at_ms, "
        "updated_at_ms, settings_json, msg_count, rev "
    )
    rows = session.fetch_all(
        "SELECT "
        + projection
        + "FROM storage_conversations WHERE "
        + " AND ".join(where)
        + f" ORDER BY {order_sql} LIMIT ?",
        tuple(params + [limit]),
    )
    _backfill_turn_message_counts(session, rows)
    if include_messages:
        _derive_turn_messages_bulk(session, rows)
    return [
        _conversation_document(
            row,
            include_messages=include_messages,
            messages=row.get("_projected_messages"),
            settings_keys=settings_keys,
        )
        for row in rows
    ]


def _derive_turn_messages_bulk(session: Session, rows: list[dict]) -> None:
    """Project every listed transcript from turns in one owner-aware query."""
    if not rows:
        return
    identities = [
        (str(row["id"]), int(row["user_id"])) for row in rows
    ]
    where = " OR ".join(
        "(conversation_id=? AND user_id=?)" for _ in identities)
    params = tuple(value for identity in identities for value in identity)
    turn_rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE (" + where + ") AND lane_id = 'main' "
        "ORDER BY user_id,conversation_id,ordinal",
        params,
    )
    by_identity: dict[tuple[str, int], list[dict]] = {}
    for turn_row in turn_rows:
        identity = (
            str(turn_row["conversation_id"]), int(turn_row["user_id"])
        )
        by_identity.setdefault(identity, []).append(turn_row)
    for row in rows:
        identity = (str(row["id"]), int(row["user_id"]))
        row["_projected_messages"] = [
            _turn_to_legacy_message(_turn_public(turn_row))
            for turn_row in by_identity.get(identity, [])
        ]


def _conversation_count(session: Session, payload: Mapping[str, Any]) -> Any:
    """Authoritative conversation count for a user, without touching blobs.

    The sidebar needs the REAL total (not the bounded window length) so the
    browser can distinguish "list is a recent page" from "list is complete"
    before it prunes locally-cached conversations. A bare ``COUNT(*)`` over
    the indexed ``user_id`` keeps the poll cheap.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    where = "WHERE user_id = ?"
    params = (user_id,)
    row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversations " + where, params
    )
    return {"count": int(row["c"]) if row else 0}


def _conversation_search_op(session: Session, payload: Mapping[str, Any]) -> Any:
    """Search canonical settled main-lane turn fragments."""
    query_raw = payload.get("query")
    if not isinstance(query_raw, str):
        raise StorageError(
            "database_protocol_error", "Invalid conversation search query"
        )
    query = query_raw.strip().lower()
    user_id = _integer(payload, "user_id", minimum=1)
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=200)
    radius = _integer(payload, "snippet_radius", default=40, minimum=0, maximum=400)
    if len(query) < 2:
        return []
    turn_head = "lower(substr(s.search_text, 1, 10000))"

    def _like(term: str) -> str:
        return (
            "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )

    def _match_clause(terms: list[str]) -> tuple[str, list[Any]]:
        likes = [_like(term) for term in terms]
        turn_matches = " AND ".join(
            "EXISTS (SELECT 1 FROM storage_search_turns AS s "
            "WHERE s.conversation_id=c.id AND s.user_id=c.user_id "
            "AND s.lane_id='main' AND "
            f"{turn_head} LIKE ? ESCAPE '\\')"
            for _ in terms
        )
        return turn_matches, likes

    def _fetch(
        terms: list[str], excluded_ids: list[str], fetch_limit: int
    ) -> list[dict[str, Any]]:
        clauses, params = _match_clause(terms)
        if excluded_ids:
            clauses += " AND c.id NOT IN (" + ",".join(
                "?" for _ in excluded_ids) + ")"
            params.extend(excluded_ids)
        return session.fetch_all(
            "SELECT c.id FROM storage_search_conversations AS c "
            "WHERE c.user_id = ? AND "
            + clauses
            + " ORDER BY c.updated_at_ms DESC, c.id DESC LIMIT ?",
            tuple([user_id, *params, fetch_limit]),
        )

    rows = _fetch([query], [], limit)
    words = query.split()
    if len(rows) < limit and len(words) > 1:
        found = [row["id"] for row in rows]
        rows.extend(_fetch(words, found, limit - len(rows)))

    width = 2 * radius + len(query)
    items = []
    for row in rows:
        snippet_terms = [query]
        if words and words[0] != query:
            snippet_terms.append(words[0])
        snippet_where = " OR ".join(
            f"{turn_head} LIKE ? ESCAPE '\\'" for _ in snippet_terms)
        fragment = session.fetch_one(
            "SELECT substr(s.search_text, 1, 10000) AS head "
            "FROM storage_search_turns AS s "
            "WHERE s.conversation_id=? AND s.user_id=? "
            "AND s.lane_id='main' AND (" + snippet_where + ") "
            "ORDER BY CASE WHEN " + turn_head
            + " LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, s.ordinal LIMIT 1",
            tuple([
                row["id"], user_id,
                *[_like(term) for term in snippet_terms],
                _like(query),
            ]),
        )
        head = str(fragment["head"] or "") if fragment is not None else ""
        lowered = head.lower()
        pos = lowered.find(query)
        if pos < 0 and words:
            pos = lowered.find(words[0])
        snippet = ""
        if pos >= 0 and radius > 0:
            snippet = head[max(0, pos - radius) : pos - radius + width]
            snippet = snippet.replace("\n", " ").strip()
            if snippet:
                snippet = "…" + snippet + "…"
        items.append({"id": row["id"], "snippet": snippet})
    return items


def _conversation_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise StorageError("database_protocol_error", "Invalid conversation metadata")
    unknown = set(metadata) - _CONVERSATION_METADATA
    if unknown:
        raise StorageError(
            "database_protocol_error",
            f"Unsupported conversation metadata: {sorted(unknown)}",
        )
    result = dict(metadata)
    if "settings" in result and not isinstance(result["settings"], (str, Mapping)):
        raise StorageError("database_protocol_error", "Invalid conversation settings")
    return result


def _conversation_create(session: Session, payload: Mapping[str, Any]) -> Any:
    """Create an owner-scoped conversation header without a transcript.

    Turn commands are the only way to add transcript content. Keeping header
    creation separate makes empty conversations explicit without reopening a
    whole-document writer.
    """
    conv_id, user_id = _conversation_identity(payload)
    settings = payload.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings")
    title = payload.get("title") or "New Chat"
    if not isinstance(title, str):
        raise StorageError("database_protocol_error", "Invalid conversation title")
    now = int(time.time() * 1000)
    created_at = _integer(payload, "created_at", default=now, minimum=0)
    updated_at = _integer(payload, "updated_at", default=created_at, minimum=0)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    active_id = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=?",
        (conv_id,),
    )
    trashed_id = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_trash "
        "WHERE conversation_id=?",
        (conv_id,),
    )
    if active_id is not None or trashed_id is not None:
        raise StorageError("database_conflict", "Conversation already exists")
    session.execute(
        "INSERT INTO storage_conversations "
        "(id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
        (
            conv_id,
            user_id,
            title[:500],
            _dump([]),
            created_at,
            updated_at,
            _dump(dict(settings)),
            "",
        ),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    return {"applied": True, "rev": 0}


def _conversation_settings_update(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _conversation_identity(payload)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings updates"
        )
    replace = payload.get("replace", False)
    if not isinstance(replace, bool):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings replace flag"
        )
    expected_settings = payload.get("expected_settings")
    if replace and not isinstance(expected_settings, Mapping):
        raise StorageError(
            "database_protocol_error",
            "Replacing conversation settings requires expected_settings",
        )

    session.lock_key("conversation", f"{user_id}:{conv_id}")
    row = session.fetch_one(
        "SELECT settings_json, rev FROM storage_conversations "
        "WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    if row is None:
        return {"applied": False, "missing": True, "rev": None}
    actual = int(row["rev"])
    expected = payload.get("expected_rev")
    if expected is not None:
        expected = _integer(payload, "expected_rev", minimum=0)
        if expected != actual:
            return {"applied": False, "missing": False, "rev": actual}
    settings = _load(row["settings_json"]) or {}
    if not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation settings are malformed")
    current = dict(settings)
    if replace:
        # Settings-only changes intentionally do not advance transcript rev.
        # Compare the complete settings snapshot under the storage lock instead:
        # this is a real cross-process CAS, supports key deletion, and cannot
        # clobber a concurrent mutation that happened after the caller's read.
        if current != dict(expected_settings):
            return {
                "applied": False,
                "missing": False,
                "conflict": True,
                "rev": actual,
            }
        merged = dict(updates)
    else:
        merged = current
        merged.update(dict(updates))
    session.execute(
        "UPDATE storage_conversations SET settings_json = ? "
        "WHERE id = ? AND user_id = ?",
        (_dump(merged), conv_id, user_id),
    )
    return {
        "applied": True,
        "missing": False,
        "conflict": False,
        "rev": actual,
    }


def _conversation_metadata_update(session: Session, payload: Mapping[str, Any]) -> Any:
    """Update scalar conversation metadata without touching transcript state.

    Transcript revisions describe turn mutations. Renames must therefore not
    replay an archived message array or advance ``rev`` merely to change a
    label. This operation is the sole metadata write boundary for that case.
    """
    conv_id, user_id = _conversation_identity(payload)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise StorageError(
            "database_protocol_error", "Invalid conversation metadata updates"
        )
    unknown = set(updates) - {"title", "updated_at"}
    if unknown:
        raise StorageError(
            "database_protocol_error",
            f"Unsupported conversation metadata updates: {sorted(unknown)}",
        )
    title = updates.get("title")
    if title is not None and not isinstance(title, str):
        raise StorageError("database_protocol_error", "Invalid conversation title")
    updated_at = updates.get("updated_at")
    if updated_at is not None and (
        isinstance(updated_at, bool) or not isinstance(updated_at, int)
        or updated_at < 0
    ):
        raise StorageError("database_protocol_error", "Invalid update timestamp")

    session.lock_key("conversation", f"{user_id}:{conv_id}")
    row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if row is None:
        return {"applied": False, "missing": True, "rev": None}
    assignments: list[str] = []
    values: list[Any] = []
    if title is not None:
        assignments.append("title=?")
        values.append(title)
    if updated_at is not None:
        assignments.append("updated_at_ms=?")
        values.append(updated_at)
    session.execute(
        "UPDATE storage_conversations SET " + ",".join(assignments)
        + " WHERE id=? AND user_id=?",
        tuple([*values, conv_id, user_id]),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    return {"applied": True, "missing": False, "rev": int(row["rev"])}


def _drop_active_conversation_rows(
    session: Session,
    conv_id: str,
    user_id: int,
    *,
    retain_recoverable_rows: bool,
) -> bool:
    """Remove the live authority and every executable child row.

    Search fragments and compaction archives are immutable derived/history
    rows.  A recoverable delete keeps them detached until restore or retention
    purge; a permanent purge removes them as well.  No active reader can reach
    either while the conversation header and turns are absent.
    """
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    timer_ids = session.fetch_all(
        "SELECT id FROM storage_timers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    for timer in timer_ids:
        session.execute(
            "DELETE FROM storage_timer_poll_log WHERE timer_id=?", (timer["id"],)
        )
    session.execute(
        "DELETE FROM storage_timers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_queue_items WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_autopilot_markers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "UPDATE storage_scheduled_tasks SET enabled=0 "
        "WHERE user_id=? AND (target_conv_id=? OR source_conv_id=?)",
        (user_id, conv_id, conv_id),
    )
    session.execute(
        "DELETE FROM storage_attempt_events WHERE attempt_id IN ("
        "SELECT a.attempt_id FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE t.conversation_id=? AND t.user_id=?)",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_generation_attempts WHERE turn_id IN ("
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?)",
        (conv_id, user_id),
    )
    if not retain_recoverable_rows:
        session.execute(
            "DELETE FROM storage_compaction_archives "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        )
    session.execute(
        "DELETE FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_turn_tombstones "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_changes "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_sync_heads "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    deleted = session.execute(
        "DELETE FROM storage_conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return bool(deleted)


def _purge_conversation_identity(
    session: Session, conv_id: str, user_id: int,
) -> bool:
    active = _drop_active_conversation_rows(
        session,
        conv_id,
        user_id,
        retain_recoverable_rows=False,
    )
    session.execute(
        "DELETE FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    trashed_turns = session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    trashed_header = session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    return bool(active or trashed_turns or trashed_header)


def _conversation_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically move one live conversation into the recoverable trash.

    The active header and turns disappear in the same transaction, so every
    existing read/write path naturally treats the conversation as absent and
    late executor frames cannot resurrect it.  Trash rows are normalized turn
    records, not a second live transcript implementation.
    """
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    header = session.fetch_one(
        "SELECT title,created_at_ms,updated_at_ms,settings_json,msg_count,rev "
        "FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if header is None:
        already_deleted = session.fetch_one(
            "SELECT 1 AS present FROM storage_conversation_trash "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        ) is not None
        return {"deleted": False, "alreadyDeleted": already_deleted}

    now = int(time.time() * 1000)
    count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        (conv_id, user_id),
    )
    main_count = int(count_row["c"] or 0) if count_row else 0
    session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "INSERT INTO storage_conversation_trash("
        "conversation_id,user_id,title,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,rev,deleted_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            user_id,
            str(header["title"] or "New Chat"),
            int(header["created_at_ms"] or 0),
            int(header["updated_at_ms"] or 0),
            _dump(_settings_without_live_runtime(header["settings_json"])),
            main_count,
            int(header["rev"] or 0),
            now,
        ),
    )
    session.execute(
        "INSERT INTO storage_conversation_trash_turns("
        "conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,actor,"
        "kind,run_id,status,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at) "
        "SELECT conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,"
        "actor,kind,run_id,status,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at "
        "FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "UPDATE storage_conversation_trash_turns "
        "SET status='interrupted',settlement_json=?,updated_at=? "
        "WHERE conversation_id=? AND user_id=? "
        "AND status IN ('pending','running')",
        (
            _dump({
                "outcome": "interrupted",
                "cause": "conversation_deleted",
                "resumeOptions": [],
            }),
            now,
            conv_id,
            user_id,
        ),
    )
    deleted = _drop_active_conversation_rows(
        session,
        conv_id,
        user_id,
        retain_recoverable_rows=True,
    )
    if not deleted:
        raise StorageError(
            "database_integrity", "Conversation disappeared during delete"
        )
    return {"deleted": True, "recoverable": True, "deletedAt": now}


def _conversation_restore(session: Session, payload: Mapping[str, Any]) -> Any:
    """Restore one trashed conversation without reviving executable work."""
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    if session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=?", (conv_id,)
    ) is not None:
        return {"restored": False, "conflict": True, "missing": False}
    header = session.fetch_one(
        "SELECT * FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    if header is None:
        return {"restored": False, "conflict": False, "missing": True}
    restored_revision = int(header["rev"] or 0) + 1
    settings = _settings_without_live_runtime(header["settings_json"])
    session.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            user_id,
            str(header["title"] or "New Chat"),
            _dump([]),
            int(header["created_at_ms"] or 0),
            int(header["updated_at_ms"] or 0),
            _dump(settings),
            int(header["msg_count"] or 0),
            "",
            restored_revision,
        ),
    )
    session.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,"
        "kind,run_id,status,current_attempt_id,projection_json,"
        "projection_revision,settlement_json,created_at,updated_at) "
        "SELECT turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,"
        "actor,kind,run_id,status,NULL,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at "
        "FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    restored_turns = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal",
        (conv_id, user_id),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "restored": True,
        "conflict": False,
        "missing": False,
        "rev": restored_revision,
        "turnCount": len(restored_turns),
    }


def _conversation_clone(session: Session, payload: Mapping[str, Any]) -> Any:
    """Create an independent settled turn graph from one active source."""
    source_id, user_id = _conversation_identity(payload)
    destination_id = _required_text(payload, "destination_conv_id", 256)
    for lock_id in sorted({source_id, destination_id}):
        session.lock_key("conversation", f"{user_id}:{lock_id}")
    source = session.fetch_one(
        "SELECT * FROM storage_conversations WHERE id=? AND user_id=?",
        (source_id, user_id),
    )
    if source is None:
        return {"cloned": False, "missing": True, "busy": False}
    live = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? "
        "AND status IN ('pending','running') LIMIT 1",
        (source_id, user_id),
    )
    if live is not None:
        return {"cloned": False, "missing": False, "busy": True}
    destination_taken = (
        session.fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=?",
            (destination_id,),
        ) is not None
        or session.fetch_one(
            "SELECT 1 AS present FROM storage_conversation_trash "
            "WHERE conversation_id=?",
            (destination_id,),
        ) is not None
    )
    if destination_taken:
        raise StorageError("database_conflict", "Destination conversation exists")

    raw_title = payload.get("title")
    if raw_title is None:
        title = f"{str(source['title'] or 'Untitled')} (copy)"
    elif not isinstance(raw_title, str) or not raw_title.strip():
        raise StorageError("database_protocol_error", "Invalid clone title")
    else:
        title = raw_title.strip()
    now = int(time.time() * 1000)
    source_turns = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal",
        (source_id, user_id),
    )
    turn_ids = {
        str(turn["turn_id"]): str(uuid.uuid4()) for turn in source_turns
    }
    source_archives = session.fetch_all(
        "SELECT archive_id,task_id FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=? ORDER BY created_at_ms,archive_id",
        (source_id, user_id),
    )
    archive_ids = {
        str(row["archive_id"]): f"{time.time_ns():020d}_{uuid.uuid4().hex}"
        for row in source_archives
    }
    task_ids: dict[str, str] = {}
    settings = _settings_without_live_runtime(source["settings_json"])
    main_count = sum(
        1 for turn in source_turns if str(turn["lane_id"] or "main") == "main"
    )
    session.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        (
            destination_id,
            user_id,
            title[:500],
            _dump([]),
            now,
            now,
            _dump(settings),
            main_count,
            "",
        ),
    )
    for turn in source_turns:
        old_turn_id = str(turn["turn_id"])
        raw_projection = _load(turn["projection_json"]) or {}
        if not isinstance(raw_projection, Mapping):
            raise StorageError("database_integrity", "Turn projection is malformed")
        projection = _clone_projection_value(
            raw_projection,
            turn_ids=turn_ids,
            archive_ids=archive_ids,
            task_ids=task_ids,
        )
        parent_id = turn["parent_turn_id"]
        mapped_parent = turn_ids.get(str(parent_id)) if parent_id else None
        settlement = _load(turn["settlement_json"]) or {}
        if not isinstance(settlement, Mapping):
            raise StorageError("database_integrity", "Turn settlement is malformed")
        session.execute(
            "INSERT INTO storage_conversation_turns("
            "turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,"
            "actor,kind,run_id,status,current_attempt_id,projection_json,"
            "projection_revision,settlement_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                turn_ids[old_turn_id],
                destination_id,
                user_id,
                str(turn["lane_id"] or "main"),
                mapped_parent,
                int(turn["ordinal"]),
                str(turn["actor"]),
                str(turn["kind"] or "reply"),
                "",
                str(turn["status"]),
                None,
                _dump(projection),
                1,
                _dump(dict(settlement)),
                int(turn["created_at"]),
                int(turn["updated_at"]),
            ),
        )
    _mark_conversation_search_projection_dirty(
        session, destination_id, user_id)

    for archive in source_archives:
        old_archive_id = str(archive["archive_id"])
        old_task_id = str(archive["task_id"] or "")
        new_task_id = (
            task_ids.setdefault(old_task_id, f"clone-task-{uuid.uuid4().hex}")
            if old_task_id else ""
        )
        session.execute(
            "INSERT INTO storage_compaction_archives("
            "archive_id,conversation_id,user_id,messages_json,summary,receipt_json,trigger,"
            "task_id,round_num,model,tokens_before,tokens_after,msgs_before,"
            "msgs_after,reason,payload_size,created_at_ms) "
            "SELECT ?,?,?,messages_json,summary,receipt_json,trigger,?,round_num,model,"
            "tokens_before,tokens_after,msgs_before,msgs_after,reason,"
            "payload_size,created_at_ms FROM storage_compaction_archives "
            "WHERE archive_id=? AND conversation_id=? AND user_id=?",
            (
                archive_ids[old_archive_id],
                destination_id,
                user_id,
                new_task_id,
                old_archive_id,
                source_id,
                user_id,
            ),
        )
    return {
        "cloned": True,
        "missing": False,
        "busy": False,
        "conversationId": destination_id,
        "turnCount": len(source_turns),
        "archiveCount": len(source_archives),
        "rev": 0,
    }


def _conversation_purge(session: Session, payload: Mapping[str, Any]) -> Any:
    """Permanently remove one active or trashed conversation (no HTTP route)."""
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    return {"purged": _purge_conversation_identity(session, conv_id, user_id)}


def _conversation_trash_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    """Purge a bounded oldest-first page after the recovery horizon."""
    deleted_before = _integer(payload, "deleted_before_ms", minimum=1)
    maximum = _integer(
        payload, "max_conversations", default=4, minimum=1, maximum=64
    )
    rows = session.fetch_all(
        "SELECT conversation_id,user_id FROM storage_conversation_trash "
        "WHERE deleted_at_ms<? "
        "ORDER BY deleted_at_ms,conversation_id,user_id LIMIT ?",
        (deleted_before, maximum),
    )
    purged = 0
    for row in rows:
        conv_id = str(row["conversation_id"])
        user_id = int(row["user_id"])
        session.lock_key("conversation", f"{user_id}:{conv_id}")
        purged += int(_purge_conversation_identity(session, conv_id, user_id))
    remaining = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_trash "
        "WHERE deleted_at_ms<? LIMIT 1",
        (deleted_before,),
    ) is not None
    return {"purgedConversations": purged, "remaining": remaining}

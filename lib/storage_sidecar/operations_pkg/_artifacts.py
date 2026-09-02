"""Artifact library and research-artifact operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any

import orjson

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._papers import (
    _research_lang,
)
from lib.storage_sidecar.operations_pkg._runs import (
    _json_text,
    _optional_text,
)

_ARTIFACT_FORMATS = {"markdown", "html", "svg"}


_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024
_TOOL_RESULT_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
_TOOL_RESULT_RANGE_MAX_BYTES = 64 * 1024
_TOOL_RESULT_TTL_MAX_MS = 7 * 24 * 60 * 60 * 1000


_ARTIFACT_COLUMNS = (
    "id, conv_id, task_id, msg_id, source, source_ref, format, title, "
    "content, content_sha256, size_bytes, version, parent_id, pinned, meta, "
    "created_at"
)


def _tool_result_digest(artifact_ref: str) -> str:
    prefix = "tool-result:"
    digest = str(artifact_ref or "")
    if digest.startswith(prefix):
        digest = digest[len(prefix):]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise StorageError("database_protocol_error", "Invalid tool artifact ref")
    return digest


def _tool_result_artifact_put(session: Session,
                              payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    content = _optional_text(
        payload, "content", maximum=_TOOL_RESULT_ARTIFACT_MAX_BYTES,
        scope="tool_result_artifact")
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) > _TOOL_RESULT_ARTIFACT_MAX_BYTES:
        raise StorageError("database_protocol_error", "Tool artifact is too large")
    created = _integer(payload, "created_at_ms", minimum=1)
    expires = _integer(payload, "expires_at_ms", minimum=created + 1)
    if expires - created > _TOOL_RESULT_TTL_MAX_MS:
        raise StorageError("database_protocol_error", "Tool artifact TTL is too long")
    digest = hashlib.sha256(encoded).hexdigest()
    media_type = _optional_text(
        payload, "media_type", maximum=128, scope="tool_result_artifact"
    ).strip() or "text/plain"
    session.lock_key("tool_result_artifact", f"{user_id}:{digest}")
    session.execute(
        "INSERT INTO tool_result_artifacts("
        "user_id, content_sha256, content, media_type, size_bytes, "
        "created_at_ms, expires_at_ms, last_accessed_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, content_sha256) DO UPDATE SET "
        "expires_at_ms=CASE WHEN tool_result_artifacts.expires_at_ms "
        "> excluded.expires_at_ms THEN tool_result_artifacts.expires_at_ms "
        "ELSE excluded.expires_at_ms END, "
        "last_accessed_at_ms=CASE WHEN "
        "tool_result_artifacts.last_accessed_at_ms "
        "> excluded.last_accessed_at_ms THEN "
        "tool_result_artifacts.last_accessed_at_ms "
        "ELSE excluded.last_accessed_at_ms END",
        (user_id, digest, content, media_type, len(encoded), created, expires,
         created),
    )
    effective = session.fetch_one(
        "SELECT expires_at_ms FROM tool_result_artifacts "
        "WHERE user_id = ? AND content_sha256 = ?",
        (user_id, digest),
    )
    if effective is None:
        raise StorageError(
            "database_integrity", "Tool artifact insert was not visible")
    return {
        "artifactRef": f"tool-result:{digest}",
        "contentSha256": digest,
        "sizeBytes": len(encoded),
        "expiresAtMs": int(effective["expires_at_ms"]),
    }


def _tool_result_artifact_read(session: Session,
                               payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    digest = _tool_result_digest(_required_text(
        payload, "artifact_ref", 96))
    now = _integer(payload, "now_ms", minimum=1)
    offset = _integer(payload, "offset", default=0, minimum=0)
    limit = _integer(
        payload, "limit", default=8192, minimum=1,
        maximum=_TOOL_RESULT_RANGE_MAX_BYTES)
    row = session.fetch_one(
        "SELECT content, media_type, size_bytes, expires_at_ms "
        "FROM tool_result_artifacts WHERE user_id = ? AND content_sha256 = ? "
        "AND expires_at_ms > ?",
        (user_id, digest, now),
    )
    if row is None:
        return None
    encoded = str(row["content"] or "").encode("utf-8", errors="replace")
    start = min(offset, len(encoded))
    # Cursors are byte offsets, but every emitted cursor must be a UTF-8 code
    # point boundary so concatenating ranges reconstructs the exact text.
    while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
        start += 1
    end = min(len(encoded), start + limit)
    while end < len(encoded) and end > start \
            and encoded[end] & 0xC0 == 0x80:
        end -= 1
    if end == start and end < len(encoded):
        end += 1
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end += 1
    visible = encoded[start:end].decode("utf-8", errors="strict")
    return {
        "artifactRef": f"tool-result:{digest}",
        "content": visible,
        "offset": start,
        "nextCursor": str(end) if end < len(encoded) else None,
        "truncated": end < len(encoded),
        "sizeBytes": int(row["size_bytes"] or len(encoded)),
        "visibleBytes": len(visible.encode("utf-8")),
        "mediaType": str(row["media_type"] or "text/plain"),
        "expiresAtMs": int(row["expires_at_ms"] or 0),
    }


def _tool_result_artifact_search(session: Session,
                                 payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    digest = _tool_result_digest(_required_text(
        payload, "artifact_ref", 96))
    query = _required_text(payload, "query", 200)
    now = _integer(payload, "now_ms", minimum=1)
    limit = _integer(payload, "limit", default=8, minimum=1, maximum=20)
    cursor = _integer(payload, "cursor", default=0, minimum=0)
    row = session.fetch_one(
        "SELECT content, size_bytes, expires_at_ms FROM tool_result_artifacts "
        "WHERE user_id = ? AND content_sha256 = ? AND expires_at_ms > ?",
        (user_id, digest, now),
    )
    if row is None:
        return None
    content = str(row["content"] or "")
    folded = content.casefold()
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    position = min(cursor, len(content))
    while len(matches) < limit:
        found = folded.find(needle, position)
        if found < 0:
            position = len(content)
            break
        before = max(0, found - 160)
        after = min(len(content), found + len(query) + 320)
        matches.append({
            "offset": found,
            "text": content[before:after],
        })
        position = max(found + max(1, len(query)), after)
    has_more = folded.find(needle, position) >= 0
    return {
        "artifactRef": f"tool-result:{digest}",
        "query": query,
        "items": matches,
        "nextCursor": str(position) if has_more else None,
        "truncated": has_more,
        "sizeBytes": int(row["size_bytes"] or 0),
        "expiresAtMs": int(row["expires_at_ms"] or 0),
    }


def _tool_result_artifact_prune(session: Session,
                                payload: Mapping[str, Any]) -> Any:
    now = _integer(payload, "now_ms", minimum=1)
    limit = _integer(payload, "limit", default=1000, minimum=1, maximum=5000)
    rows = session.fetch_all(
        "SELECT user_id, content_sha256 FROM tool_result_artifacts "
        "WHERE expires_at_ms <= ? ORDER BY expires_at_ms LIMIT ?",
        (now, limit),
    )
    deleted = 0
    for row in rows:
        deleted += session.execute(
            "DELETE FROM tool_result_artifacts WHERE user_id = ? "
            "AND content_sha256 = ? AND expires_at_ms <= ?",
            (row["user_id"], row["content_sha256"], now),
        )
    return {"deleted": deleted, "hasMore": len(rows) == limit}


def _artifact_document(row: Mapping[str, Any], *, content: bool) -> dict[str, Any]:
    source_ref = _load(row["source_ref"])
    meta = _load(row["meta"])
    if not isinstance(source_ref, dict) or not isinstance(meta, dict):
        raise StorageError("database_integrity", "Artifact JSON is invalid")
    result = {
        "id": row["id"],
        "conv_id": row["conv_id"],
        "task_id": row["task_id"] or "",
        "msg_id": row["msg_id"] or "",
        "source": row["source"],
        "source_ref": source_ref,
        "format": row["format"],
        "title": row["title"] or "",
        "content_sha256": row["content_sha256"],
        "size_bytes": int(row["size_bytes"] or 0),
        "version": int(row["version"] or 1),
        "parent_id": row["parent_id"] or "",
        "pinned": bool(row["pinned"]),
        "meta": meta,
        "created_at": int(row["created_at"] or 0),
    }
    if content:
        result["content"] = row["content"] or ""
    return result


def _artifact_create(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, "artifact_id", 256)
    conv_id = _required_text(payload, "conv_id", 512)
    source = _required_text(payload, "source", 256)
    artifact_format = _required_text(payload, "format", 32)
    if artifact_format not in _ARTIFACT_FORMATS:
        raise StorageError("database_protocol_error", "Invalid artifact format")
    content = _optional_text(
        payload, "content", maximum=_ARTIFACT_MAX_BYTES, scope="artifact"
    )
    size = len(content.encode("utf-8", errors="replace"))
    if size > _ARTIFACT_MAX_BYTES:
        raise StorageError("database_protocol_error", "Artifact is too large")
    source_ref = payload.get("source_ref", {})
    meta = payload.get("meta", {})
    if not isinstance(source_ref, Mapping) or not isinstance(meta, Mapping):
        raise StorageError("database_protocol_error", "Invalid artifact metadata")
    source_ref = dict(source_ref)
    meta = dict(meta)
    sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    session.lock_key("artifact.conv", conv_id)
    existing = session.fetch_one(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
        "WHERE conv_id = ? AND content_sha256 = ? AND deleted_at = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (conv_id, sha),
    )
    if existing is not None:
        return {
            "created": False,
            "artifact": _artifact_document(existing, content=False),
        }

    parent_id = _optional_text(payload, "parent_id", maximum=256, scope="artifact")
    version = 1
    path = source_ref.get("path")
    if not parent_id and isinstance(path, str) and path:
        path_predicate = (
            "source_ref ->> 'path' = ?"
            if session.backend == "postgres"
            else "json_extract(source_ref, '$.path') = ?"
        )
        candidate = session.fetch_one(
            "SELECT id, version FROM chat_artifacts "
            f"WHERE conv_id = ? AND deleted_at = 0 AND {path_predicate} "
            "ORDER BY version DESC, created_at DESC LIMIT 1",
            (conv_id, path),
        )
        if candidate is not None:
            parent_id = str(candidate["id"])
            version = int(candidate["version"] or 1) + 1
    session.execute(
        "INSERT INTO chat_artifacts("
        "id, conv_id, task_id, msg_id, source, source_ref, format, title, "
        "content, content_sha256, size_bytes, version, parent_id, pinned, "
        "meta, created_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_id,
            conv_id,
            _optional_text(payload, "task_id", maximum=512, scope="artifact"),
            _optional_text(payload, "msg_id", maximum=512, scope="artifact"),
            source,
            _json_text(source_ref),
            artifact_format,
            _optional_text(payload, "title", maximum=300, scope="artifact").strip(),
            content,
            sha,
            size,
            version,
            parent_id,
            False,
            _json_text(meta),
            _integer(payload, "created_at", minimum=0),
            0,
        ),
    )
    row = session.fetch_one(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts WHERE id = ?",
        (artifact_id,),
    )
    if row is None:
        raise StorageError("database_integrity", "Artifact insert was not visible")
    return {"created": True, "artifact": _artifact_document(row, content=False)}


def _artifact_get(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, "artifact_id", 256)
    include_content = payload.get("include_content", False)
    if not isinstance(include_content, bool):
        raise StorageError(
            "database_protocol_error", "Invalid artifact content selector"
        )
    row = session.fetch_one(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
        "WHERE id = ? AND deleted_at = 0",
        (artifact_id,),
    )
    return None if row is None else _artifact_document(row, content=include_content)


def _artifact_list(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id = _required_text(payload, "conv_id", 512)
    include_deleted = payload.get("include_deleted", False)
    if not isinstance(include_deleted, bool):
        raise StorageError(
            "database_protocol_error", "Invalid artifact deleted selector"
        )
    where = "WHERE conv_id = ?" + ("" if include_deleted else " AND deleted_at = 0")
    rows = session.fetch_all(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts {where} "
        "ORDER BY created_at DESC",
        (conv_id,),
    )
    return [_artifact_document(row, content=False) for row in rows]


def _artifact_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, "artifact_id", 256)
    count = session.execute(
        "UPDATE chat_artifacts SET deleted_at = ? WHERE id = ? AND deleted_at = 0",
        (_integer(payload, "deleted_at", minimum=1), artifact_id),
    )
    return {"deleted": bool(count)}


def _artifact_versions(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, "artifact_id", 256)
    row = session.fetch_one(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
        "WHERE id = ? AND deleted_at = 0",
        (artifact_id,),
    )
    if row is None:
        return []
    seen_up: set[str] = {str(row["id"])}
    while row.get("parent_id") and row["parent_id"] not in seen_up:
        seen_up.add(str(row["parent_id"]))
        parent = session.fetch_one(
            f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
            "WHERE id = ? AND deleted_at = 0",
            (row["parent_id"],),
        )
        if parent is None:
            break
        row = parent
    chain = [_artifact_document(row, content=False)]
    current_id = str(row["id"])
    seen_forward = {current_id}
    while True:
        child = session.fetch_one(
            f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
            "WHERE parent_id = ? AND deleted_at = 0 "
            "ORDER BY version ASC, created_at ASC LIMIT 1",
            (current_id,),
        )
        if child is None or str(child["id"]) in seen_forward:
            break
        seen_forward.add(str(child["id"]))
        chain.append(_artifact_document(child, content=False))
        current_id = str(child["id"])
    return chain


def _artifact_library(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=200)
    rows = session.fetch_all(
        f"SELECT {_ARTIFACT_COLUMNS} FROM chat_artifacts "
        "WHERE deleted_at = 0 ORDER BY pinned DESC, created_at DESC LIMIT ?",
        (limit,),
    )
    return [_artifact_document(row, content=False) for row in rows]


def _artifact_pin(session: Session, payload: Mapping[str, Any]) -> Any:
    artifact_id = _required_text(payload, "artifact_id", 256)
    pinned = payload.get("pinned")
    if not isinstance(pinned, bool):
        raise StorageError("database_protocol_error", "Invalid artifact pin flag")
    count = session.execute(
        "UPDATE chat_artifacts SET pinned = ? WHERE id = ? AND deleted_at = 0",
        (pinned, artifact_id),
    )
    return {"changed": bool(count)}


def _research_artifact_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang_key = _required_text(payload, "lang_key", 64)
    if not (lang_key.startswith("survey:") or lang_key.startswith("ideate:")):
        raise StorageError("database_protocol_error", "Invalid research artifact kind")
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        raise StorageError("database_protocol_error", "Invalid research metadata")
    created_at = _integer(payload, "created_at", minimum=0)
    session.lock_key(
        "research.artifact",
        f"{user_id}:{len(paper_hash)}:{paper_hash}{lang_key}",
    )
    session.execute(
        "INSERT INTO paper_reports("
        "user_id, paper_hash, lang, report, model, meta, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, paper_hash, lang) DO UPDATE SET "
        "report = excluded.report, model = excluded.model, "
        "meta = excluded.meta, created_at = excluded.created_at",
        (
            user_id,
            paper_hash,
            lang_key,
            _optional_text(payload, "report", maximum=10_000_000, scope="research"),
            _optional_text(payload, "model", maximum=512, scope="research"),
            _json_text(dict(meta)),
            created_at,
        ),
    )
    return {"saved": True}


def _research_artifacts_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _research_lang(payload)
    rows = session.fetch_all(
        "SELECT lang, report, meta FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? "
        "AND lang IN (?, ?) ORDER BY lang",
        (user_id, paper_hash, f"survey:{lang}", f"ideate:{lang}"),
    )
    result = []
    for row in rows:
        try:
            meta = _load(row["meta"])
        except (TypeError, orjson.JSONDecodeError) as exc:
            logger.debug("[StorageSidecar] skipping invalid research artifact: %s", exc)
            continue
        if isinstance(meta, dict):
            result.append(
                {
                    "lang_key": row["lang"],
                    "report": row["report"] or "",
                    "meta": meta,
                }
            )
    return result


def _research_directions_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=1000)
    rows = session.fetch_all(
        "SELECT paper_hash, lang, meta, created_at FROM paper_reports "
        "WHERE user_id = ? AND (lang LIKE ? OR lang LIKE ?) "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, "survey:%", "ideate:%", min(2000, limit * 2)),
    )
    folded: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            meta = _load(row["meta"])
        except (TypeError, orjson.JSONDecodeError) as exc:
            logger.debug(
                "[StorageSidecar] skipping invalid research direction: %s", exc
            )
            continue
        if not isinstance(meta, dict):
            continue
        direction = str(meta.get("direction") or "").strip()
        if not direction:
            continue
        lang_key = str(row["lang"] or "")
        lang = lang_key.split(":", 1)[1] if ":" in lang_key else "en"
        key = (str(row["paper_hash"]), lang)
        item = folded.setdefault(
            key,
            {
                "direction": direction,
                "lang": lang,
                "created_at": int(row["created_at"] or 0),
                "accepted": 0,
                "rejected": 0,
                "gate_reached": "",
                "degraded": False,
                "has_survey": False,
                "has_ideas": False,
            },
        )
        item["created_at"] = max(int(item["created_at"]), int(row["created_at"] or 0))
        if meta.get("kind") == "survey":
            item["has_survey"] = True
        elif meta.get("kind") == "ideate":
            item["has_ideas"] = True
            item["accepted"] = len(meta.get("accepted") or [])
            item["rejected"] = len(meta.get("rejected") or [])
            item["gate_reached"] = meta.get("gate_reached") or ""
            item["degraded"] = bool(meta.get("degraded"))
    return sorted(folded.values(), key=lambda item: item["created_at"], reverse=True)[
        :limit
    ]

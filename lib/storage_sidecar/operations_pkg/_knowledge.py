"""Normalized, owner-scoped knowledge-corpus storage operations.

The Sidecar owns metadata, chunk and asset integrity, bounded catalogue reads,
search-candidate selection, consent state, and enrichment claims. Application
processes never open a knowledge database or move complete corpora over RPC.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
import time
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _integer, _required_text


_DOCUMENT_CATEGORIES = frozenset({
    "all", "pdf", "document", "spreadsheet", "presentation", "image",
    "email", "ebook", "text", "other",
})
_DOCUMENT_SORTS = {
    "updated_desc": "d.updated_at DESC, d.id DESC",
    "created_desc": "d.created_at DESC, d.id DESC",
    "name_asc": "LOWER(d.name) ASC, d.id ASC",
    "size_desc": "d.size_bytes DESC, d.id DESC",
}
_ENRICHMENT_STATUSES = frozenset({
    "not_requested", "pending", "running", "ready", "no_vision", "failed",
})
_MUTABLE_ASSET_FIELDS = frozenset({
    "caption", "ocr_text", "description", "enrichment_status",
    "enrichment_model", "enrichment_error",
})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CATEGORY_SQL = """CASE
    WHEN LOWER(d.kind) = '.pdf' THEN 'pdf'
    WHEN LOWER(d.kind) IN ('.doc','.docx','.odt','.rtf') THEN 'document'
    WHEN LOWER(d.kind) IN ('.xls','.xlsx','.ods','.csv','.tsv') THEN 'spreadsheet'
    WHEN LOWER(d.kind) IN ('.ppt','.pptx','.odp') THEN 'presentation'
    WHEN LOWER(d.kind) IN ('.png','.jpg','.jpeg','.gif','.webp','.bmp') THEN 'image'
    WHEN LOWER(d.kind) = '.eml' THEN 'email'
    WHEN LOWER(d.kind) = '.epub' THEN 'ebook'
    WHEN LOWER(d.kind) IN (
        '.txt','.md','.markdown','.json','.jsonl','.xml','.html','.htm',
        '.yaml','.yml','.toml','.ini','.cfg','.rst','.log','.tex','.bib',
        '.srt','.vtt','.sql','.py','.js','.ts','.java','.c','.cpp','.h',
        '.hpp','.go','.rs','.rb','.php','.sh','.bash','.zsh','.css',
        '.scss','.less','.r','.m','.swift'
    ) THEN 'text'
    ELSE 'other'
END"""
_DOCUMENT_WITH_COUNTS = """
    SELECT d.*,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id) AS asset_count,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id
              AND a.enrichment_status IN ('pending','running'))
             AS pending_asset_count,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id
              AND a.enrichment_status IN ('no_vision','failed'))
             AS asset_issue_count
    FROM storage_knowledge_documents d
"""


def _protocol_error(message: str) -> StorageError:
    return StorageError("database_protocol_error", message)


def _owner_id(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "user_id", minimum=1)


def _nested_text(
    row: Mapping[str, Any], key: str, *, maximum: int, default: str | None = None,
) -> str:
    value = row.get(key, default)
    if not isinstance(value, str) or (default is None and not value) or len(value) > maximum:
        raise _protocol_error(f"Invalid knowledge document field: {key}")
    return value


def _nested_integer(
    row: Mapping[str, Any], key: str, *, minimum: int = 0,
    default: int | None = None,
) -> int:
    value = row.get(key, default)
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < minimum):
        raise _protocol_error(f"Invalid knowledge document field: {key}")
    return value


def _nested_number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value)) or float(value) < 0):
        raise _protocol_error(f"Invalid knowledge document field: {key}")
    return float(value)


def _json_array_text(row: Mapping[str, Any], key: str, *, default: str = "[]") -> str:
    value = _nested_text(row, key, maximum=200_000, default=default)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise _protocol_error(f"Invalid knowledge JSON field: {key}") from exc
    if not isinstance(decoded, list):
        raise _protocol_error(f"Invalid knowledge JSON field: {key}")
    return value


def _safe_stored_name(row: Mapping[str, Any]) -> str:
    value = _nested_text(row, "stored_name", maximum=512)
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise _protocol_error("Invalid knowledge stored_name")
    return value


def _search_terms(search_text: str) -> list[str]:
    """Validate and deduplicate the application's backend-neutral tokens."""
    raw_terms = search_text.split()
    if len(raw_terms) > 4096:
        raise _protocol_error("Knowledge chunk has too many search terms")
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        term = raw_term.casefold()
        if not term or len(term) > 128:
            raise _protocol_error("Invalid knowledge search term")
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _validated_document(payload: Mapping[str, Any]) -> dict:
    source = payload.get("document")
    if not isinstance(source, Mapping):
        raise _protocol_error("Invalid knowledge document")
    document_id = _nested_text(source, "id", maximum=128)
    if document_id != _required_text(payload, "document_id", 128):
        raise _protocol_error("Knowledge document identity mismatch")
    digest = _nested_text(source, "sha256", maximum=64).lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise _protocol_error("Invalid knowledge document sha256")

    raw_chunks = source.get("chunks")
    raw_assets = source.get("assets")
    if not isinstance(raw_chunks, list) or not isinstance(raw_assets, list):
        raise _protocol_error("Knowledge chunks and assets must be arrays")

    assets: list[dict] = []
    asset_ids: set[str] = set()
    for expected_ordinal, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise _protocol_error("Invalid knowledge asset")
        asset_id = _nested_text(raw_asset, "id", maximum=128)
        if asset_id in asset_ids:
            raise _protocol_error("Duplicate knowledge asset id")
        asset_ids.add(asset_id)
        ordinal = _nested_integer(raw_asset, "ordinal")
        if ordinal != expected_ordinal:
            raise _protocol_error("Knowledge asset ordinals must be contiguous")
        status = _nested_text(
            raw_asset, "enrichment_status", maximum=32,
            default="not_requested")
        if status not in _ENRICHMENT_STATUSES:
            raise _protocol_error("Invalid knowledge enrichment status")
        asset_digest = _nested_text(raw_asset, "sha256", maximum=64).lower()
        if _SHA256_RE.fullmatch(asset_digest) is None:
            raise _protocol_error("Invalid knowledge asset sha256")
        assets.append({
            "id": asset_id,
            "ordinal": ordinal,
            "kind": _nested_text(raw_asset, "kind", maximum=64),
            "stored_name": _safe_stored_name(raw_asset),
            "mime_type": _nested_text(raw_asset, "mime_type", maximum=255),
            "sha256": asset_digest,
            "size_bytes": _nested_integer(raw_asset, "size_bytes"),
            "width": _nested_integer(raw_asset, "width", default=0),
            "height": _nested_integer(raw_asset, "height", default=0),
            "page": _nested_integer(raw_asset, "page", default=0),
            "pages_json": _json_array_text(raw_asset, "pages_json"),
            "bbox_json": _json_array_text(raw_asset, "bbox_json"),
            "caption": _nested_text(raw_asset, "caption", maximum=20_000, default=""),
            "ocr_text": _nested_text(raw_asset, "ocr_text", maximum=1_000_000, default=""),
            "description": _nested_text(
                raw_asset, "description", maximum=1_000_000, default=""),
            "enrichment_status": status,
            "enrichment_model": _nested_text(
                raw_asset, "enrichment_model", maximum=512, default=""),
            "enrichment_error": _nested_text(
                raw_asset, "enrichment_error", maximum=2000, default=""),
            "created_at": _nested_number(raw_asset, "created_at"),
            "updated_at": _nested_number(raw_asset, "updated_at"),
        })

    chunks: list[dict] = []
    for expected_ordinal, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, Mapping):
            raise _protocol_error("Invalid knowledge chunk")
        ordinal = _nested_integer(raw_chunk, "ordinal")
        if ordinal != expected_ordinal:
            raise _protocol_error("Knowledge chunk ordinals must be contiguous")
        raw_refs = raw_chunk.get("assets", [])
        if not isinstance(raw_refs, list):
            raise _protocol_error("Invalid knowledge chunk assets")
        references = []
        seen_references: set[tuple[str, str]] = set()
        for reference_ordinal, raw_reference in enumerate(raw_refs):
            if not isinstance(raw_reference, Mapping):
                raise _protocol_error("Invalid knowledge asset reference")
            asset_id = _nested_text(raw_reference, "id", maximum=128)
            relation = _nested_text(
                raw_reference, "relation", maximum=64, default="evidence")
            if asset_id not in asset_ids:
                raise _protocol_error("Knowledge chunk references an unknown asset")
            identity = (asset_id, relation)
            if identity in seen_references:
                raise _protocol_error("Duplicate knowledge asset reference")
            seen_references.add(identity)
            references.append({
                "id": asset_id,
                "relation": relation,
                "ordinal": reference_ordinal,
            })
        chunks.append({
            "ordinal": ordinal,
            "section": _nested_text(raw_chunk, "section", maximum=20_000, default=""),
            "location": _nested_text(raw_chunk, "location", maximum=20_000, default=""),
            "content": _nested_text(raw_chunk, "content", maximum=4_000_000),
            "search_text": _nested_text(
                raw_chunk, "search_text", maximum=4_000_000),
            "assets": references,
        })
        chunks[-1]["terms"] = _search_terms(chunks[-1]["search_text"])

    chunk_count = _nested_integer(source, "chunk_count")
    if chunk_count != len(chunks):
        raise _protocol_error("Knowledge chunk_count does not match chunks")
    return {
        "id": document_id,
        "sha256": digest,
        "name": _nested_text(source, "name", maximum=240),
        "stored_name": _safe_stored_name(source),
        "kind": _nested_text(source, "kind", maximum=32),
        "size_bytes": _nested_integer(source, "size_bytes"),
        "method": _nested_text(source, "method", maximum=255),
        "warnings_json": _json_array_text(source, "warnings_json"),
        "text_chars": _nested_integer(source, "text_chars"),
        "chunk_count": chunk_count,
        "pages": _nested_integer(source, "pages", default=0),
        "created_at": _nested_number(source, "created_at"),
        "updated_at": _nested_number(source, "updated_at"),
        "chunks": chunks,
        "assets": assets,
    }


def _document_metadata(row: Mapping[str, Any]) -> dict:
    return {
        "id": str(row["id"]),
        "sha256": str(row["sha256"]),
        "name": str(row["name"]),
        "stored_name": str(row["stored_name"]),
        "kind": str(row["kind"]),
        "size_bytes": int(row["size_bytes"]),
        "method": str(row["method"]),
        "warnings_json": str(row["warnings_json"]),
        "text_chars": int(row["text_chars"]),
        "chunk_count": int(row["chunk_count"]),
        "pages": int(row["pages"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "asset_count": int(row.get("asset_count") or 0),
        "pending_asset_count": int(row.get("pending_asset_count") or 0),
        "asset_issue_count": int(row.get("asset_issue_count") or 0),
    }


def _asset_from_row(row: Mapping[str, Any]) -> dict:
    return {
        "id": str(row["id"]),
        "ordinal": int(row["ordinal"]),
        "kind": str(row["kind"]),
        "stored_name": str(row["stored_name"]),
        "mime_type": str(row["mime_type"]),
        "sha256": str(row["sha256"]),
        "size_bytes": int(row["size_bytes"]),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "page": int(row["page"]),
        "pages_json": str(row["pages_json"]),
        "bbox_json": str(row["bbox_json"]),
        "caption": str(row["caption"]),
        "ocr_text": str(row["ocr_text"]),
        "description": str(row["description"]),
        "enrichment_status": str(row["enrichment_status"]),
        "enrichment_model": str(row["enrichment_model"]),
        "enrichment_error": str(row["enrichment_error"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _asset_projection(row: Mapping[str, Any]) -> dict:
    return {
        **_asset_from_row(row),
        "document_id": str(row["document_id"]),
        "document_name": str(row.get("document_name") or ""),
    }


def _load_document(session: Session, user_id: int, document_id: str) -> dict | None:
    row = session.fetch_one(
        _DOCUMENT_WITH_COUNTS + " WHERE d.user_id=? AND d.id=?",
        (user_id, document_id),
    )
    if row is None:
        return None
    document = _document_metadata(row)
    assets = session.fetch_all(
        "SELECT * FROM storage_knowledge_assets "
        "WHERE user_id=? AND document_id=? ORDER BY ordinal",
        (user_id, document_id),
    )
    links = session.fetch_all(
        "SELECT chunk_ordinal, asset_id, relation, ordinal "
        "FROM storage_knowledge_chunk_assets "
        "WHERE user_id=? AND document_id=? "
        "ORDER BY chunk_ordinal, ordinal, asset_id",
        (user_id, document_id),
    )
    references: dict[int, list[dict]] = {}
    for link in links:
        references.setdefault(int(link["chunk_ordinal"]), []).append({
            "id": str(link["asset_id"]),
            "relation": str(link["relation"]),
        })
    chunk_rows = session.fetch_all(
        "SELECT ordinal, section, location, content, search_text "
        "FROM storage_knowledge_chunks "
        "WHERE user_id=? AND document_id=? ORDER BY ordinal",
        (user_id, document_id),
    )
    document["chunks"] = [{
        "ordinal": int(chunk["ordinal"]),
        "section": str(chunk["section"]),
        "location": str(chunk["location"]),
        "content": str(chunk["content"]),
        "search_text": str(chunk["search_text"]),
        "assets": references.get(int(chunk["ordinal"]), []),
    } for chunk in chunk_rows]
    document["assets"] = [_asset_from_row(asset) for asset in assets]
    return document


def _insert_document_dependents(
    session: Session, user_id: int, document: Mapping[str, Any],
) -> None:
    """Insert assets, chunks, links, and search projection as one unit."""
    document_id = str(document["id"])
    for asset in document["assets"]:
        session.execute(
            "INSERT INTO storage_knowledge_assets("
            "user_id,id,document_id,ordinal,kind,stored_name,mime_type,sha256,"
            "size_bytes,width,height,page,pages_json,bbox_json,caption,ocr_text,"
            "description,enrichment_status,enrichment_model,enrichment_error,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, asset["id"], document_id, asset["ordinal"],
                asset["kind"], asset["stored_name"], asset["mime_type"],
                asset["sha256"], asset["size_bytes"], asset["width"],
                asset["height"], asset["page"], asset["pages_json"],
                asset["bbox_json"], asset["caption"], asset["ocr_text"],
                asset["description"], asset["enrichment_status"],
                asset["enrichment_model"], asset["enrichment_error"],
                asset["created_at"], asset["updated_at"],
            ),
        )
    for chunk in document["chunks"]:
        session.execute(
            "INSERT INTO storage_knowledge_chunks("
            "user_id,document_id,ordinal,section,location,content,search_text) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                user_id, document_id, chunk["ordinal"], chunk["section"],
                chunk["location"], chunk["content"], chunk["search_text"],
            ),
        )
        for reference in chunk["assets"]:
            session.execute(
                "INSERT INTO storage_knowledge_chunk_assets("
                "user_id,document_id,chunk_ordinal,asset_id,relation,ordinal) "
                "VALUES (?,?,?,?,?,?)",
                (
                    user_id, document_id, chunk["ordinal"], reference["id"],
                    reference["relation"], reference["ordinal"],
                ),
            )
        for term in chunk["terms"]:
            session.execute(
                "INSERT INTO storage_knowledge_terms("
                "user_id,term,document_id,chunk_ordinal) VALUES (?,?,?,?)",
                (user_id, term, document_id, chunk["ordinal"]),
            )


def _insert_document(session: Session, user_id: int, document: Mapping[str, Any]) -> None:
    session.execute(
        "INSERT INTO storage_knowledge_documents("
        "user_id,id,sha256,name,stored_name,kind,size_bytes,method,warnings_json,"
        "text_chars,chunk_count,pages,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, document["id"], document["sha256"], document["name"],
            document["stored_name"], document["kind"], document["size_bytes"],
            document["method"], document["warnings_json"],
            document["text_chars"], document["chunk_count"], document["pages"],
            document["created_at"], document["updated_at"],
        ),
    )
    _insert_document_dependents(session, user_id, document)


def _knowledge_document_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    return [_document_metadata(row) for row in session.fetch_all(
        _DOCUMENT_WITH_COUNTS
        + " WHERE d.user_id=? ORDER BY d.created_at DESC, d.id DESC",
        (user_id,),
    )]


def _knowledge_document_get(session: Session, payload: Mapping[str, Any]) -> Any:
    return _load_document(
        session, _owner_id(payload), _required_text(payload, "document_id", 128))


def _knowledge_document_content(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    """Return one bounded parsed-body page without materializing the document."""
    user_id = _owner_id(payload)
    document_id = _required_text(payload, "document_id", 128)
    offset = _integer(payload, "offset", default=0, minimum=0)
    limit = _integer(payload, "limit", default=80, minimum=1, maximum=200)
    row = session.fetch_one(
        _DOCUMENT_WITH_COUNTS + " WHERE d.user_id=? AND d.id=?",
        (user_id, document_id),
    )
    if row is None:
        return None

    chunk_rows = session.fetch_all(
        "SELECT ordinal,section,location,content,search_text "
        "FROM storage_knowledge_chunks "
        "WHERE user_id=? AND document_id=? AND ordinal>=? "
        "ORDER BY ordinal LIMIT ?",
        (user_id, document_id, offset, limit),
    )
    upper_ordinal = offset + limit
    links = session.fetch_all(
        "SELECT chunk_ordinal,asset_id,relation,ordinal "
        "FROM storage_knowledge_chunk_assets "
        "WHERE user_id=? AND document_id=? "
        "AND chunk_ordinal>=? AND chunk_ordinal<? "
        "ORDER BY chunk_ordinal,ordinal,asset_id",
        (user_id, document_id, offset, upper_ordinal),
    )
    references: dict[int, list[dict]] = {}
    for link in links:
        references.setdefault(int(link["chunk_ordinal"]), []).append({
            "id": str(link["asset_id"]),
            "relation": str(link["relation"]),
        })
    chunks = [{
        "ordinal": int(chunk["ordinal"]),
        "section": str(chunk["section"]),
        "location": str(chunk["location"]),
        "content": str(chunk["content"]),
        "search_text": str(chunk["search_text"]),
        "assets": references.get(int(chunk["ordinal"]), []),
    } for chunk in chunk_rows]
    total = int(row["chunk_count"])
    return {
        "document": _document_metadata(row),
        "chunks": chunks,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total_items": total,
            "has_more": offset + len(chunks) < total,
        },
    }


def _knowledge_document_find_digest(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    digest = _required_text(payload, "sha256", 64).lower()
    if _SHA256_RE.fullmatch(digest) is None:
        raise _protocol_error("Invalid knowledge document sha256")
    row = session.fetch_one(
        _DOCUMENT_WITH_COUNTS + " WHERE d.user_id=? AND d.sha256=?",
        (_owner_id(payload), digest),
    )
    return _document_metadata(row) if row is not None else None


def _knowledge_document_create(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    document = _validated_document(payload)
    session.lock_key("knowledge.digest", f'{user_id}:{document["sha256"]}')
    existing = session.fetch_one(
        "SELECT id FROM storage_knowledge_documents WHERE user_id=? AND sha256=?",
        (user_id, document["sha256"]),
    )
    if existing is not None:
        return {
            "created": False,
            "document": _load_document(session, user_id, str(existing["id"])),
        }
    _insert_document(session, user_id, document)
    settings = session.fetch_one(
        "SELECT 1 AS present FROM storage_knowledge_settings WHERE user_id=?",
        (user_id,),
    )
    if settings is None:
        now = time.time()
        session.execute(
            "INSERT INTO storage_knowledge_settings("
            "user_id,enabled,visual_enrichment,updated_at) VALUES (?,?,?,?)",
            (user_id, 1, 0, now),
        )
    return {
        "created": True,
        "document": _load_document(session, user_id, str(document["id"])),
    }


def _knowledge_document_replace(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    document = _validated_document(payload)
    document_id = str(document["id"])
    session.lock_key("knowledge.document", f"{user_id}:{document_id}")
    current = session.fetch_one(
        "SELECT id FROM storage_knowledge_documents WHERE user_id=? AND id=?",
        (user_id, document_id),
    )
    if current is None:
        return None
    old_assets = session.fetch_all(
        "SELECT stored_name FROM storage_knowledge_assets "
        "WHERE user_id=? AND document_id=? ORDER BY ordinal",
        (user_id, document_id),
    )
    session.execute(
        "DELETE FROM storage_knowledge_chunk_assets WHERE user_id=? AND document_id=?",
        (user_id, document_id),
    )
    session.execute(
        "DELETE FROM storage_knowledge_chunks WHERE user_id=? AND document_id=?",
        (user_id, document_id),
    )
    session.execute(
        "DELETE FROM storage_knowledge_assets WHERE user_id=? AND document_id=?",
        (user_id, document_id),
    )
    session.execute(
        "UPDATE storage_knowledge_documents SET sha256=?,name=?,stored_name=?,"
        "kind=?,size_bytes=?,method=?,warnings_json=?,text_chars=?,chunk_count=?,"
        "pages=?,created_at=?,updated_at=? WHERE user_id=? AND id=?",
        (
            document["sha256"], document["name"], document["stored_name"],
            document["kind"], document["size_bytes"], document["method"],
            document["warnings_json"], document["text_chars"],
            document["chunk_count"], document["pages"], document["created_at"],
            document["updated_at"], user_id, document_id,
        ),
    )
    _insert_document_dependents(session, user_id, document)
    result = _load_document(session, user_id, document_id)
    return {
        **(result or {}),
        "_replaced_asset_names": [str(row["stored_name"]) for row in old_assets],
    }


def _knowledge_document_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    document_id = _required_text(payload, "document_id", 128)
    session.lock_key("knowledge.document", f"{user_id}:{document_id}")
    document = _load_document(session, user_id, document_id)
    if document is None:
        return {"deleted": False, "document": None}
    deleted = session.execute(
        "DELETE FROM storage_knowledge_documents WHERE user_id=? AND id=?",
        (user_id, document_id),
    )
    return {"deleted": bool(deleted), "document": document}


def _knowledge_settings_get(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT enabled,visual_enrichment FROM storage_knowledge_settings "
        "WHERE user_id=?",
        (_owner_id(payload),),
    )
    return {
        "enabled": bool(row and row["enabled"]),
        "visual_enrichment": bool(row and row["visual_enrichment"]),
    }


def _knowledge_settings_patch(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    # Consent changes and claims mutate the same owner-scoped asset state.
    session.lock_key("knowledge.enrichment", str(user_id))
    current = _knowledge_settings_get(session, payload)
    for field in ("enabled", "visual_enrichment"):
        if field in payload:
            if not isinstance(payload[field], bool):
                raise _protocol_error(f"Invalid knowledge setting: {field}")
            current[field] = payload[field]
    now = time.time()
    present = session.fetch_one(
        "SELECT 1 AS present FROM storage_knowledge_settings WHERE user_id=?",
        (user_id,),
    )
    if present is None:
        session.execute(
            "INSERT INTO storage_knowledge_settings("
            "user_id,enabled,visual_enrichment,updated_at) VALUES (?,?,?,?)",
            (user_id, int(current["enabled"]),
             int(current["visual_enrichment"]), now),
        )
    else:
        session.execute(
            "UPDATE storage_knowledge_settings SET enabled=?,"
            "visual_enrichment=?,updated_at=? WHERE user_id=?",
            (int(current["enabled"]), int(current["visual_enrichment"]), now, user_id),
        )
    if payload.get("visual_enrichment") is True:
        session.execute(
            "UPDATE storage_knowledge_assets SET enrichment_status='pending',"
            "enrichment_error='',updated_at=? WHERE user_id=? "
            "AND enrichment_status IN ('not_requested','no_vision','failed')",
            (now, user_id),
        )
    elif payload.get("visual_enrichment") is False:
        # Work which has not started must stop being eligible immediately.
        # A running provider request cannot be unsent, so preserve its state.
        session.execute(
            "UPDATE storage_knowledge_assets SET enrichment_status='not_requested',"
            "updated_at=? WHERE user_id=? AND enrichment_status='pending'",
            (now, user_id),
        )
    return current


def _knowledge_availability(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    row = session.fetch_one(
        "SELECT s.enabled, EXISTS(SELECT 1 FROM storage_knowledge_documents d "
        "WHERE d.user_id=s.user_id) AS has_documents "
        "FROM storage_knowledge_settings s WHERE s.user_id=?",
        (user_id,),
    )
    return {"available": bool(row and row["enabled"] and row["has_documents"])}


def _knowledge_asset_get(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT a.*,d.name AS document_name FROM storage_knowledge_assets a "
        "JOIN storage_knowledge_documents d "
        "ON d.user_id=a.user_id AND d.id=a.document_id "
        "WHERE a.user_id=? AND a.id=?",
        (_owner_id(payload), _required_text(payload, "asset_id", 128)),
    )
    return _asset_projection(row) if row is not None else None


def _knowledge_enrichment_activity(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN enrichment_status IN ('pending','running') "
        "THEN 1 ELSE 0 END),0) AS pending_assets,"
        "COALESCE(SUM(CASE WHEN enrichment_status IN ('no_vision','failed') "
        "THEN 1 ELSE 0 END),0) AS asset_issues "
        "FROM storage_knowledge_assets WHERE user_id=?",
        (_owner_id(payload),),
    ) or {}
    settings = _knowledge_settings_get(session, payload)
    return {
        "pending_assets": int(row.get("pending_assets") or 0),
        "asset_issues": int(row.get("asset_issues") or 0),
        "visual_enrichment": settings["visual_enrichment"],
    }


def _knowledge_enrichment_owners(session: Session, payload: Mapping[str, Any]) -> Any:
    del payload
    return [int(row["user_id"]) for row in session.fetch_all(
        "SELECT user_id FROM storage_knowledge_settings "
        "WHERE visual_enrichment<>0 ORDER BY user_id")]


def _knowledge_asset_claim(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    session.lock_key("knowledge.enrichment", str(user_id))
    stale_before = time.time() - (30 * 60)
    row = session.fetch_one(
        "SELECT a.*,d.name AS document_name FROM storage_knowledge_assets a "
        "JOIN storage_knowledge_documents d "
        "ON d.user_id=a.user_id AND d.id=a.document_id "
        "WHERE a.user_id=? AND (a.enrichment_status='pending' OR "
        "(a.enrichment_status='running' AND a.updated_at<?)) "
        "ORDER BY CASE a.kind WHEN 'image' THEN 0 WHEN 'figure' THEN 1 "
        "WHEN 'table' THEN 2 ELSE 3 END,a.created_at,a.document_id,a.ordinal "
        "LIMIT 1",
        (user_id, stale_before),
    )
    if row is None:
        return None
    now = time.time()
    session.execute(
        "UPDATE storage_knowledge_assets SET enrichment_status='running',"
        "enrichment_error='',updated_at=? WHERE user_id=? AND id=? AND "
        "(enrichment_status='pending' OR "
        "(enrichment_status='running' AND updated_at<?))",
        (now, user_id, str(row["id"]), stale_before),
    )
    return _asset_projection({
        **dict(row),
        "enrichment_status": "running",
        "enrichment_error": "",
        "updated_at": now,
    })


def _knowledge_asset_update(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    asset_id = _required_text(payload, "asset_id", 128)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise _protocol_error("Invalid knowledge asset update")
    unknown = set(updates) - _MUTABLE_ASSET_FIELDS
    if unknown:
        raise _protocol_error("Knowledge asset update contains immutable fields")
    normalized: dict[str, str] = {}
    for field, value in updates.items():
        maximum = 1_000_000 if field in {"description", "ocr_text"} else 2000
        if not isinstance(value, str) or len(value) > maximum:
            raise _protocol_error(f"Invalid knowledge asset update: {field}")
        if field == "enrichment_status" and value not in _ENRICHMENT_STATUSES:
            raise _protocol_error("Invalid knowledge enrichment status")
        normalized[field] = value
    session.lock_key("knowledge.enrichment", str(user_id))
    current = _knowledge_asset_get(session, payload)
    if current is None:
        return {"updated": False}
    now = time.time()
    assignments = ",".join(f"{field}=?" for field in normalized)
    session.execute(
        f"UPDATE storage_knowledge_assets SET {assignments},updated_at=? "
        "WHERE user_id=? AND id=?",
        (*normalized.values(), now, user_id, asset_id),
    )
    chunk_content = payload.get("chunk_content")
    chunk_search_text = payload.get("chunk_search_text")
    if chunk_content is not None or chunk_search_text is not None:
        if (chunk_content is not None and not isinstance(chunk_content, str)) or (
                chunk_search_text is not None
                and not isinstance(chunk_search_text, str)):
            raise _protocol_error("Invalid enriched knowledge chunk")
        linked_chunks = session.fetch_all(
            "SELECT DISTINCT chunk_ordinal FROM storage_knowledge_chunk_assets "
            "WHERE user_id=? AND asset_id=? AND document_id=? "
            "ORDER BY chunk_ordinal",
            (user_id, asset_id, current["document_id"]),
        )
        fields = []
        values: list[Any] = []
        if chunk_content is not None:
            fields.append("content=?")
            values.append(chunk_content)
        if chunk_search_text is not None:
            fields.append("search_text=?")
            values.append(chunk_search_text)
        session.execute(
            "UPDATE storage_knowledge_chunks SET " + ",".join(fields)
            + " WHERE user_id=? AND document_id=? AND ordinal IN ("
            "SELECT chunk_ordinal FROM storage_knowledge_chunk_assets "
            "WHERE user_id=? AND asset_id=? AND document_id=?)",
            (*values, user_id, current["document_id"], user_id, asset_id,
             current["document_id"]),
        )
        if chunk_search_text is not None:
            terms = _search_terms(chunk_search_text)
            for linked_chunk in linked_chunks:
                chunk_ordinal = int(linked_chunk["chunk_ordinal"])
                session.execute(
                    "DELETE FROM storage_knowledge_terms WHERE user_id=? "
                    "AND document_id=? AND chunk_ordinal=?",
                    (user_id, current["document_id"], chunk_ordinal),
                )
                for term in terms:
                    session.execute(
                        "INSERT INTO storage_knowledge_terms("
                        "user_id,term,document_id,chunk_ordinal) "
                        "VALUES (?,?,?,?)",
                        (user_id, term, current["document_id"], chunk_ordinal),
                    )
    updated = _knowledge_asset_get(session, payload)
    return {"updated": True, "asset": updated}


def _knowledge_assets_mark_no_vision(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    user_id = _owner_id(payload)
    session.lock_key("knowledge.enrichment", str(user_id))
    changed = session.execute(
        "UPDATE storage_knowledge_assets SET enrichment_status='no_vision',"
        "enrichment_error='No configured vision model',updated_at=? "
        "WHERE user_id=? AND enrichment_status='pending'",
        (time.time(), user_id),
    )
    return {"changed": int(changed)}


def _escaped_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _knowledge_catalog(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    page = _integer(payload, "page", default=1, minimum=1)
    page_size = _integer(payload, "page_size", default=30, minimum=1, maximum=100)
    query = str(payload.get("query") or "").strip()
    category = str(payload.get("category") or "all").lower()
    sort = str(payload.get("sort") or "updated_desc").lower()
    if len(query) > 200 or category not in _DOCUMENT_CATEGORIES or sort not in _DOCUMENT_SORTS:
        raise _protocol_error("Invalid knowledge catalogue filter")

    where = ["d.user_id=?"]
    params: list[Any] = [user_id]
    if query:
        where.append("LOWER(d.name) LIKE ? ESCAPE '!'")
        params.append("%" + _escaped_like(query.casefold()) + "%")
    if category != "all":
        where.append(f"({_CATEGORY_SQL})=?")
        params.append(category)
    where_sql = " WHERE " + " AND ".join(where)
    filtered = session.fetch_one(
        "SELECT COUNT(*) AS count FROM storage_knowledge_documents d" + where_sql,
        tuple(params),
    ) or {"count": 0}
    total_items = int(filtered["count"] or 0)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    bounded_page = min(page, total_pages)
    offset = (bounded_page - 1) * page_size
    rows = session.fetch_all(
        _DOCUMENT_WITH_COUNTS.replace(
            "    FROM storage_knowledge_documents d\n",
            f",\n           ({_CATEGORY_SQL}) AS category\n"
            "    FROM storage_knowledge_documents d\n",
        )
        + where_sql + " ORDER BY " + _DOCUMENT_SORTS[sort] + " LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    )
    documents = []
    for row in rows:
        documents.append({**_document_metadata(row), "category": str(row["category"])})

    document_totals = session.fetch_one(
        "SELECT COUNT(*) AS documents,COALESCE(SUM(chunk_count),0) AS chunks,"
        "COALESCE(SUM(text_chars),0) AS text_chars,"
        "COALESCE(SUM(size_bytes),0) AS size_bytes "
        "FROM storage_knowledge_documents WHERE user_id=?",
        (user_id,),
    ) or {}
    asset_totals = session.fetch_one(
        "SELECT COUNT(*) AS assets,"
        "COALESCE(SUM(CASE WHEN enrichment_status IN ('pending','running') "
        "THEN 1 ELSE 0 END),0) AS pending_assets,"
        "COALESCE(SUM(CASE WHEN enrichment_status IN ('no_vision','failed') "
        "THEN 1 ELSE 0 END),0) AS asset_issues "
        "FROM storage_knowledge_assets WHERE user_id=?",
        (user_id,),
    ) or {}
    facet_rows = session.fetch_all(
        f"SELECT ({_CATEGORY_SQL}) AS category,COUNT(*) AS count "
        "FROM storage_knowledge_documents d WHERE d.user_id=? "
        f"GROUP BY ({_CATEGORY_SQL}) ORDER BY count DESC,category ASC",
        (user_id,),
    )
    settings = _knowledge_settings_get(session, payload)
    totals = {
        "documents": int(document_totals.get("documents") or 0),
        "chunks": int(document_totals.get("chunks") or 0),
        "assets": int(asset_totals.get("assets") or 0),
        "pending_assets": int(asset_totals.get("pending_assets") or 0),
        "asset_issues": int(asset_totals.get("asset_issues") or 0),
        "text_chars": int(document_totals.get("text_chars") or 0),
        "size_bytes": int(document_totals.get("size_bytes") or 0),
    }
    return {
        **settings,
        "available": bool(settings["enabled"] and totals["documents"]),
        "documents": documents,
        "totals": totals,
        "facets": [{"category": str(row["category"]), "count": int(row["count"])}
                   for row in facet_rows],
        "pagination": {
            "page": bounded_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_previous": bounded_page > 1,
            "has_next": bounded_page < total_pages,
        },
        "filters": {"query": query, "category": category, "sort": sort},
    }


def _knowledge_search_candidates(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    raw_tokens = payload.get("tokens")
    if not isinstance(raw_tokens, list):
        raise _protocol_error("Invalid knowledge search tokens")
    tokens = []
    for value in raw_tokens[:32]:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise _protocol_error("Invalid knowledge search token")
        tokens.append(value.casefold())
    if not tokens:
        return []
    limit = _integer(payload, "limit", default=80, minimum=1, maximum=200)
    tokens = list(dict.fromkeys(tokens))
    placeholders = ",".join("?" for _ in tokens)
    candidate_keys = session.fetch_all(
        "SELECT t.document_id,t.chunk_ordinal,COUNT(*) AS matched_terms,"
        "MAX(d.updated_at) AS document_updated_at "
        "FROM storage_knowledge_terms t "
        "JOIN storage_knowledge_documents d "
        "ON d.user_id=t.user_id AND d.id=t.document_id "
        "WHERE t.user_id=? AND t.term IN (" + placeholders + ") "
        "GROUP BY t.document_id,t.chunk_ordinal "
        "ORDER BY matched_terms DESC,document_updated_at DESC,"
        "t.document_id,t.chunk_ordinal LIMIT ?",
        (user_id, *tokens, limit),
    )
    if not candidate_keys:
        return []
    key_clauses = []
    row_params: list[Any] = [user_id]
    matched_terms: dict[tuple[str, int], int] = {}
    for candidate in candidate_keys:
        document_id = str(candidate["document_id"])
        chunk_ordinal = int(candidate["chunk_ordinal"])
        key_clauses.append("(c.document_id=? AND c.ordinal=?)")
        row_params.extend((document_id, chunk_ordinal))
        matched_terms[(document_id, chunk_ordinal)] = int(
            candidate["matched_terms"])
    rows = session.fetch_all(
        "SELECT c.document_id,c.ordinal,c.section,c.location,c.content,"
        "d.name,d.kind,next.content AS next_content,next.section AS next_section "
        "FROM storage_knowledge_chunks c "
        "JOIN storage_knowledge_documents d "
        "ON d.user_id=c.user_id AND d.id=c.document_id "
        "LEFT JOIN storage_knowledge_chunks next "
        "ON next.user_id=c.user_id AND next.document_id=c.document_id "
        "AND next.ordinal=c.ordinal+1 "
        "WHERE c.user_id=? AND (" + " OR ".join(key_clauses) + ")",
        tuple(row_params),
    )
    link_clauses = []
    link_params: list[Any] = [user_id]
    for row in rows:
        link_clauses.append("(l.document_id=? AND l.chunk_ordinal=?)")
        link_params.extend((str(row["document_id"]), int(row["ordinal"])))
    asset_rows = session.fetch_all(
        "SELECT l.document_id AS linked_document_id,"
        "l.chunk_ordinal AS linked_chunk_ordinal,a.* "
        "FROM storage_knowledge_chunk_assets l "
        "JOIN storage_knowledge_assets a ON a.user_id=l.user_id AND a.id=l.asset_id "
        "WHERE l.user_id=? AND (" + " OR ".join(link_clauses) + ") "
        "ORDER BY l.document_id,l.chunk_ordinal,l.ordinal,a.id",
        tuple(link_params),
    )
    assets_by_chunk: dict[tuple[str, int], list[dict]] = {}
    for asset in asset_rows:
        key = (str(asset["linked_document_id"]), int(asset["linked_chunk_ordinal"]))
        assets_by_chunk.setdefault(key, []).append(_asset_from_row(asset))
    return [{
        "document_id": str(row["document_id"]),
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "ordinal": int(row["ordinal"]),
        "section": str(row["section"]),
        "location": str(row["location"]),
        "content": str(row["content"]),
        "next_content": str(row.get("next_content") or ""),
        "next_section": str(row.get("next_section") or ""),
        "assets": assets_by_chunk.get(
            (str(row["document_id"]), int(row["ordinal"])), []),
        "matched_terms": matched_terms.get(
            (str(row["document_id"]), int(row["ordinal"])), 0),
        "bm25_score": 0,
    } for row in rows]


def _knowledge_owner_clear(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _owner_id(payload)
    session.lock_key("knowledge.owner", str(user_id))
    row = session.fetch_one(
        "SELECT COUNT(*) AS count FROM storage_knowledge_documents WHERE user_id=?",
        (user_id,),
    ) or {"count": 0}
    session.execute(
        "DELETE FROM storage_knowledge_documents WHERE user_id=?", (user_id,))
    session.execute(
        "DELETE FROM storage_knowledge_settings WHERE user_id=?", (user_id,))
    return {"deleted_documents": int(row["count"] or 0)}


__all__ = [name for name in globals() if name.startswith("_knowledge_")]

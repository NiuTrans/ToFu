"""Research paper, translation, library, podcast, and daily-cost operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import orjson

from lib.log import get_logger
from lib.paper.contracts import (
    PAPER_FANIN_MAX_PAPERS,
    PAPER_FANIN_MAX_TEXT_CHARS,
    PAPER_QA_MAX_SOURCE_CHARS,
    PAPER_REPORT_REOPEN_MAX_SIBLINGS,
    PAPER_TRANSLATION_MAX_OUTPUT_BYTES,
    PAPER_TRANSLATION_MAX_OUTPUT_CHARS,
)
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _number,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._runs import (
    _json_text,
    _optional_text,
)


def _research_lang(payload: Mapping[str, Any]) -> str:
    lang = _required_text(payload, "lang", 32)
    if ":" in lang:
        raise StorageError("database_protocol_error", "Invalid research language")
    return lang


def _paper_report_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 64)
    meta = payload.get("meta", {})
    if not isinstance(meta, Mapping):
        raise StorageError("database_protocol_error", "Invalid paper report metadata")
    session.lock_key(
        "paper.report", f"{user_id}:{len(paper_hash)}:{paper_hash}{lang}")
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
            lang,
            _optional_text(payload, "report", maximum=10_000_000, scope="paper report"),
            _optional_text(payload, "model", maximum=512, scope="paper report"),
            _json_text(dict(meta)),
            _integer(payload, "created_at", minimum=0),
        ),
    )
    return {"saved": True}


def _paper_report_projection(row, *, user_id: int, paper_hash: str, lang: str):
    """Decode the one canonical Sidecar projection for a report row."""
    try:
        meta = _load(row["meta"])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug("[StorageSidecar] invalid paper report metadata: %s", exc)
        meta = {}
    return {
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": lang,
        "report": row["report"] or "",
        "model": row["model"] or "",
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": int(row["created_at"] or 0),
    }


def _paper_report_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 64)
    max_report_chars = payload.get("max_report_chars")
    select_projection = "report, model, meta, created_at"
    args: tuple[Any, ...] = (user_id, paper_hash, lang)
    if max_report_chars is not None:
        max_report_chars = _integer(
            payload, "max_report_chars", minimum=1,
            maximum=PAPER_FANIN_MAX_TEXT_CHARS)
        select_projection = (
            "substr(report, 1, ?) AS report, '' AS model, "
            "'{}' AS meta, created_at"
        )
        args = (max_report_chars, user_id, paper_hash, lang)
    row = session.fetch_one(
        f"SELECT {select_projection} "
        "FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        args,
    )
    if row is None:
        return None
    return _paper_report_projection(
        row, user_id=user_id, paper_hash=paper_hash, lang=lang)


def _paper_report_resolve(session: Session, payload: Mapping[str, Any]) -> Any:
    """Resolve one owner's preferred/fallback report in a single SQL read."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    preferred_lang = _required_text(payload, "preferred_lang", 64)
    languages = [preferred_lang]
    if payload.get("fallback_lang") is not None:
        fallback_lang = _required_text(payload, "fallback_lang", 64)
        if fallback_lang != preferred_lang:
            languages.append(fallback_lang)
    placeholders = ",".join("?" for _ in languages)
    row = session.fetch_one(
        "SELECT lang, report, model, meta, created_at FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? "
        f"AND lang IN ({placeholders}) "
        "ORDER BY CASE WHEN lang = ? THEN 0 ELSE 1 END, "
        "created_at DESC, lang ASC LIMIT 1",
        (user_id, paper_hash, *languages, preferred_lang),
    )
    if row is None:
        return None
    return _paper_report_projection(
        row,
        user_id=user_id,
        paper_hash=paper_hash,
        lang=row["lang"] or "",
    )


def _paper_report_reopen(session: Session, payload: Mapping[str, Any]) -> Any:
    """Resolve a base report and its bounded sibling artifacts in one read."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    preferred_lang = _required_text(payload, "preferred_lang", 64)
    fallback_lang = None
    if payload.get("fallback_lang") is not None:
        fallback_lang = _required_text(payload, "fallback_lang", 64)

    offered_groups = payload.get("sibling_langs_by_base", {})
    if not isinstance(offered_groups, Mapping):
        raise StorageError(
            "database_protocol_error",
            "paper report reopen sibling groups must be an object",
        )

    base_langs = [preferred_lang]
    if fallback_lang and fallback_lang != preferred_lang:
        base_langs.append(fallback_lang)
    allowed_base_langs = set(base_langs)
    sibling_langs_by_base = {}
    offered_sibling_count = 0
    for base_lang, offered_siblings in offered_groups.items():
        if base_lang not in allowed_base_langs or not isinstance(offered_siblings, list):
            raise StorageError(
                "database_protocol_error",
                "Invalid paper report sibling group",
            )
        offered_sibling_count += len(offered_siblings)
        if offered_sibling_count > PAPER_REPORT_REOPEN_MAX_SIBLINGS:
            raise StorageError(
                "database_protocol_error",
                "paper report reopen requires at most "
                f"{PAPER_REPORT_REOPEN_MAX_SIBLINGS} sibling languages",
            )
        normalized_siblings = []
        for value in offered_siblings:
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 64
            ):
                raise StorageError(
                    "database_protocol_error",
                    "Invalid paper report sibling language",
                )
            normalized = value.strip()
            if normalized not in normalized_siblings:
                normalized_siblings.append(normalized)
        sibling_langs_by_base[base_lang] = normalized_siblings

    base_placeholders = ",".join("?" for _ in base_langs)
    selected_artifact_clauses = ["p.lang = r.lang"]
    selected_artifact_args = []
    for base_lang in base_langs:
        sibling_langs = sibling_langs_by_base.get(base_lang, [])
        if not sibling_langs:
            continue
        sibling_placeholders = ",".join("?" for _ in sibling_langs)
        selected_artifact_clauses.append(
            f"(r.lang = ? AND p.lang IN ({sibling_placeholders}))")
        selected_artifact_args.extend((base_lang, *sibling_langs))
    rows = session.fetch_all(
        "WITH resolved_lang AS ("
        "SELECT lang FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? "
        f"AND lang IN ({base_placeholders}) "
        "ORDER BY CASE WHEN lang = ? THEN 0 ELSE 1 END, "
        "created_at DESC, lang ASC LIMIT 1) "
        "SELECT p.lang, p.report, p.model, p.meta, p.created_at "
        "FROM paper_reports AS p JOIN resolved_lang AS r ON 1 = 1 "
        "WHERE p.user_id = ? AND p.paper_hash = ? AND ("
        + " OR ".join(selected_artifact_clauses)
        + ") ORDER BY p.lang ASC",
        (
            user_id,
            paper_hash,
            *base_langs,
            preferred_lang,
            user_id,
            paper_hash,
            *selected_artifact_args,
        ),
    )
    rows_by_lang = {row["lang"] or "": row for row in rows}
    resolved_lang = preferred_lang if preferred_lang in rows_by_lang else ""
    if not resolved_lang and fallback_lang in rows_by_lang:
        resolved_lang = fallback_lang
    resolved = None
    if resolved_lang:
        resolved = _paper_report_projection(
            rows_by_lang[resolved_lang],
            user_id=user_id,
            paper_hash=paper_hash,
            lang=resolved_lang,
        )
    return {
        "report": resolved,
        "siblings": [
            _paper_report_projection(
                rows_by_lang[lang],
                user_id=user_id,
                paper_hash=paper_hash,
                lang=lang,
            )
            for lang in sibling_langs_by_base.get(resolved_lang, [])
            if lang in rows_by_lang
        ],
    }


def _paper_report_excerpts(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return bounded report text for at most 40 requested paper hashes."""
    user_id = _integer(payload, "user_id", minimum=1)
    lang = _required_text(payload, "lang", 64)
    max_report_chars = _integer(
        payload, "max_report_chars", minimum=1,
        maximum=PAPER_FANIN_MAX_TEXT_CHARS)
    offered = payload.get("paper_hashes")
    if (
        not isinstance(offered, list)
        or len(offered) > PAPER_FANIN_MAX_PAPERS
    ):
        raise StorageError(
            "database_protocol_error",
            "paper report excerpts require at most "
            f"{PAPER_FANIN_MAX_PAPERS} paper_hashes",
        )
    paper_hashes = []
    seen = set()
    for value in offered:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise StorageError(
                "database_protocol_error", "Invalid paper report hash")
        normalized = value.strip()
        if normalized not in seen:
            seen.add(normalized)
            paper_hashes.append(normalized)
    if not paper_hashes:
        return []
    placeholders = ",".join("?" for _ in paper_hashes)
    rows = session.fetch_all(
        "SELECT paper_hash, substr(report, 1, ?) AS report, created_at "
        "FROM paper_reports WHERE user_id=? AND lang=? "
        f"AND paper_hash IN ({placeholders}) ORDER BY paper_hash ASC",
        (max_report_chars, user_id, lang, *paper_hashes),
    )
    return [{
        "user_id": user_id,
        "paper_hash": row["paper_hash"],
        "lang": lang,
        "report": row["report"] or "",
        "created_at": int(row["created_at"] or 0),
    } for row in rows]


def _paper_report_latest(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return this owner's newest report variant for one paper."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    row = session.fetch_one(
        "SELECT lang, report, model, meta, created_at FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? "
        "ORDER BY created_at DESC, lang ASC LIMIT 1",
        (user_id, paper_hash),
    )
    if row is None:
        return None
    try:
        meta = _load(row["meta"])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug("[StorageSidecar] invalid paper report metadata: %s", exc)
        meta = {}
    return {
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": row["lang"] or "",
        "report": row["report"] or "",
        "model": row["model"] or "",
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": int(row["created_at"] or 0),
    }


def _paper_report_second_pass_merge(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Atomically merge one billed second pass without a callback over RPC."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 64)
    name = _required_text(payload, "name", 64)
    entry = payload.get("entry")
    if not isinstance(entry, Mapping):
        raise StorageError("database_protocol_error", "Invalid paper second-pass entry")
    # Validate and detach the entire document before taking the row lock.
    entry = _load(_dump(dict(entry)))
    session.lock_key(
        "paper.report.meta", f"{user_id}:{len(paper_hash)}:{paper_hash}{lang}")
    row = session.fetch_one(
        "SELECT meta FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (user_id, paper_hash, lang),
    )
    if row is None:
        return {"found": False, "meta": None}
    try:
        current = _load(row["meta"])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug("invalid paper report metadata during merge: %s", exc)
        current = {}
    if not isinstance(current, dict):
        current = {}
    passes = current.get("secondPasses")
    if not isinstance(passes, dict):
        passes = {}
        current["secondPasses"] = passes
    passes[name] = entry

    token_keys = (
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )

    def integer(value: Any) -> int:
        return (
            int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        )

    total = {
        key: integer(
            current.get(
                key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:])
            )
        )
        for key in token_keys
    }
    for pass_meta in passes.values():
        usage = pass_meta.get("usage") if isinstance(pass_meta, Mapping) else None
        if isinstance(usage, Mapping):
            for key in token_keys:
                total[key] += integer(usage.get(key))
    current["totalUsage"] = total

    def cost(value: Any) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0

    for suffix in ("Cny", "Usd"):
        field = f"cost{suffix}"
        total_cost = cost(current.get(field)) + sum(
            cost(item.get(field))
            for item in passes.values()
            if isinstance(item, Mapping)
        )
        if total_cost:
            current[f"totalCost{suffix}"] = total_cost

    session.execute(
        "UPDATE paper_reports SET meta = ? "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (_json_text(current), user_id, paper_hash, lang),
    )
    return {"found": True, "meta": current}


def _paper_report_second_pass_accumulate(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Atomically add one pass invocation to an existing aggregate."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 64)
    name = _required_text(payload, "name", 64)
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, Mapping):
        raise StorageError("database_protocol_error", "Invalid paper second-pass usage")
    token_keys = (
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )
    usage = {
        key: _integer(raw_usage, key, default=0, minimum=0, maximum=10_000_000_000)
        for key in token_keys
    }
    incremental_costs = {}
    for suffix in ("Cny", "Usd"):
        field = f"cost{suffix}"
        incremental_costs[field] = (
            _number(payload, field, minimum=0, maximum=1_000_000_000)
            if field in payload
            else 0.0
        )

    session.lock_key(
        "paper.report.meta", f"{user_id}:{len(paper_hash)}:{paper_hash}{lang}")
    row = session.fetch_one(
        "SELECT meta FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (user_id, paper_hash, lang),
    )
    if row is None:
        return {"found": False, "meta": None}
    try:
        current = _load(row["meta"])
    except (TypeError, orjson.JSONDecodeError) as exc:
        logger.debug("invalid paper report metadata during accumulate: %s", exc)
        current = {}
    if not isinstance(current, dict):
        current = {}
    passes = current.get("secondPasses")
    if not isinstance(passes, dict):
        passes = {}
        current["secondPasses"] = passes
    entry = passes.get(name)
    if not isinstance(entry, dict):
        entry = {}
        passes[name] = entry
    previous_usage = entry.get("usage")
    if not isinstance(previous_usage, Mapping):
        previous_usage = {}

    def integer(value: Any) -> int:
        return (
            int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        )

    entry["usage"] = {
        key: integer(previous_usage.get(key)) + usage[key] for key in token_keys
    }
    entry["calls"] = integer(entry.get("calls")) + 1
    for field, increment in incremental_costs.items():
        previous = entry.get(field)
        prior = (
            float(previous)
            if isinstance(previous, (int, float)) and not isinstance(previous, bool)
            else 0.0
        )
        if prior or increment:
            entry[field] = prior + increment

    total_usage = {
        key: integer(
            current.get(
                key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:])
            )
        )
        for key in token_keys
    }
    for pass_meta in passes.values():
        pass_usage = pass_meta.get("usage") if isinstance(pass_meta, Mapping) else None
        if isinstance(pass_usage, Mapping):
            for key in token_keys:
                total_usage[key] += integer(pass_usage.get(key))
    current["totalUsage"] = total_usage
    for suffix in ("Cny", "Usd"):
        field = f"cost{suffix}"
        body = current.get(field)
        body_cost = (
            float(body)
            if isinstance(body, (int, float)) and not isinstance(body, bool)
            else 0.0
        )
        total_cost = body_cost + sum(
            float(item.get(field) or 0)
            for item in passes.values()
            if isinstance(item, Mapping)
            and isinstance(item.get(field), (int, float))
            and not isinstance(item.get(field), bool)
        )
        if total_cost:
            current[f"totalCost{suffix}"] = total_cost

    session.execute(
        "UPDATE paper_reports SET meta = ? "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (_json_text(current), user_id, paper_hash, lang),
    )
    return {"found": True, "meta": current}


def _paper_translation_text(payload: Mapping[str, Any]) -> str:
    """Validate one artifact against task, replay, and storage budgets."""
    text = _optional_text(
        payload,
        "text",
        maximum=PAPER_TRANSLATION_MAX_OUTPUT_CHARS,
        scope="paper translation",
    )
    if len(text.encode("utf-8")) > PAPER_TRANSLATION_MAX_OUTPUT_BYTES:
        raise StorageError(
            "storage_payload_too_large",
            "Paper translation exceeds "
            f"{PAPER_TRANSLATION_MAX_OUTPUT_BYTES} UTF-8 bytes",
        )
    return text


def _paper_translation_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 128)
    session.lock_key(
        "paper.translation", f"{user_id}:{len(paper_hash)}:{paper_hash}{lang}")
    session.execute(
        "INSERT INTO paper_translations("
        "user_id, paper_hash, lang, text, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, paper_hash, lang) DO UPDATE SET "
        "text = excluded.text, model = excluded.model, "
        "created_at = excluded.created_at",
        (
            user_id,
            paper_hash,
            lang,
            _paper_translation_text(payload),
            _optional_text(payload, "model", maximum=512, scope="paper translation"),
            _integer(payload, "created_at", minimum=0),
        ),
    )
    return {"saved": True}


def _paper_translation_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 128)
    row = session.fetch_one(
        "SELECT substr(text, 1, ?) AS bounded_text, model, created_at "
        "FROM paper_translations "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (
            PAPER_TRANSLATION_MAX_OUTPUT_CHARS + 1,
            user_id,
            paper_hash,
            lang,
        ),
    )
    if row is None:
        return None
    text = row["bounded_text"] or ""
    if (
        len(text) > PAPER_TRANSLATION_MAX_OUTPUT_CHARS
        or len(text.encode("utf-8")) > PAPER_TRANSLATION_MAX_OUTPUT_BYTES
    ):
        logger.warning(
            "[StorageSidecar] ignoring oversized legacy paper translation "
            "user=%s hash=%s lang=%s",
            user_id,
            paper_hash[:12],
            lang,
        )
        return None
    return {
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": lang,
        "text": text,
        "model": row["model"] or "",
        "created_at": int(row["created_at"] or 0),
    }


def _paper_library_put(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_id = _required_text(payload, "id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _optional_text(
        payload, "paper_hash", maximum=128, scope="paper library"
    )
    session.lock_key("paper.library", f"{user_id}:{paper_id}")
    values = (
        paper_id,
        user_id,
        _optional_text(payload, "title", maximum=1000, scope="paper library"),
        _optional_text(payload, "pdf_url", maximum=10_000, scope="paper library"),
        _optional_text(payload, "pdf_filename", maximum=2000, scope="paper library"),
        _optional_text(payload, "arxiv_id", maximum=256, scope="paper library"),
        paper_hash,
        _optional_text(
            payload, "parsed_text", maximum=20_000_000, scope="paper library"
        ),
        _optional_text(payload, "parser_version", maximum=256, scope="paper library"),
        _optional_text(
            payload,
            "qa_history",
            default="[]",
            maximum=10_000_000,
            scope="paper library",
        ),
        _optional_text(
            payload, "images", default="[]", maximum=10_000_000, scope="paper library"
        ),
        _optional_text(
            payload,
            "babel_cache",
            default="{}",
            maximum=10_000_000,
            scope="paper library",
        ),
        _integer(payload, "page_count", default=0, minimum=0),
        _optional_text(payload, "folder_id", maximum=512, scope="paper library"),
        _integer(payload, "created_at", minimum=0),
        _integer(payload, "updated_at", minimum=0),
    )
    session.execute(
        "INSERT INTO paper_library("
        "id, user_id, title, pdf_url, pdf_filename, arxiv_id, paper_hash, "
        "parsed_text, parser_version, qa_history, images, babel_cache, "
        "page_count, folder_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id, user_id) DO UPDATE SET "
        "title = excluded.title, pdf_url = excluded.pdf_url, "
        "pdf_filename = excluded.pdf_filename, arxiv_id = excluded.arxiv_id, "
        "paper_hash = excluded.paper_hash, parsed_text = excluded.parsed_text, "
        "parser_version = excluded.parser_version, "
        "qa_history = excluded.qa_history, images = excluded.images, "
        "babel_cache = excluded.babel_cache, page_count = excluded.page_count, "
        "folder_id = excluded.folder_id, updated_at = excluded.updated_at",
        values,
    )
    return {"saved": True}


def _paper_library_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    paper_id = _required_text(payload, "id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    session.lock_key("paper.library", f"{user_id}:{paper_id}")
    deleted = session.execute(
        "DELETE FROM paper_library WHERE id=? AND user_id=?", (paper_id, user_id)
    )
    return {"deleted": bool(deleted)}


def _paper_library_recent(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    exclude_hash = _optional_text(
        payload, "exclude_paper_hash", maximum=128, scope="paper library"
    )
    limit = _integer(payload, "limit", default=40, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT title, arxiv_id FROM paper_library "
        "WHERE user_id = ? AND paper_hash != ? AND title != '' "
        "ORDER BY updated_at DESC LIMIT ?",
        (user_id, exclude_hash, limit),
    )
    return [
        {
            "title": row["title"] or "",
            "arxiv_id": row["arxiv_id"] or "",
        }
        for row in rows
    ]


def _paper_library_detail_item(
    row: Mapping[str, Any],
    *,
    include_babel_cache: bool = True,
) -> dict[str, Any]:
    """Decode one complete bookshelf row into the stable wire projection."""
    item = {
        "id": row["id"],
        "title": row["title"] or "",
        "pdfUrl": row["pdf_url"] or "",
        "pdfFilename": row["pdf_filename"] or "",
        "arxivId": row["arxiv_id"] or "",
        "paperHash": row["paper_hash"] or "",
        "parsedText": row["parsed_text"] or "",
        "qaHistory": _load(row["qa_history"]) or [],
        "images": _load(row["images"]) or [],
        "pageCount": int(row["page_count"] or 0),
        "folderId": row["folder_id"] or "",
        "parserVersion": row["parser_version"] or "",
        "createdAt": int(row["created_at"] or 0),
        "updatedAt": int(row["updated_at"] or 0),
        "hasReport": bool(row["has_report"]),
    }
    if include_babel_cache:
        item["babelCache"] = _load(row["babel_cache"]) or {}
    return item


def _paper_library_list(session: Session, payload: Mapping[str, Any]) -> Any:
    """Compatibility projection for clients that still require full rows."""
    user_id = _integer(payload, "user_id", minimum=1)
    rows = session.fetch_all(
        "SELECT library.id, library.title, library.pdf_url, "
        "library.pdf_filename, library.arxiv_id, library.paper_hash, "
        "library.parsed_text, library.qa_history, library.images, "
        "library.babel_cache, library.page_count, library.folder_id, "
        "library.parser_version, library.created_at, library.updated_at, "
        "EXISTS(SELECT 1 FROM paper_reports AS report "
        "WHERE report.user_id=library.user_id "
        "AND report.paper_hash=library.paper_hash) AS has_report "
        "FROM paper_library AS library "
        "WHERE library.user_id = ? ORDER BY library.updated_at DESC",
        (user_id,),
    )
    return [_paper_library_detail_item(row) for row in rows]


def _paper_library_summaries(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return bookshelf metadata without loading content or auxiliary JSON."""
    user_id = _integer(payload, "user_id", minimum=1)
    rows = session.fetch_all(
        "SELECT library.id, library.title, library.pdf_url, "
        "library.pdf_filename, library.arxiv_id, library.paper_hash, "
        "library.page_count, library.folder_id, library.created_at, "
        "library.updated_at, "
        "EXISTS(SELECT 1 FROM paper_reports AS report "
        "WHERE report.user_id=library.user_id "
        "AND report.paper_hash=library.paper_hash) AS has_report "
        "FROM paper_library AS library "
        "WHERE library.user_id = ? ORDER BY library.updated_at DESC",
        (user_id,),
    )
    return [
        {
            "id": row["id"],
            "title": row["title"] or "",
            "pdfUrl": row["pdf_url"] or "",
            "pdfFilename": row["pdf_filename"] or "",
            "arxivId": row["arxiv_id"] or "",
            "paperHash": row["paper_hash"] or "",
            "pageCount": int(row["page_count"] or 0),
            "folderId": row["folder_id"] or "",
            "createdAt": int(row["created_at"] or 0),
            "updatedAt": int(row["updated_at"] or 0),
            "hasReport": bool(row["has_report"]),
        }
        for row in rows
    ]


def _paper_library_get(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return one complete owner-scoped bookshelf row by its durable id."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_id = _required_text(payload, "id", 256)
    row = session.fetch_one(
        "SELECT library.id, library.title, library.pdf_url, "
        "library.pdf_filename, library.arxiv_id, library.paper_hash, "
        "library.parsed_text, library.qa_history, library.images, "
        "library.babel_cache, library.page_count, library.folder_id, "
        "library.parser_version, library.created_at, library.updated_at, "
        "EXISTS(SELECT 1 FROM paper_reports AS report "
        "WHERE report.user_id=library.user_id "
        "AND report.paper_hash=library.paper_hash) AS has_report "
        "FROM paper_library AS library "
        "WHERE library.user_id = ? AND library.id = ?",
        (user_id, paper_id),
    )
    if row is None:
        return None
    return _paper_library_detail_item(row)


def _paper_library_reader(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return reader state without the duplicate legacy Babel JSON column."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_id = _required_text(payload, "id", 256)
    row = session.fetch_one(
        "SELECT library.id, library.title, library.pdf_url, "
        "library.pdf_filename, library.arxiv_id, library.paper_hash, "
        "library.parsed_text, library.qa_history, library.images, "
        "library.page_count, library.folder_id, library.parser_version, "
        "library.created_at, library.updated_at, "
        "EXISTS(SELECT 1 FROM paper_reports AS report "
        "WHERE report.user_id=library.user_id "
        "AND report.paper_hash=library.paper_hash) AS has_report "
        "FROM paper_library AS library "
        "WHERE library.user_id = ? AND library.id = ?",
        (user_id, paper_id),
    )
    if row is None:
        return None
    return _paper_library_detail_item(row, include_babel_cache=False)


def _paper_library_inputs(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return only bounded paper bodies requested by an owner-scoped pipeline.

    Unlike the UI bookshelf projection, this operation does not load auxiliary
    JSON columns.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    max_text_chars = _integer(
        payload, "max_text_chars", default=0, minimum=0,
        maximum=PAPER_FANIN_MAX_TEXT_CHARS)
    offered = payload.get("arxiv_ids")
    if (
        not isinstance(offered, list)
        or len(offered) > PAPER_FANIN_MAX_PAPERS
    ):
        raise StorageError(
            "database_protocol_error",
            "paper library inputs require at most "
            f"{PAPER_FANIN_MAX_PAPERS} arxiv_ids",
        )
    arxiv_ids = []
    seen = set()
    for value in offered:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise StorageError(
                "database_protocol_error", "Invalid paper library arxiv_id")
        normalized = value.strip()
        if normalized not in seen:
            seen.add(normalized)
            arxiv_ids.append(normalized)
    if not arxiv_ids:
        return []
    placeholders = ",".join("?" for _ in arxiv_ids)
    rows = session.fetch_all(
        "SELECT id, title, arxiv_id, paper_hash, "
        "substr(parsed_text, 1, ?) AS parsed_text, "
        "length(parsed_text) AS parsed_text_length, parser_version, "
        "page_count, folder_id, created_at, updated_at FROM paper_library "
        f"WHERE user_id=? AND arxiv_id IN ({placeholders}) "
        "ORDER BY updated_at DESC",
        (max_text_chars, user_id, *arxiv_ids),
    )
    return [{
        "id": row["id"],
        "title": row["title"] or "",
        "arxivId": row["arxiv_id"] or "",
        "paperHash": row["paper_hash"] or "",
        "parsedText": row["parsed_text"] or "",
        "parsedTextLength": int(row["parsed_text_length"] or 0),
        "parserVersion": row["parser_version"] or "",
        "pageCount": int(row["page_count"] or 0),
        "folderId": row["folder_id"] or "",
        "createdAt": int(row["created_at"] or 0),
        "updatedAt": int(row["updated_at"] or 0),
    } for row in rows]


def _paper_library_identity(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    offered_max = payload.get("max_text_chars")
    include_text_length = payload.get("include_text_length", True)
    if not isinstance(include_text_length, bool):
        raise StorageError(
            "database_protocol_error",
            "Invalid include_text_length in storage request",
        )
    if offered_max is None:
        if not include_text_length:
            raise StorageError(
                "database_protocol_error",
                "Text length may be omitted only for a zero-text projection",
            )
        row = session.fetch_one(
            "SELECT title, arxiv_id, parsed_text, "
            "length(parsed_text) AS parsed_text_length FROM paper_library "
            "WHERE user_id = ? AND paper_hash = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, paper_hash),
        )
    else:
        max_text_chars = _integer(
            payload,
            "max_text_chars",
            minimum=0,
            maximum=PAPER_QA_MAX_SOURCE_CHARS,
        )
        if not include_text_length:
            if max_text_chars != 0:
                raise StorageError(
                    "database_protocol_error",
                    "Text length may be omitted only for a zero-text projection",
                )
            row = session.fetch_one(
                "SELECT title, arxiv_id, '' AS parsed_text, "
                "0 AS parsed_text_length FROM paper_library "
                "WHERE user_id = ? AND paper_hash = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id, paper_hash),
            )
        else:
            row = session.fetch_one(
                "SELECT title, arxiv_id, "
                "substr(parsed_text, 1, ?) AS parsed_text, "
                "length(parsed_text) AS parsed_text_length "
                "FROM paper_library "
                "WHERE user_id = ? AND paper_hash = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (max_text_chars, user_id, paper_hash),
            )
    if row is None:
        return None
    return {
        "title": row["title"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "parsed_text": row["parsed_text"] or "",
        "parsed_text_length": int(row["parsed_text_length"] or 0),
    }


def _paper_library_title_backfill(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Heal placeholder titles for one content-addressed paper atomically."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    title = _required_text(payload, "title", 1000).strip()
    if not title:
        raise StorageError(
            "database_protocol_error", "Invalid title in storage request"
        )
    session.lock_key("paper.library.title", f"{user_id}:{paper_hash}")
    rows = session.fetch_all(
        "SELECT id, user_id, title FROM paper_library "
        "WHERE user_id = ? AND paper_hash = ? ORDER BY updated_at DESC",
        (user_id, paper_hash),
    )
    if not rows:
        return {"title": title, "updated": 0}

    def is_placeholder(value: Any) -> bool:
        normalized = str(value or "").strip().lower()
        return not normalized or normalized.startswith(("arxiv:", "arxiv "))

    authoritative = next(
        (
            str(row["title"] or "").strip()
            for row in rows
            if not is_placeholder(row["title"])
        ),
        "",
    )
    updated = 0
    now = int(time.time())
    for row in rows:
        if not is_placeholder(row["title"]):
            continue
        updated += session.execute(
            "UPDATE paper_library SET title = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND title = ?",
            (title, now, row["id"], user_id, row["title"]),
        )
    return {"title": authoritative or title, "updated": updated}


def _paper_note_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _optional_text(payload, "lang", maximum=64, scope="paper note")
    rows = session.fetch_all(
        "SELECT id, paper_hash, lang, anchor, note, created_at, updated_at "
        "FROM paper_notes WHERE user_id = ? AND paper_hash = ? AND lang = ? "
        "ORDER BY created_at ASC, id ASC",
        (user_id, paper_hash, lang),
    )
    result = []
    for row in rows:
        try:
            anchor = _load(row["anchor"])
        except (TypeError, orjson.JSONDecodeError) as exc:
            logger.debug("[StorageSidecar] invalid paper note anchor: %s", exc)
            anchor = {}
        result.append({
            "id": row["id"],
            "paper_hash": row["paper_hash"],
            "lang": row["lang"],
            "anchor": anchor if isinstance(anchor, dict) else {},
            "note": row["note"] or "",
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        })
    return result


def _paper_note_create(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    note_id = _required_text(payload, "id", 256)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _optional_text(payload, "lang", maximum=64, scope="paper note")
    anchor = payload.get("anchor", {})
    if not isinstance(anchor, Mapping):
        raise StorageError("database_protocol_error", "Invalid paper note anchor")
    note = _required_text(payload, "note", 100_000)
    created_at = _integer(payload, "created_at", minimum=0)
    updated_at = _integer(payload, "updated_at", minimum=0)
    session.lock_key("paper.note", f"{user_id}:{note_id}")
    session.execute(
        "INSERT INTO paper_notes("
        "user_id, id, paper_hash, lang, anchor, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, note_id, paper_hash, lang, _json_text(dict(anchor)), note,
         created_at, updated_at),
    )
    return {"saved": True}


def _paper_note_update(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    note_id = _required_text(payload, "id", 256)
    note = _required_text(payload, "note", 100_000)
    updated_at = _integer(payload, "updated_at", minimum=0)
    session.lock_key("paper.note", f"{user_id}:{note_id}")
    changed = session.execute(
        "UPDATE paper_notes SET note = ?, updated_at = ? "
        "WHERE user_id = ? AND id = ?",
        (note, updated_at, user_id, note_id),
    )
    return {"updated": bool(changed)}


def _paper_note_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    note_id = _required_text(payload, "id", 256)
    session.lock_key("paper.note", f"{user_id}:{note_id}")
    changed = session.execute(
        "DELETE FROM paper_notes WHERE user_id = ? AND id = ?",
        (user_id, note_id),
    )
    return {"deleted": bool(changed)}


def _daily_cost_date(payload: Mapping[str, Any], key: str = "date") -> str:
    value = _required_text(payload, key, 10)
    if (
        len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not (value[:4] + value[5:7] + value[8:]).isdigit()
    ):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in storage request"
        )
    return value


def _daily_cost_month(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    year = _integer(payload, "year", minimum=1970, maximum=9999)
    month = _integer(payload, "month", minimum=1, maximum=12)
    rows = session.fetch_all(
        "SELECT date, cost, conversations_json, computed_at "
        "FROM daily_cost_cache WHERE user_id = ? AND date LIKE ? "
        "ORDER BY date",
        (user_id, f"{year:04d}-{month:02d}-%"),
    )
    return [
        {
            "date": row["date"],
            "cost": float(row["cost"] or 0),
            "conversations": _load(row["conversations_json"]) or {},
            "computed_at": int(row["computed_at"] or 0),
        }
        for row in rows
    ]


def _daily_cost_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    date = _daily_cost_date(payload)
    conversations = payload.get("conversations", {})
    if not isinstance(conversations, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid conversations in storage request"
        )
    cost = _number(payload, "cost", minimum=0, maximum=1_000_000_000)
    computed_at = _integer(payload, "computed_at", minimum=0)
    session.lock_key("daily.cost", f"{user_id}:{date}")
    session.execute(
        "INSERT INTO daily_cost_cache("
        "user_id, date, cost, conversations_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, date) DO UPDATE SET "
        "cost = excluded.cost, "
        "conversations_json = excluded.conversations_json, "
        "computed_at = excluded.computed_at",
        (user_id, date, cost, _json_text(dict(conversations)), computed_at),
    )
    return {"saved": True}


def _daily_cost_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    raw_date = payload.get("date")
    if raw_date is None:
        count = session.execute(
            "DELETE FROM daily_cost_cache WHERE user_id = ?", (user_id,)
        )
    else:
        date = _daily_cost_date(payload)
        session.lock_key("daily.cost", f"{user_id}:{date}")
        count = session.execute(
            "DELETE FROM daily_cost_cache WHERE user_id = ? AND date = ?",
            (user_id, date),
        )
    return {"deleted": int(count)}


def _daily_cost_persisted_dates(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    values = payload.get("dates")
    if not isinstance(values, list) or len(values) > 366:
        raise StorageError(
            "database_protocol_error", "Invalid dates in storage request"
        )
    dates = []
    for value in values:
        dates.append(_daily_cost_date({"date": value}))
    dates = list(dict.fromkeys(dates))
    if not dates:
        return {"dates": []}
    placeholders = ",".join("?" for _ in dates)
    rows = session.fetch_all(
        "SELECT date FROM daily_cost_cache WHERE user_id = ? "
        f"AND date IN ({placeholders})",
        (user_id, *dates),
    )
    return {"dates": [row["date"] for row in rows]}


def _daily_cost_latest(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    row = session.fetch_one(
        "SELECT date, cost, conversations_json, computed_at "
        "FROM daily_cost_cache WHERE user_id = ? "
        "ORDER BY date DESC LIMIT 1",
        (user_id,),
    )
    if row is None:
        return None
    return {
        "date": row["date"],
        "cost": float(row["cost"] or 0),
        "conversations": _load(row["conversations_json"]) or {},
        "computed_at": int(row["computed_at"] or 0),
    }


def _paper_podcast_key(
    payload: Mapping[str, Any],
) -> tuple[int, str, str, str, str]:
    return (
        _integer(payload, "user_id", minimum=1),
        _required_text(payload, "paper_hash", 128),
        _required_text(payload, "mode", 64),
        _required_text(payload, "lang", 32),
        _optional_text(payload, "voice", maximum=256, scope="paper podcast"),
    )


def _paper_podcast_upsert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id, paper_hash, mode, lang, voice = _paper_podcast_key(payload)
    script = payload.get("script", {})
    meta = payload.get("meta", {})
    if not isinstance(script, Mapping) or not isinstance(meta, Mapping):
        raise StorageError("database_protocol_error", "Invalid paper podcast document")
    status = _required_text(payload, "status", 64)
    if status not in {
        "generating",
        "interrupted",
        "done",
        "script_only",
        "error",
        "aborted",
    }:
        raise StorageError("database_protocol_error", "Invalid paper podcast status")
    now = _integer(payload, "updated_at", minimum=0)
    created_at = _integer(payload, "created_at", minimum=0)
    session.lock_key(
        "paper.podcast",
        f"{user_id}:{len(paper_hash)}:{paper_hash}{len(mode)}:{mode}"
        f"{len(lang)}:{lang}{voice}",
    )
    session.execute(
        "INSERT INTO paper_podcasts("
        "user_id, paper_hash, mode, lang, voice, status, script_json, file_path, "
        "duration_sec, model, tts_model, meta, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, paper_hash, mode, lang, voice) DO UPDATE SET "
        "status = excluded.status, script_json = excluded.script_json, "
        "file_path = excluded.file_path, duration_sec = excluded.duration_sec, "
        "model = excluded.model, tts_model = excluded.tts_model, "
        "meta = excluded.meta, updated_at = excluded.updated_at",
        (
            user_id,
            paper_hash,
            mode,
            lang,
            voice,
            status,
            _json_text(dict(script)),
            _optional_text(payload, "file_path", maximum=10_000, scope="paper podcast"),
            _number(payload, "duration_sec", minimum=0, maximum=10_000_000),
            _optional_text(payload, "model", maximum=512, scope="paper podcast"),
            _optional_text(payload, "tts_model", maximum=512, scope="paper podcast"),
            _json_text(dict(meta)),
            created_at,
            now,
        ),
    )
    return {"saved": True}


def _paper_podcast_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id, paper_hash, mode, lang, voice = _paper_podcast_key(payload)
    row = session.fetch_one(
        "SELECT status, script_json, file_path, duration_sec, model, "
        "tts_model, meta, created_at, updated_at FROM paper_podcasts "
        "WHERE user_id = ? AND paper_hash = ? AND mode = ? AND lang = ? "
        "AND voice = ?",
        (user_id, paper_hash, mode, lang, voice),
    )
    if row is None:
        return None
    script = _load(row["script_json"])
    meta = _load(row["meta"])
    if not isinstance(script, dict) or not isinstance(meta, dict):
        raise StorageError("database_integrity", "Paper podcast JSON is invalid")
    return {
        "user_id": user_id,
        "paper_hash": paper_hash,
        "mode": mode,
        "lang": lang,
        "voice": voice,
        "status": row["status"] or "",
        "script_json": script,
        "file_path": row["file_path"] or "",
        "duration_sec": float(row["duration_sec"] or 0),
        "model": row["model"] or "",
        "tts_model": row["tts_model"] or "",
        "meta": meta,
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _paper_podcast_mark_interrupted(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    count = session.execute(
        "UPDATE paper_podcasts SET status = 'interrupted', updated_at = ? "
        "WHERE status = 'generating'",
        (_integer(payload, "updated_at", minimum=0),),
    )
    return {"changed": int(count)}

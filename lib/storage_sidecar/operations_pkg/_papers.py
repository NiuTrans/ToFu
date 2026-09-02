"""Research paper, translation, library, podcast, and daily-cost operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import orjson

from lib.log import get_logger
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


def _paper_report_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    lang = _required_text(payload, "lang", 64)
    row = session.fetch_one(
        "SELECT report, model, meta, created_at FROM paper_reports "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (user_id, paper_hash, lang),
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
        "lang": lang,
        "report": row["report"] or "",
        "model": row["model"] or "",
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": int(row["created_at"] or 0),
    }


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
            _optional_text(
                payload, "text", maximum=20_000_000, scope="paper translation"
            ),
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
        "SELECT text, model, created_at FROM paper_translations "
        "WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        (user_id, paper_hash, lang),
    )
    if row is None:
        return None
    return {
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": lang,
        "text": row["text"] or "",
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


def _paper_library_list(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return the authoritative bookshelf projection for the UI/API."""
    user_id = _integer(payload, "user_id", minimum=1)
    paper_id = _optional_text(payload, "id", maximum=256, scope="paper library")
    where = "WHERE user_id = ?"
    args: list[Any] = [user_id]
    if paper_id:
        where += " AND id = ?"
        args.append(paper_id)
    rows = session.fetch_all(
        "SELECT id, title, pdf_url, pdf_filename, arxiv_id, paper_hash, "
        "parsed_text, qa_history, images, babel_cache, page_count, folder_id, "
        "parser_version, created_at, updated_at FROM paper_library "
        + where
        + " ORDER BY updated_at DESC",
        tuple(args),
    )
    result = []
    for row in rows:
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
            "babelCache": _load(row["babel_cache"]) or {},
            "pageCount": int(row["page_count"] or 0),
            "folderId": row["folder_id"] or "",
            "parserVersion": row["parser_version"] or "",
            "createdAt": int(row["created_at"] or 0),
            "updatedAt": int(row["updated_at"] or 0),
        }
        report = (
            session.fetch_one(
                "SELECT 1 AS present FROM paper_reports "
                "WHERE user_id=? AND paper_hash=? LIMIT 1",
                (user_id, item["paperHash"]),
            )
            if item["paperHash"]
            else None
        )
        item["hasReport"] = report is not None
        result.append(item)
    return result


def _paper_library_identity(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    paper_hash = _required_text(payload, "paper_hash", 128)
    row = session.fetch_one(
        "SELECT title, arxiv_id, parsed_text FROM paper_library "
        "WHERE user_id = ? AND paper_hash = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (user_id, paper_hash),
    )
    if row is None:
        return None
    return {
        "title": row["title"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "parsed_text": row["parsed_text"] or "",
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

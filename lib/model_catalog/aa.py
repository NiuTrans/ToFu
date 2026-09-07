"""Artificial Analysis enrichment for canonical Creator/Model identities.

Responsibility: fetch the Artificial Analysis model dataset, cache the public
dataset within an explicit resource budget, and project benchmark scores onto
the caller's canonical Models.  Provider, Offering, Deployment, alias, and
route state are deliberately outside this module.

The caller supplies the effective owner credential.  This module never reads
or persists plaintext credentials and never returns one.  Ordinary reads are
non-blocking: stale/missing data schedules one bounded background refresh;
only explicit user refresh/save actions fetch inline.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from lib.log import get_logger
from lib.runtime_paths import data_root

from ._score_matching import match_model, normalize_name


logger = get_logger(__name__)

# The free-shape endpoint accepts Free, Pro, and Commercial keys and contains
# every headline index used here.  The richer sibling would reject Free keys
# while adding fields this model-only projection deliberately does not consume.
AA_API_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_SOURCE_LABEL = "Artificial Analysis"
AA_SOURCE_URL = "https://artificialanalysis.ai/"

_TTL_ENV = "TOFU_AA_CACHE_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 8.0
_BACKGROUND_MIN_INTERVAL_SECONDS = 60.0
_MAX_DATASET_ROWS = 4096
_MAX_DATASET_PAGES = 24

_lock = threading.Lock()
_memo: dict[str, Any] | None = None
_background: dict[str, Any] = {
    "running": False,
    "last_attempt": 0.0,
    "thread": None,
}


def _cache_path() -> str:
    return os.path.join(data_root(), "aa_index", "models.json")


def _ttl_seconds() -> int:
    try:
        return max(60, int(os.environ.get(_TTL_ENV, "") or _DEFAULT_TTL_SECONDS))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _parse_dataset(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("Artificial Analysis payload is missing its data array")
    parsed: list[dict[str, Any]] = []
    for raw in rows[:_MAX_DATASET_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        evaluations = raw.get("evaluations")
        if not isinstance(evaluations, Mapping):
            continue

        def number(field: str) -> float | None:
            value = evaluations.get(field)
            return float(value) if isinstance(value, (int, float)) else None

        name = str(raw.get("name") or "").strip()
        slug = str(raw.get("slug") or "").strip()
        raw_creator = raw.get("model_creator")
        creator_name = str(
            raw_creator.get("name") if isinstance(raw_creator, Mapping) else ""
        ).strip()
        keys = {key for key in (normalize_name(name), normalize_name(slug)) if key}
        if not keys:
            continue
        parsed.append({
            "aa_name": name,
            "aa_slug": slug,
            "aa_creator": creator_name,
            "intelligence": number("artificial_analysis_intelligence_index"),
            "coding": number("artificial_analysis_coding_index"),
            "agentic": number("artificial_analysis_agentic_index"),
            # Retained for cached/legacy API rows; the current API publishes
            # the Agentic index in this slot instead.
            "math": number("artificial_analysis_math_index"),
            "_keys": keys,
            "_creator_keys": {
                key for key in (normalize_name(creator_name),) if key
            },
        })
    return parsed


def _fetch_dataset(api_key: str, fetcher: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    if not api_key:
        raise PermissionError("Artificial Analysis API key is not configured")
    if fetcher is None:
        from lib.http_client import http_get

        fetcher = http_get
    models: list[dict[str, Any]] = []
    page = 1
    while page <= _MAX_DATASET_PAGES and len(models) < _MAX_DATASET_ROWS:
        url = AA_API_URL if page == 1 else f"{AA_API_URL}?page={page}"
        response = fetcher(
            url,
            timeout=_FETCH_TIMEOUT_SECONDS,
            headers={"x-api-key": api_key},
        )
        response.raise_for_status()
        payload = response.json()
        models.extend(_parse_dataset(payload))
        pagination = payload.get("pagination") if isinstance(payload, Mapping) else None
        if not isinstance(pagination, Mapping) or pagination.get("has_more") is not True:
            break
        reported_page = pagination.get("page")
        page = reported_page + 1 if isinstance(reported_page, int) else page + 1
    return models[:_MAX_DATASET_ROWS]


def _read_disk_cache() -> dict[str, Any] | None:
    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, Mapping) or not isinstance(cached.get("models"), list):
        return None
    models: list[dict[str, Any]] = []
    for raw in cached["models"][:_MAX_DATASET_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["_keys"] = {
            key for key in (
                normalize_name(row.get("aa_name")),
                normalize_name(row.get("aa_slug")),
            ) if key
        }
        row["_creator_keys"] = {
            key for key in (normalize_name(row.get("aa_creator")),) if key
        }
        models.append(row)
    return {"fetched_at": cached.get("fetched_at"), "models": models}


def _write_disk_cache(models: list[dict[str, Any]], fetched_at: float) -> None:
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.tmp"
        public_rows = [
            {
                key: value for key, value in row.items()
                if key not in {"_keys", "_creator_keys"}
            }
            for row in models[:_MAX_DATASET_ROWS]
        ]
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": fetched_at, "models": public_rows}, handle)
        os.replace(temporary_path, path)
    except OSError as exc:
        logger.debug("[ModelIntelligence] AA cache write failed: %s", exc)


def _background_refresh(api_key: str, fetcher: Callable[..., Any] | None) -> None:
    global _memo
    try:
        try:
            models = _fetch_dataset(api_key, fetcher)
        except Exception as exc:
            logger.warning("[ModelIntelligence] AA background refresh failed: %s", exc)
            return
        fetched_at = time.time()
        with _lock:
            _memo = {"fetched_at": fetched_at, "models": models}
        _write_disk_cache(models, fetched_at)
    finally:
        with _lock:
            _background["running"] = False


def _schedule_background_refresh(
    api_key: str,
    now: float,
    fetcher: Callable[..., Any] | None,
) -> None:
    if (
        _background["running"]
        or now - float(_background["last_attempt"]) < _BACKGROUND_MIN_INTERVAL_SECONDS
    ):
        return
    _background["running"] = True
    _background["last_attempt"] = now
    worker = threading.Thread(
        target=_background_refresh,
        args=(api_key, fetcher),
        name="aa-index-refresh",
        daemon=True,
    )
    _background["thread"] = worker
    worker.start()


def _dataset(
    api_key: str,
    *,
    force: bool = False,
    allow_fetch: bool = True,
    fetcher: Callable[..., Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a fresh/stale/no-key/unavailable bounded dataset state."""
    global _memo
    if not api_key:
        return {"status": "no_key", "fetched_at": None, "models": []}
    current_time = time.time() if now is None else now
    with _lock:
        candidates = [row for row in (_memo, _read_disk_cache()) if row]
        freshest = max(
            candidates,
            key=lambda row: float(row.get("fetched_at") or 0),
            default=None,
        )
        if (
            freshest is not None
            and not force
            and current_time - float(freshest.get("fetched_at") or 0) < _ttl_seconds()
        ):
            _memo = freshest
            return {"status": "ok", **freshest}
        if not allow_fetch and not force:
            _schedule_background_refresh(api_key, current_time, fetcher)
            if freshest is not None:
                return {"status": "stale", **freshest}
            return {"status": "unavailable", "fetched_at": None, "models": []}
    try:
        models = _fetch_dataset(api_key, fetcher)
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA refresh failed: %s", exc)
        if freshest is not None:
            return {"status": "stale", **freshest}
        return {"status": "unavailable", "fetched_at": None, "models": []}
    result = {"fetched_at": current_time, "models": models}
    with _lock:
        _memo = result
    _write_disk_cache(models, current_time)
    return {"status": "ok", **result}


def _score_key(model: Mapping[str, Any]) -> str:
    return f"{model.get('creator_id') or ''}::{model.get('model_id') or ''}"


def _block(
    models: Sequence[Mapping[str, Any]],
    dataset: Mapping[str, Any],
    *,
    key_source: str | None,
    key_hint: str,
) -> dict[str, Any]:
    scores: dict[str, Any] = {}
    source_rows = dataset.get("models")
    rows = source_rows if isinstance(source_rows, list) else []
    for model in models:
        matched = match_model(model, rows)
        if matched is None:
            continue
        scores[_score_key(model)] = {
            "intelligence": matched.get("intelligence"),
            "coding": matched.get("coding"),
            "agentic": matched.get("agentic"),
            "math": matched.get("math"),
            "aa_name": matched.get("aa_name") or "",
            "aa_slug": matched.get("aa_slug") or "",
        }
    return {
        "status": dataset.get("status", "unavailable"),
        "source": AA_SOURCE_LABEL,
        "source_url": AA_SOURCE_URL,
        "fetched_at": dataset.get("fetched_at"),
        "key_source": key_source,
        "key_hint": key_hint,
        "scores": scores,
    }


def aa_block_for_models(
    models: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    key_source: str | None,
    key_hint: str = "",
) -> dict[str, Any]:
    """Return the non-blocking AA projection for canonical Models."""
    try:
        return _block(
            models,
            _dataset(api_key, allow_fetch=False),
            key_source=key_source,
            key_hint=key_hint,
        )
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            key_source=key_source,
            key_hint=key_hint,
        )


def refresh_scores(
    models: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    key_source: str | None,
    key_hint: str = "",
) -> dict[str, Any]:
    """Force-refresh AA scores after an explicit user action."""
    try:
        return _block(
            models,
            _dataset(api_key, force=True),
            key_source=key_source,
            key_hint=key_hint,
        )
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA forced projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            key_source=key_source,
            key_hint=key_hint,
        )


__all__ = [
    "AA_API_URL",
    "aa_block_for_models",
    "refresh_scores",
]

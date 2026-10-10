"""OpenRouter pricing enrichment for canonical Creator/Model identities.

Responsibility: fetch the public OpenRouter model catalog, cache it within an
explicit resource budget, and project per-million-token USD list prices onto
the caller's canonical Models as a REPORT.  Writing fills into the
model-routing authority is the caller's explicit, policy-guarded action.

Pricing policy (owner decision, 2026-09-05):

* A Model with a positive ``list_pricing`` keeps it — official list prices
  stay authoritative.  OpenRouter rows for priced Models are a cross-check
  only and surface in the drift report, never as a write source.
* An unpriced Model whose Creator has official-direct coverage (any sibling
  resolves in the maintained ``lib.pricing`` tables) is reported for manual
  official lookup — OpenRouter is never the source for those vendors.
* An unpriced Model with no official coverage is fillable when its
  OpenRouter row has a positive price.  All-zero OpenRouter rows (``:free``
  variants, promos) never become a $0 list price: unknown stays unknown
  instead of impersonating an official free tier.

The dataset endpoint is public and keyless.  Ordinary reads are non-blocking:
stale/missing data schedules one bounded background refresh; only explicit
refresh actions fetch inline.
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


logger = get_logger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_SOURCE_LABEL = "OpenRouter"
OPENROUTER_SOURCE_URL = "https://openrouter.ai/"

_TTL_ENV = "TOFU_OPENROUTER_CACHE_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 12.0
_BACKGROUND_MIN_INTERVAL_SECONDS = 60.0
_MAX_DATASET_ROWS = 2048

# Relative blended-cost gap above which a priced Model and its OpenRouter row
# disagree enough to report as drift (cross-check, never auto-rewritten).
_DRIFT_TOLERANCE = 0.25

_lock = threading.Lock()
_memo: dict[str, Any] | None = None
_background: dict[str, Any] = {
    "running": False,
    "last_attempt": 0.0,
    "thread": None,
}


def _cache_path() -> str:
    return os.path.join(data_root(), "openrouter", "models.json")


def _ttl_seconds() -> int:
    try:
        return max(60, int(os.environ.get(_TTL_ENV, "") or _DEFAULT_TTL_SECONDS))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _per_million(value: object) -> float:
    try:
        amount = float(value)  # per-token USD string
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    return round(amount * 1e6, 6)


def _parse_dataset(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("OpenRouter payload is missing its data array")
    parsed: list[dict[str, Any]] = []
    for raw in rows[:_MAX_DATASET_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        or_id = str(raw.get("id") or "").strip().lower()
        # "~"-prefixed rows are alias redirects; a ":" suffix marks a variant
        # row (:free / :batch / :extended) whose price never represents the
        # canonical interactive endpoint.
        if not or_id or or_id.startswith("~") or ":" in or_id:
            continue
        author, _, slug = or_id.partition("/")
        if not author or not slug:
            continue
        pricing = raw.get("pricing")
        if not isinstance(pricing, Mapping):
            continue
        input_price = _per_million(pricing.get("prompt"))
        output_price = _per_million(pricing.get("completion"))
        cache_read = pricing.get("input_cache_read")
        parsed.append({
            "or_id": or_id,
            "or_author": author,
            "or_slug": slug,
            "input": input_price,
            "output": output_price,
            "cache_read": (
                _per_million(cache_read) if cache_read is not None else None
            ),
            "positive": input_price > 0 or output_price > 0,
        })
    return parsed


def _fetch_dataset(fetcher: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    if fetcher is None:
        from lib.http_client import http_get

        fetcher = http_get
    response = fetcher(OPENROUTER_MODELS_URL, timeout=_FETCH_TIMEOUT_SECONDS)
    response.raise_for_status()
    return _parse_dataset(response.json())


def _read_disk_cache() -> dict[str, Any] | None:
    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, Mapping) or not isinstance(cached.get("models"), list):
        return None
    models = [
        dict(row) for row in cached["models"][:_MAX_DATASET_ROWS]
        if isinstance(row, Mapping)
    ]
    return {"fetched_at": cached.get("fetched_at"), "models": models}


def _write_disk_cache(models: list[dict[str, Any]], fetched_at: float) -> None:
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"fetched_at": fetched_at, "models": models[:_MAX_DATASET_ROWS]},
                handle,
            )
        os.replace(temporary_path, path)
    except OSError as exc:
        logger.debug("[ModelIntelligence] OpenRouter cache write failed: %s", exc)


def _background_refresh(fetcher: Callable[..., Any] | None) -> None:
    global _memo
    try:
        try:
            models = _fetch_dataset(fetcher)
        except Exception as exc:
            logger.warning(
                "[ModelIntelligence] OpenRouter background refresh failed: %s", exc)
            return
        fetched_at = time.time()
        with _lock:
            _memo = {"fetched_at": fetched_at, "models": models}
        _write_disk_cache(models, fetched_at)
    finally:
        with _lock:
            _background["running"] = False


def _schedule_background_refresh(
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
        args=(fetcher,),
        name="openrouter-pricing-refresh",
        daemon=True,
    )
    _background["thread"] = worker
    worker.start()


def _dataset(
    *,
    force: bool = False,
    allow_fetch: bool = True,
    fetcher: Callable[..., Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a fresh/stale/unavailable bounded dataset state."""
    global _memo
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
            _schedule_background_refresh(current_time, fetcher)
            if freshest is not None:
                return {"status": "stale", **freshest}
            return {"status": "unavailable", "fetched_at": None, "models": []}
    try:
        models = _fetch_dataset(fetcher)
    except Exception as exc:
        logger.warning("[ModelIntelligence] OpenRouter refresh failed: %s", exc)
        if freshest is not None:
            return {"status": "stale", **freshest}
        return {"status": "unavailable", "fetched_at": None, "models": []}
    result = {"fetched_at": current_time, "models": models}
    with _lock:
        _memo = result
    _write_disk_cache(models, current_time)
    return {"status": "ok", **result}


def _model_key(model: Mapping[str, Any]) -> str:
    return f"{model.get('creator_id') or ''}::{model.get('model_id') or ''}"


def _match_candidates(creator_id: object, model_id: object) -> list[str]:
    creator = str(creator_id or "").strip().lower()
    model = str(model_id or "").strip().lower()
    if not creator or not model:
        return []
    leaf = model.rsplit("/", 1)[-1]
    candidates = []
    if "/" not in model:
        candidates.append(f"{creator}/{model}")
    if leaf != model:
        # Namespaced leftovers ("thinkingmachines/thinkingmachines/inkling")
        # fold to the provider/slug shape OpenRouter publishes.
        candidates.append(f"{creator}/{leaf}")
    return candidates


def _positive_list_pricing(pricing: object) -> bool:
    if not isinstance(pricing, Mapping):
        return False
    amounts = []
    for field in ("input", "output", "cache_read", "cache_write"):
        try:
            amounts.append(float(pricing.get(field) or 0))
        except (TypeError, ValueError):
            continue
    return any(amount > 0 for amount in amounts)


def _blended(pricing: Mapping[str, Any]) -> float | None:
    try:
        input_price = float(pricing.get("input") or 0)
        output_price = float(pricing.get("output") or 0)
    except (TypeError, ValueError):
        return None
    if input_price <= 0 and output_price <= 0:
        return None
    return (3 * input_price + output_price) / 4


def _official_coverage(
    models: Sequence[Mapping[str, Any]],
    official_lookup: Callable[[str], Any],
) -> set[str]:
    """Creators with any Model resolving in the maintained pricing tables."""
    covered: set[str] = set()
    for model in models:
        creator = str(model.get("creator_id") or "")
        if not creator or creator in covered:
            continue
        model_id = str(model.get("model_id") or "")
        leaf = model_id.rsplit("/", 1)[-1]
        try:
            if model_id and official_lookup(leaf) is not None:
                covered.add(creator)
        except Exception as exc:
            logger.debug('official coverage lookup failed for %s: %s',
                         model_id, type(exc).__name__)
            continue
    return covered


def _block(
    models: Sequence[Mapping[str, Any]],
    dataset: Mapping[str, Any],
    *,
    official_lookup: Callable[[str], Any],
) -> dict[str, Any]:
    index: dict[str, Mapping[str, Any]] = {}
    source_rows = dataset.get("models")
    for row in source_rows if isinstance(source_rows, list) else []:
        if isinstance(row, Mapping) and row.get("or_id"):
            index[str(row["or_id"])] = row

    covered = _official_coverage(models, official_lookup)
    fillable: dict[str, Any] = {}
    official_covered: list[str] = []
    openrouter_free: list[str] = []
    unmatched: list[str] = []
    drift: dict[str, Any] = {}
    priced = 0

    for model in models:
        key = _model_key(model)
        match = next(
            (
                index[candidate]
                for candidate in _match_candidates(
                    model.get("creator_id"), model.get("model_id"))
                if candidate in index
            ),
            None,
        )
        list_pricing = model.get("list_pricing")
        if _positive_list_pricing(list_pricing):
            priced += 1
            if (
                match is not None
                and match.get("positive")
                and isinstance(list_pricing, Mapping)
                and list_pricing.get("currency") == "USD"
            ):
                listed = _blended(list_pricing)
                openrouter = _blended(match)
                if listed and openrouter and (
                    abs(listed - openrouter) / max(listed, openrouter)
                    > _DRIFT_TOLERANCE
                ):
                    drift[key] = {
                        "or_id": match["or_id"],
                        "list_pricing": dict(list_pricing),
                        "openrouter": {
                            "input": match["input"],
                            "output": match["output"],
                            "currency": "USD",
                            "unit": "per_million_tokens",
                        },
                    }
            continue
        if match is None:
            unmatched.append(key)
            continue
        if not match.get("positive"):
            openrouter_free.append(key)
            continue
        if str(model.get("creator_id") or "") in covered:
            official_covered.append(key)
            continue
        pricing: dict[str, Any] = {
            "input": match["input"],
            "output": match["output"],
            "currency": "USD",
            "unit": "per_million_tokens",
        }
        if match.get("cache_read") is not None:
            pricing["cache_read"] = match["cache_read"]
        fillable[key] = {"or_id": match["or_id"], "pricing": pricing}

    return {
        "status": dataset.get("status", "unavailable"),
        "source": OPENROUTER_SOURCE_LABEL,
        "source_url": OPENROUTER_SOURCE_URL,
        "fetched_at": dataset.get("fetched_at"),
        "summary": {
            "models": len(models),
            "priced": priced,
            "fillable": len(fillable),
            "official_covered": len(official_covered),
            "openrouter_free": len(openrouter_free),
            "unmatched": len(unmatched),
            "drift": len(drift),
        },
        "fillable": fillable,
        "official_covered": official_covered,
        "openrouter_free": openrouter_free,
        "unmatched": unmatched,
        "drift": drift,
    }


def _default_official_lookup() -> Callable[[str], Any]:
    from lib.pricing import pricing_for_model

    return pricing_for_model


def pricing_block_for_models(
    models: Sequence[Mapping[str, Any]],
    *,
    official_lookup: Callable[[str], Any] | None = None,
    fetcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return the non-blocking OpenRouter pricing projection for Models."""
    lookup = official_lookup or _default_official_lookup()
    try:
        return _block(
            models,
            _dataset(allow_fetch=False, fetcher=fetcher),
            official_lookup=lookup,
        )
    except Exception as exc:
        logger.warning(
            "[ModelIntelligence] OpenRouter pricing projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            official_lookup=lookup,
        )


def refresh_pricing_block(
    models: Sequence[Mapping[str, Any]],
    *,
    official_lookup: Callable[[str], Any] | None = None,
    fetcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Force-refresh OpenRouter pricing after an explicit user action."""
    lookup = official_lookup or _default_official_lookup()
    try:
        return _block(
            models,
            _dataset(force=True, fetcher=fetcher),
            official_lookup=lookup,
        )
    except Exception as exc:
        logger.warning(
            "[ModelIntelligence] OpenRouter forced projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            official_lookup=lookup,
        )


def fill_document_list_pricing(
    document: dict[str, Any],
    fillable: Mapping[str, Any],
) -> list[str]:
    """Write policy-approved fills into a working authority document.

    Only Models still lacking a positive ``list_pricing`` are written (a CAS
    retry re-checks under the freshest document).  A fill whose price is not
    positive is skipped defensively — a $0 list price would impersonate an
    official free tier.  Returns the applied Creator/Model keys.
    """
    applied: list[str] = []
    for row in document.get("models") or []:
        if not isinstance(row, dict):
            continue
        key = _model_key(row)
        fill = fillable.get(key)
        if not isinstance(fill, Mapping):
            continue
        if _positive_list_pricing(row.get("list_pricing")):
            continue
        pricing = fill.get("pricing")
        if not isinstance(pricing, Mapping):
            continue
        candidate = {
            "input": float(pricing.get("input") or 0),
            "output": float(pricing.get("output") or 0),
            "currency": "USD",
            "unit": "per_million_tokens",
        }
        if candidate["input"] <= 0 and candidate["output"] <= 0:
            continue
        if pricing.get("cache_read") is not None:
            candidate["cache_read"] = float(pricing["cache_read"])
        row["list_pricing"] = candidate
        applied.append(key)
    return applied


__all__ = [
    "OPENROUTER_MODELS_URL",
    "fill_document_list_pricing",
    "pricing_block_for_models",
    "refresh_pricing_block",
]

"""Detect earned Codex usage-limit reset credits without scraping the TUI.

OpenAI's Codex ``/usage`` screen formats a structured backend field:
``rate_limit_reset_credits.available_count``.  This module owns that private
control-plane read for Tofu's direct Codex OAuth account.

Public entry points
-------------------
``codex_usage_reset_status``
    Non-blocking status projection used by the authenticated OAuth route.  A
    missing or stale entry starts one daemon refresh and returns immediately.
``refresh_codex_usage_reset``
    Synchronous refresh used by the daemon and focused tests.
``clear_codex_usage_reset_cache``
    Lifecycle cleanup for logout/account replacement.

The cache is reconstructible, private (0600), owner+account scoped, bounded to
16 entries, and contains no token or raw account identifier.  A failed or
shape-drifted request is always ``unknown``; it is never converted to zero.
Ordinary quota-window ``resets_at`` timestamps are deliberately ignored because
they describe scheduled rolling-window rollover, not an earned reset credit.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import datetime
from typing import Any

from lib.config_dir import config_path
from lib.json_store import locked_path, read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

CODEX_USAGE_RESET_TTL_S = 30 * 60
CODEX_USAGE_RESET_FAILURE_RETRY_S = 60
CODEX_USAGE_RESET_TIMEOUT_S = 5
CODEX_USAGE_RESET_DETAILS_TIMEOUT_S = 5

_CACHE_SCHEMA_VERSION = 1
_MAX_CACHE_ENTRIES = 16
_MAX_CONCURRENT_REFRESHES = 2
_REFRESH_LOCK_STRIPES = 16
_CAPACITY_RETRY_S = 5
_MAX_AVAILABLE_COUNT = 100
_MAX_DETAILS = 16
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TITLE_CHARS = 160
_MAX_DESCRIPTION_CHARS = 400
_HEX_RE = re.compile(r"^[0-9a-f]+$")

_state_lock = threading.Lock()
_refreshing_keys: set[str] = set()


def _cache_path() -> str:
    return config_path("oauth", "codex_usage_reset_cache.json")


def _fingerprint(namespace: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(
        f"tofu:{namespace}:v1\0{value}".encode("utf-8")
    ).hexdigest()
    return digest[:length]


def _identity(stored: dict | None, owner_user_id: str = "") -> dict[str, str] | None:
    account_id = str((stored or {}).get("account_id") or "").strip()
    if not account_id:
        return None
    owner = str(owner_user_id or "legacy")
    account_fingerprint = _fingerprint("codex-account", account_id)
    owner_fingerprint = _fingerprint("owner", owner)
    cache_key = _fingerprint(
        "codex-usage-reset-cache", f"{owner_fingerprint}:{account_fingerprint}"
    )
    return {
        "cache_key": cache_key,
        "account_fingerprint": account_fingerprint,
        "owner_fingerprint": owner_fingerprint,
    }


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _integer(value: Any, *, low: int = 0, high: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < low or (high is not None and value > high):
        return None
    return value


def _timestamp(value: Any) -> int | None:
    parsed = _integer(value, low=1)
    return parsed


def _normalise_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cache_key = str(raw.get("cache_key") or "")
    account_fingerprint = str(raw.get("account_fingerprint") or "")
    owner_fingerprint = str(raw.get("owner_fingerprint") or "")
    if not (
        len(cache_key) == 24
        and len(account_fingerprint) == 24
        and len(owner_fingerprint) == 24
        and _HEX_RE.fullmatch(cache_key)
        and _HEX_RE.fullmatch(account_fingerprint)
        and _HEX_RE.fullmatch(owner_fingerprint)
    ):
        return None

    state = str(raw.get("state") or "")
    if state not in {"available", "none", "unknown"}:
        return None
    captured_at = _timestamp(raw.get("captured_at"))
    if captured_at is None:
        return None

    available_count = raw.get("available_count")
    if state == "available":
        available_count = _integer(
            available_count, low=1, high=_MAX_AVAILABLE_COUNT
        )
        if available_count is None:
            return None
    elif state == "none":
        available_count = _integer(
            available_count, low=0, high=_MAX_AVAILABLE_COUNT)
        if available_count != 0:
            return None
    else:
        available_count = None

    entry: dict[str, Any] = {
        "cache_key": cache_key,
        "account_fingerprint": account_fingerprint,
        "owner_fingerprint": owner_fingerprint,
        "state": state,
        "available_count": available_count,
        "captured_at": captured_at,
    }
    for key in (
        "availability_started_at",
        "expires_at",
        "refresh_failed_at",
        "next_retry_at",
    ):
        value = _timestamp(raw.get(key))
        if value is not None:
            entry[key] = value
    notification_key = str(raw.get("notification_key") or "")
    credit_set_fingerprint = str(raw.get("credit_set_fingerprint") or "")
    if notification_key and len(notification_key) == 24 and _HEX_RE.fullmatch(
        notification_key
    ):
        entry["notification_key"] = notification_key
    if credit_set_fingerprint and len(
        credit_set_fingerprint
    ) == 24 and _HEX_RE.fullmatch(credit_set_fingerprint):
        entry["credit_set_fingerprint"] = credit_set_fingerprint
    reason = str(raw.get("reason") or "")
    if reason in {"not_reported", "refresh_failed"}:
        entry["reason"] = reason
    title = _bounded_text(raw.get("title"), _MAX_TITLE_CHARS)
    description = _bounded_text(raw.get("description"), _MAX_DESCRIPTION_CHARS)
    if title:
        entry["title"] = title
    if description:
        entry["description"] = description
    return entry


def _read_entries() -> dict[str, dict[str, Any]]:
    raw = read_json(_cache_path(), default={}) or {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return {}
    rows = raw.get("entries")
    if not isinstance(rows, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for row in rows[-(_MAX_CACHE_ENTRIES * 2) :]:
        entry = _normalise_entry(row)
        if entry is not None:
            entries[entry["cache_key"]] = entry
    return entries


def _entry_recency(entry: dict[str, Any]) -> int:
    return max(
        int(entry.get("captured_at") or 0),
        int(entry.get("refresh_failed_at") or 0),
    )


def _write_entries(entries: dict[str, dict[str, Any]]) -> None:
    rows = sorted(entries.values(), key=_entry_recency)[-_MAX_CACHE_ENTRIES:]
    write_json_atomic(
        _cache_path(),
        {"schema_version": _CACHE_SCHEMA_VERSION, "entries": rows},
        mode=0o600,
    )


def _parse_usage_available_count(payload: Any) -> int | None:
    """Return the structured earned-credit count, never a quota reset time."""
    if not isinstance(payload, dict):
        return None
    summary = payload.get("rate_limit_reset_credits")
    if not isinstance(summary, dict):
        return None
    return _integer(
        summary.get("available_count"), low=0, high=_MAX_AVAILABLE_COUNT
    )


def _parse_iso_timestamp(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = int(parsed.astimezone(datetime.timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


def _parse_credit_details(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("credits"), list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload["credits"]:
        if len(rows) >= _MAX_DETAILS:
            break
        if not isinstance(raw, dict):
            continue
        credit_id = _bounded_text(raw.get("id"), 256)
        if not credit_id or credit_id in seen:
            continue
        if str(raw.get("status") or "").strip().lower() != "available":
            continue
        seen.add(credit_id)
        row: dict[str, Any] = {"id": credit_id}
        expires_at = _parse_iso_timestamp(raw.get("expires_at"))
        if expires_at is not None:
            row["expires_at"] = expires_at
        title = _bounded_text(raw.get("title"), _MAX_TITLE_CHARS)
        description = _bounded_text(
            raw.get("description"), _MAX_DESCRIPTION_CHARS
        )
        if title:
            row["title"] = title
        if description:
            row["description"] = description
        rows.append(row)
    rows.sort(key=lambda row: (row.get("expires_at") is None, row.get("expires_at", 0), row["id"]))
    return rows


def _response_json(response: Any, *, label: str) -> dict[str, Any]:
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(f"Codex {label} request failed with HTTP {status}")
    content = getattr(response, "content", b"") or b""
    if len(content) > _MAX_RESPONSE_BYTES:
        raise RuntimeError(f"Codex {label} response exceeded 1 MiB")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Codex {label} response was not an object")
    return payload


def _authenticated_get(
    url: str, *, timeout: float, user_id: str, affinity_key: str,
) -> dict[str, Any]:
    from lib.oauth.outbound import resolve_oauth_request

    # Control-plane GETs still use the sole Codex identity owner.  A stable,
    # hashed conversation seed prevents random session/thread headers on every
    # poll without exposing the owner/account cache key upstream.
    token, headers, _body = resolve_oauth_request(
        "codex", {"_conv_id": f"codex-usage-reset:{affinity_key}"}, None,
        user_id=user_id,
    )
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/json"

    from lib.desktop import egress as desktop_egress

    route = desktop_egress.route_request(url, user_id=user_id)
    if route == "direct":
        from lib.http_client import http_get

        response = http_get(url, headers=headers, timeout=timeout)
    else:
        response = desktop_egress.egress_http(
            url,
            method="GET",
            headers=headers,
            timeout=timeout,
            user_id=user_id,
            agent_id=route,
        )
    return _response_json(response, label="usage" if url.endswith("/usage") else "reset-credit details")


def _refresh_lock_path(cache_key: str) -> str:
    """Return one of finitely many cross-process refresh lock sidecars.

    A sidecar per historical account can never be safely deleted while another
    process may hold it. Hash striping therefore keeps both lock files and
    ``json_store``'s process-local lock registry bounded while preserving
    same-identity singleflight across processes.
    """
    stripe = int(cache_key[:8], 16) % _REFRESH_LOCK_STRIPES
    return f"{_cache_path()}.refresh.{stripe:02x}"


def _codex_usage_url() -> str:
    from lib.oauth.codex import CODEX_OAUTH_CONFIG

    return f"{CODEX_OAUTH_CONFIG['account_api_base'].rstrip('/')}/usage"


def _details_url() -> str:
    from lib.oauth.codex import CODEX_OAUTH_CONFIG

    return (
        f"{CODEX_OAUTH_CONFIG['account_api_base'].rstrip('/')}"
        "/rate-limit-reset-credits"
    )


def _credit_set_fingerprint(
    account_fingerprint: str, details: list[dict[str, Any]]
) -> str:
    ids = sorted({row["id"] for row in details if row.get("id")})
    if not ids:
        return ""
    return _fingerprint(
        "codex-reset-credit-set", f"{account_fingerprint}\0" + "\0".join(ids)
    )


def _available_entry(
    identity: dict[str, str],
    available_count: int,
    details: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    now: int,
) -> dict[str, Any]:
    current_credit_set = _credit_set_fingerprint(
        identity["account_fingerprint"], details
    )
    previous_available = bool(
        previous
        and previous.get("state") == "available"
        and previous.get("notification_key")
    )
    previous_credit_set = str((previous or {}).get("credit_set_fingerprint") or "")

    reuse_previous = False
    if previous_available:
        if current_credit_set:
            # The first successful detail read after a count-only read adopts
            # the stable set fingerprint without re-notifying the same credit.
            reuse_previous = not previous_credit_set or previous_credit_set == current_credit_set
        else:
            reuse_previous = previous.get("available_count") == available_count

    if reuse_previous:
        notification_key = str(previous["notification_key"])
        availability_started_at = int(
            previous.get("availability_started_at") or now
        )
    elif current_credit_set:
        notification_key = current_credit_set
        availability_started_at = now
    else:
        notification_key = _fingerprint(
            "codex-reset-availability-epoch",
            f"{identity['account_fingerprint']}:{available_count}:{now}",
        )
        availability_started_at = now

    entry: dict[str, Any] = {
        **identity,
        "state": "available",
        "available_count": available_count,
        "captured_at": now,
        "availability_started_at": availability_started_at,
        "notification_key": notification_key,
    }
    if current_credit_set:
        entry["credit_set_fingerprint"] = current_credit_set
    elif previous_credit_set and reuse_previous:
        entry["credit_set_fingerprint"] = previous_credit_set
    if details:
        first = details[0]
        for key in ("title", "description", "expires_at"):
            if first.get(key):
                entry[key] = first[key]
    return entry


def _unknown_entry(
    identity: dict[str, str], *, reason: str, now: int, retry_at: int
) -> dict[str, Any]:
    return {
        **identity,
        "state": "unknown",
        "available_count": None,
        "captured_at": now,
        "reason": reason,
        "next_retry_at": retry_at,
    }


def _needs_refresh(entry: dict[str, Any] | None, now: int) -> bool:
    if entry is None:
        return True
    retry_at = int(entry.get("next_retry_at") or 0)
    if retry_at > now:
        return False
    if entry.get("reason") in {"not_reported", "refresh_failed"}:
        return True
    if entry.get("refresh_failed_at"):
        return True
    return now - int(entry.get("captured_at") or 0) > CODEX_USAGE_RESET_TTL_S


def _project(
    entry: dict[str, Any] | None,
    *,
    now: int,
    refreshing: bool,
    missing_reason: str = "not_checked",
) -> dict[str, Any]:
    if entry is None:
        return {
            "state": "unknown",
            "available_count": None,
            "source": "codex_usage_api",
            "captured_at": None,
            "stale": False,
            "refreshing": refreshing,
            "reason": missing_reason,
        }
    captured_at = int(entry.get("captured_at") or 0)
    refresh_failed_at = int(entry.get("refresh_failed_at") or 0)
    stale = entry.get("state") in {"available", "none"} and (
        now - captured_at > CODEX_USAGE_RESET_TTL_S
        or refresh_failed_at > captured_at
    )
    out: dict[str, Any] = {
        "state": entry["state"],
        "available_count": entry.get("available_count"),
        "source": "codex_usage_api",
        "captured_at": captured_at,
        "age_seconds": max(0, now - captured_at),
        "stale": stale,
        "refreshing": refreshing,
    }
    for key in (
        "notification_key",
        "title",
        "description",
        "expires_at",
        "reason",
    ):
        if entry.get(key) not in (None, ""):
            out[key] = entry[key]
    retry_at = int(entry.get("next_retry_at") or 0)
    if retry_at > now:
        out["retry_after_seconds"] = retry_at - now
    return out


def refresh_codex_usage_reset(
    *, user_id: str = "", force: bool = False, now: float | None = None
) -> dict[str, Any]:
    """Refresh one owner/account snapshot synchronously.

    Callers normally use :func:`codex_usage_reset_status`; this function is
    public for deterministic tests and explicit operator diagnostics.
    """
    from lib.oauth.token_store import load_token

    current_time = int(time.time() if now is None else now)
    stored = load_token("codex") or {}
    identity = _identity(stored, user_id)
    if not stored.get("access_token"):
        return _project(
            None,
            now=current_time,
            refreshing=False,
            missing_reason="not_authenticated",
        )
    if identity is None:
        return _project(
            None,
            now=current_time,
            refreshing=False,
            missing_reason="account_identity_unavailable",
        )

    # Merge the same owner/account across serving processes, but never hold a
    # global cache lock over network I/O. Different owners may refresh in
    # parallel; the short read-modify-write section below preserves both rows.
    refresh_lock_path = _refresh_lock_path(identity["cache_key"])
    with locked_path(refresh_lock_path):
        previous = _read_entries().get(identity["cache_key"])
        if not force and not _needs_refresh(previous, current_time):
            return _project(previous, now=current_time, refreshing=False)
        try:
            usage_payload = _authenticated_get(
                _codex_usage_url(), timeout=CODEX_USAGE_RESET_TIMEOUT_S,
                user_id=user_id, affinity_key=identity["cache_key"],
            )
            available_count = _parse_usage_available_count(usage_payload)
            if available_count is None:
                entry = _unknown_entry(
                    identity,
                    reason="not_reported",
                    now=current_time,
                    retry_at=current_time + CODEX_USAGE_RESET_TTL_S,
                )
            elif available_count == 0:
                entry = {
                    **identity,
                    "state": "none",
                    "available_count": 0,
                    "captured_at": current_time,
                }
            else:
                details: list[dict[str, Any]] = []
                try:
                    details = _parse_credit_details(
                        _authenticated_get(
                            _details_url(),
                            timeout=CODEX_USAGE_RESET_DETAILS_TIMEOUT_S,
                            user_id=user_id,
                            affinity_key=identity["cache_key"],
                        )
                    )
                except Exception as detail_error:
                    # Count-only data is sufficient for detection.  Details
                    # enrich copy/deduplication but never suppress a true offer.
                    logger.debug(
                        "[CodexUsageReset] detail fetch failed; using count only: %s",
                        detail_error,
                    )
                entry = _available_entry(
                    identity, available_count, details, previous, current_time
                )
            latest_identity = _identity(load_token("codex") or {}, user_id)
            if (latest_identity is None
                    or latest_identity["cache_key"] != identity["cache_key"]):
                logger.info(
                    "[CodexUsageReset] account changed during refresh; "
                    "discarding the obsolete observation")
                return _project(
                    None, now=current_time, refreshing=False,
                    missing_reason="account_changed")
        except Exception as error:
            logger.warning(
                "[CodexUsageReset] refresh failed; preserving last good state: %s",
                error,
            )
            if previous and previous.get("state") in {"available", "none"}:
                entry = dict(previous)
                entry["refresh_failed_at"] = current_time
                entry["next_retry_at"] = (
                    current_time + CODEX_USAGE_RESET_FAILURE_RETRY_S
                )
            else:
                entry = _unknown_entry(
                    identity,
                    reason="refresh_failed",
                    now=current_time,
                    retry_at=current_time + CODEX_USAGE_RESET_FAILURE_RETRY_S,
                )
        with locked_path(_cache_path() + ".write"):
            latest_entries = _read_entries()
            latest_entries[identity["cache_key"]] = entry
            _write_entries(latest_entries)
        return _project(entry, now=current_time, refreshing=False)


def _is_refreshing(cache_key: str) -> bool:
    with _state_lock:
        return cache_key in _refreshing_keys


def trigger_codex_usage_reset_refresh(*, user_id: str = "") -> bool:
    """Start one process-local daemon refresh for the current owner/account."""
    from lib.oauth.token_store import load_token

    identity = _identity(load_token("codex") or {}, user_id)
    if identity is None:
        return False
    cache_key = identity["cache_key"]
    with _state_lock:
        if cache_key in _refreshing_keys:
            return False
        if len(_refreshing_keys) >= _MAX_CONCURRENT_REFRESHES:
            logger.debug(
                '[CodexUsageReset] refresh capacity reached; deferring %s',
                cache_key[:8])
            return False
        _refreshing_keys.add(cache_key)

    def _run() -> None:
        try:
            refresh_codex_usage_reset(user_id=user_id, force=False)
        finally:
            with _state_lock:
                _refreshing_keys.discard(cache_key)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"codex-usage-reset-{cache_key[:8]}",
    ).start()
    return True


def codex_usage_reset_status(
    *, user_id: str = "", refresh_if_stale: bool = True, now: float | None = None
) -> dict[str, Any]:
    """Return a non-blocking ``available|none|unknown`` status projection."""
    from lib.oauth.token_store import load_token

    current_time = int(time.time() if now is None else now)
    stored = load_token("codex") or {}
    if not stored.get("access_token"):
        return _project(
            None,
            now=current_time,
            refreshing=False,
            missing_reason="not_authenticated",
        )
    identity = _identity(stored, user_id)
    if identity is None:
        return _project(
            None,
            now=current_time,
            refreshing=False,
            missing_reason="account_identity_unavailable",
        )
    entry = _read_entries().get(identity["cache_key"])
    latest_identity = _identity(load_token("codex") or {}, user_id)
    if (latest_identity is None
            or latest_identity["cache_key"] != identity["cache_key"]):
        return _project(
            None, now=current_time, refreshing=False,
            missing_reason="account_changed")
    refresh_needed = refresh_if_stale and _needs_refresh(entry, current_time)
    if refresh_needed:
        trigger_codex_usage_reset_refresh(user_id=user_id)
    projected = _project(
        entry,
        now=current_time,
        refreshing=_is_refreshing(identity["cache_key"]),
    )
    if (refresh_needed and not projected["refreshing"]
            and "retry_after_seconds" not in projected):
        projected["retry_after_seconds"] = _CAPACITY_RETRY_S
    return projected


def clear_codex_usage_reset_cache(
    *, account_id: str = "", owner_user_id: str | None = None,
    clear_all: bool = False,
) -> int:
    """Remove explicitly selected rows; clearing everything requires opt-in."""
    if not str(account_id or "").strip() and owner_user_id is None and not clear_all:
        return 0
    account_fingerprint = (
        _fingerprint("codex-account", str(account_id).strip())
        if str(account_id or "").strip()
        else ""
    )
    owner_fingerprint = (
        _fingerprint("owner", str(owner_user_id or "legacy"))
        if owner_user_id is not None
        else ""
    )
    with locked_path(_cache_path() + ".write"):
        entries = _read_entries()
        kept: dict[str, dict[str, Any]] = {}
        removed = 0
        for key, entry in entries.items():
            account_matches = (
                not account_fingerprint
                or entry.get("account_fingerprint") == account_fingerprint
            )
            owner_matches = (
                not owner_fingerprint
                or entry.get("owner_fingerprint") == owner_fingerprint
            )
            if account_matches and owner_matches:
                removed += 1
            else:
                kept[key] = entry
        if removed:
            _write_entries(kept)
    return removed


def _reset_codex_usage_state_for_tests() -> None:
    with _state_lock:
        _refreshing_keys.clear()


__all__ = [
    "CODEX_USAGE_RESET_TTL_S",
    "CODEX_USAGE_RESET_FAILURE_RETRY_S",
    "codex_usage_reset_status",
    "refresh_codex_usage_reset",
    "trigger_codex_usage_reset_refresh",
    "clear_codex_usage_reset_cache",
]

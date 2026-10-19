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
``consume_codex_usage_reset``
    The explicitly confirmed, idempotent redemption command.  One banked
    credit is spent only through this function; its ``redeem_request_id``
    is persisted before the upstream POST so an ambiguous retry reuses the
    same idempotency key instead of risking a second spend.
``clear_codex_usage_reset_cache``
    Lifecycle cleanup for logout/account replacement.

The private (0600), owner/account-scoped store retains complete observations
and pending idempotency evidence without a row count quota. Expired unprotected
observations reclaim on writes; pending records survive until explicit resolution
or scoped lifecycle cleanup. No token or raw account identifier is stored.  A failed or
shape-drifted request is always ``unknown``; it is never converted to zero.
Ordinary quota-window ``resets_at`` timestamps are deliberately ignored because
they describe scheduled rolling-window rollover, not an earned reset credit.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import datetime
import uuid
from typing import Any

from lib.config_dir import config_path
from lib.json_store import JsonStoreReadError, locked_path, read_json, write_json_atomic
from lib.log import get_logger
from lib.oauth.identity_flight import identity_flight

logger = get_logger(__name__)

CODEX_USAGE_RESET_TTL_S = 30 * 60
CODEX_USAGE_RESET_FAILURE_RETRY_S = 60
# The same /usage GET also carries the structured ``rate_limit`` quota
# windows rendered by Codex CLI ``/status``.  A status read whose cached
# quota snapshot is older than this (or absent) triggers one quota-driven
# refresh, so opening Settings shows live quota without a conversation.
CODEX_USAGE_QUOTA_FRESH_S = 60
CODEX_USAGE_RESET_TIMEOUT_S = 5
CODEX_USAGE_RESET_DETAILS_TIMEOUT_S = 5
CODEX_USAGE_RESET_CONSUME_TIMEOUT_S = 15
CODEX_USAGE_RESET_PUSH_CHANNEL = "oauth"
CODEX_USAGE_RESET_PUSH_TASK_ID = "codex-reset"
CODEX_USAGE_RESET_PUSH_EVENT_TYPE = "codex.reset_offer.updated"

_CACHE_SCHEMA_VERSION = 1
_STATUS_RECONCILE_RETRY_S = 5
_MAX_AVAILABLE_COUNT = 100
_MAX_DETAILS = 16
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TITLE_CHARS = 160
_MAX_DESCRIPTION_CHARS = 400
_PENDING_REDEEM_TTL_S = 24 * 60 * 60
_OBSERVATION_RETENTION_S = 24 * 60 * 60
_PENDING_REQUEST_ID_CHARS = 64
# A 200 with one of these codes is a definitive no-spend outcome; any other
# 2xx shape is ambiguous and the persisted idempotency record is retained.
_CONSUME_SUCCESS_CODE = "reset"
_CONSUME_DEFINITIVE_CODES = frozenset({"nothing_to_reset", "already_redeemed"})
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
    pending = raw.get("pending_redeem")
    if isinstance(pending, dict):
        pending_credit = _bounded_text(pending.get("credit_id"), 256)
        pending_request = _bounded_text(
            pending.get("redeem_request_id"), _PENDING_REQUEST_ID_CHARS)
        pending_created = _timestamp(pending.get("created_at"))
        if pending_credit and pending_request and pending_created is not None:
            entry["pending_redeem"] = {
                "credit_id": pending_credit,
                "redeem_request_id": pending_request,
                "created_at": pending_created,
            }
    return entry


def _read_entries(*, strict: bool = False) -> dict[str, dict[str, Any]]:
    import os
    path = _cache_path()
    raw = read_json(path, default={}, strict=strict) or {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _CACHE_SCHEMA_VERSION:
        if strict and os.path.exists(path):
            raise JsonStoreReadError('Existing account evidence has an unsupported schema')
        return {}
    rows = raw.get("entries")
    if not isinstance(rows, list):
        if strict:
            raise JsonStoreReadError('Existing account evidence is missing its row collection')
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = _normalise_entry(row)
        if strict and (entry is None or (
                isinstance(row, dict) and row.get('pending_redeem') is not None
                and 'pending_redeem' not in entry)):
            raise JsonStoreReadError('Existing account evidence contains an invalid identity or pending record')
        if entry is not None:
            entries[entry["cache_key"]] = entry
    return entries


def _entry_recency(entry: dict[str, Any]) -> int:
    return max(
        int(entry.get("captured_at") or 0),
        int(entry.get("refresh_failed_at") or 0),
    )


def _retain_entry(entry: dict[str, Any], now: int) -> bool:
    # A pending spend is user-command evidence, never disposable observation
    # data. Its request-id reuse window is evaluated by redemption separately.
    if entry.get('pending_redeem'):
        return True
    if entry.get('state') == 'available':
        expiry = int(entry.get('expires_at') or 0)
        return not expiry or expiry > now
    return now - _entry_recency(entry) <= _OBSERVATION_RETENTION_S


def _write_entries(entries: dict[str, dict[str, Any]], *, now: int | None = None) -> None:
    # Only a refresh/command with an explicit observation clock reclaims stale
    # reconstructible rows. Scoped logout performs only its requested removal.
    rows = [entry for entry in entries.values()
            if now is None or _retain_entry(entry, now)]
    rows.sort(key=_entry_recency)
    write_json_atomic(_cache_path(),
                      {'schema_version': _CACHE_SCHEMA_VERSION, 'entries': rows},
                      mode=0o600)


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
        from lib.http_transport.client import http_get

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
    with identity_flight(_cache_path(), identity["cache_key"]):
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
            try:
                from lib.subscription_quota import (
                    record_codex_usage_snapshot)
                record_codex_usage_snapshot(
                    usage_payload, cache_key="oauth_codex", now=current_time)
            except Exception as quota_error:
                # Quota rides on the same GET as a best-effort projection; it
                # must never fail the reset-credit refresh.
                logger.debug(
                    "[CodexUsageReset] quota snapshot update failed: %s",
                    quota_error)
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
            latest_entries = _read_entries(strict=True)
            pending = (latest_entries.get(identity["cache_key"]) or {}).get('pending_redeem')
            if isinstance(pending, dict):
                # Refresh is observation, not resolution of an ambiguous spend.
                # Re-read under the write lock so concurrent evidence survives.
                entry['pending_redeem'] = dict(pending)
            latest_entries[identity["cache_key"]] = entry
            _write_entries(latest_entries, now=current_time)
        return _project(entry, now=current_time, refreshing=False)


def _consume_url() -> str:
    from lib.oauth.codex import CODEX_OAUTH_CONFIG

    return (
        f"{CODEX_OAUTH_CONFIG['account_api_base'].rstrip('/')}"
        "/rate-limit-reset-credits/consume"
    )


def _authenticated_post(
    url: str, payload: dict[str, Any], *, timeout: float, user_id: str,
    affinity_key: str,
) -> Any:
    """POST one JSON control-plane command as the sole Codex identity owner.

    Mirrors :func:`_authenticated_get` routing (direct vs pinned desktop
    egress agent) but returns the raw response so the caller can distinguish
    definitive 4xx rejections from ambiguous transport/5xx outcomes.
    """
    from lib.oauth.outbound import resolve_oauth_request

    token, headers, _body = resolve_oauth_request(
        "codex", {"_conv_id": f"codex-usage-reset:{affinity_key}"}, None,
        user_id=user_id,
    )
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    body = json.dumps(payload).encode("utf-8")

    from lib.desktop import egress as desktop_egress

    route = desktop_egress.route_request(url, user_id=user_id)
    if route == "direct":
        from lib.http_transport.client import http_post

        return http_post(url, headers=headers, data=body, timeout=timeout)
    return desktop_egress.egress_http(
        url,
        method="POST",
        headers=headers,
        body=body,
        timeout=timeout,
        user_id=user_id,
        agent_id=route,
    )


def _consume_payload(response: Any) -> tuple[int, dict[str, Any] | None]:
    status = int(getattr(response, "status_code", 0) or 0)
    content = getattr(response, "content", b"") or b""
    if len(content) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("Codex consume response exceeded 1 MiB")
    try:
        payload = response.json()
    except Exception as exc:
        logger.debug('codex consume payload not json (status=%s): %s',
                     status, type(exc).__name__)
        payload = None
    return status, payload if isinstance(payload, dict) else None


def _redeem_request_id(
    entry: dict[str, Any] | None, credit_id: str, now: int
) -> str:
    """Reuse the persisted idempotency key for an unresolved retry.

    A retry only borrows the original request id when it targets the SAME
    credit within the bounded window; anything else mints a fresh key.
    """
    pending = (entry or {}).get("pending_redeem")
    if isinstance(pending, dict):
        created = _timestamp(pending.get("created_at")) or 0
        if (pending.get("credit_id") == credit_id
                and isinstance(pending.get("redeem_request_id"), str)
                and 0 <= now - created < _PENDING_REDEEM_TTL_S):
            return pending["redeem_request_id"]
    return str(uuid.uuid4())


def _write_pending_redeem(
    identity: dict[str, str], pending: dict[str, Any] | None, *, now: int
) -> None:
    with locked_path(_cache_path() + ".write"):
        entries = _read_entries(strict=True)
        entry = entries.get(identity["cache_key"])
        if entry is None:
            if pending is None:
                return
            # No projection row exists yet.  Idempotency persistence must
            # not be skipped, so the record rides on a placeholder whose old
            # captured_at keeps it immediately refresh-eligible.
            entry = {
                **identity,
                "state": "unknown",
                "available_count": None,
                "captured_at": max(1, now - CODEX_USAGE_RESET_TTL_S - 1),
            }
        if pending is None:
            entry.pop("pending_redeem", None)
        else:
            entry["pending_redeem"] = dict(pending)
        entries[identity["cache_key"]] = entry
        _write_entries(entries, now=now)


def _consume_under_lock(
    identity: dict[str, str], *, user_id: str, now: int
) -> dict[str, Any]:
    from lib.oauth.token_store import load_token

    try:
        details = _parse_credit_details(
            _authenticated_get(
                _details_url(),
                timeout=CODEX_USAGE_RESET_DETAILS_TIMEOUT_S,
                user_id=user_id,
                affinity_key=identity["cache_key"],
            )
        )
    except Exception as error:
        logger.warning(
            "[CodexUsageReset] consume pre-read failed: %s", error)
        return {"ok": False, "error": "upstream_unavailable"}
    if not details:
        return {"ok": False, "error": "none_available"}
    if _identity(load_token("codex") or {}, user_id) != identity:
        return {"ok": False, "error": "account_changed"}

    # Details arrive expiry-ascending, so the first row is the credit that
    # would be lost soonest.
    credit_id = details[0]["id"]
    entry = _read_entries().get(identity["cache_key"])
    request_id = _redeem_request_id(entry, credit_id, now)
    # Persist the idempotency key BEFORE the spend: a retry after an
    # ambiguous outcome reuses it instead of risking a second credit.
    _write_pending_redeem(identity, {
        "credit_id": credit_id,
        "redeem_request_id": request_id,
        "created_at": now,
    }, now=now)

    try:
        response = _authenticated_post(
            _consume_url(),
            {"credit_id": credit_id, "redeem_request_id": request_id},
            timeout=CODEX_USAGE_RESET_CONSUME_TIMEOUT_S,
            user_id=user_id,
            affinity_key=identity["cache_key"],
        )
        status, payload = _consume_payload(response)
    except Exception as error:
        logger.warning(
            "[CodexUsageReset] consume outcome unknown; "
            "idempotency record retained: %s", error)
        return {"ok": False, "error": "unresolved"}

    code = str((payload or {}).get("code") or "")
    if status == 200 and code == _CONSUME_SUCCESS_CODE:
        _write_pending_redeem(identity, None, now=now)
        result: dict[str, Any] = {"ok": True, "code": code}
        windows_reset = _integer(
            (payload or {}).get("windows_reset"),
            low=0, high=_MAX_AVAILABLE_COUNT)
        if windows_reset is not None:
            result["windows_reset"] = windows_reset
        credit_body = (payload or {}).get("credit")
        if isinstance(credit_body, dict):
            redeemed_at = _bounded_text(credit_body.get("redeemed_at"), 64)
            if redeemed_at:
                result["redeemed_at"] = redeemed_at
        logger.info(
            "[CodexUsageReset] reset credit consumed for owner/account %s",
            identity["cache_key"][:8])
        return result
    if status == 200 and code in _CONSUME_DEFINITIVE_CODES:
        _write_pending_redeem(identity, None, now=now)
        return {"ok": False, "error": code}
    if 400 <= status < 500:
        _write_pending_redeem(identity, None, now=now)
        logger.info(
            "[CodexUsageReset] consume definitively rejected with HTTP %s",
            status)
        return {"ok": False, "error": "upstream_rejected", "status": status}
    logger.warning(
        "[CodexUsageReset] consume outcome unresolved "
        "(status=%s, code=%r); idempotency record retained", status, code)
    return {"ok": False, "error": "unresolved"}


def consume_codex_usage_reset(
    *, user_id: str = "", now: float | None = None
) -> dict[str, Any]:
    """Redeem one banked reset credit as an explicitly confirmed command.

    The owner/account singleflight lock serializes the spend with refreshes;
    afterwards the cache is force-refreshed and the settled projection is
    published so every surface reconciles immediately.  The returned dict
    always carries ``ok`` plus a machine-readable ``error`` on failure and
    the fresh ``reset_offer`` projection whenever the account was reachable.
    """
    from lib.oauth.token_store import load_token

    current_time = int(time.time() if now is None else now)
    stored = load_token("codex") or {}
    if not stored.get("access_token"):
        return {"ok": False, "error": "not_authenticated"}
    identity = _identity(stored, user_id)
    if identity is None:
        return {"ok": False, "error": "account_identity_unavailable"}

    with identity_flight(_cache_path(), identity["cache_key"]):
        if _identity(load_token("codex") or {}, user_id) != identity:
            outcome = {"ok": False, "error": "account_changed"}
        else:
            outcome = _consume_under_lock(
                identity, user_id=user_id, now=current_time)

    projection = refresh_codex_usage_reset(user_id=user_id, force=True)
    _publish_codex_usage_reset_update(
        user_id=user_id, reset_offer=projection)
    outcome["reset_offer"] = projection
    return outcome

def _is_refreshing(cache_key: str) -> bool:
    with _state_lock:
        return cache_key in _refreshing_keys


def _publish_codex_usage_reset_update(
    *, user_id: str, reset_offer: dict[str, Any]
) -> None:
    """Publish one bounded owner-scoped completion projection, fail-soft.

    The HTTP status read remains the durable reconciliation path.  This frame
    only lets an already-connected browser consume the daemon's result without
    issuing a second status request; lost frames converge through its bounded
    fallback timer.
    """
    if not str(user_id or "").strip():
        return
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event

        push_event(
            CODEX_USAGE_RESET_PUSH_CHANNEL,
            CODEX_USAGE_RESET_PUSH_TASK_ID,
            build_push_frame(
                CODEX_USAGE_RESET_PUSH_EVENT_TYPE,
                provider="codex",
                reset_offer=dict(reset_offer),
            ),
            user_id=user_id,
        )
    except Exception as error:
        # Push is an optimization, never the authority for account state.
        logger.debug(
            "[CodexUsageReset] completion push failed; browser will reconcile: %s",
            error,
        )


def trigger_codex_usage_reset_refresh(
    *, user_id: str = "", force: bool = False
) -> bool:
    """Start one process-local daemon refresh for the current owner/account.

    ``force=True`` bypasses the reset-credit entry freshness check inside
    :func:`refresh_codex_usage_reset`; callers use it when a different
    projection on the same GET (the quota snapshot) is due even though the
    reset-credit entry itself is still fresh.
    """
    from lib.oauth.token_store import load_token

    identity = _identity(load_token("codex") or {}, user_id)
    if identity is None:
        return False
    cache_key = identity["cache_key"]
    with _state_lock:
        if cache_key in _refreshing_keys:
            return False
        _refreshing_keys.add(cache_key)

    ready, cancelled = threading.Event(), threading.Event()

    def _run() -> None:
        reset_offer: dict[str, Any] | None = None
        try:
            ready.wait()
            if cancelled.is_set():
                return
            try:
                reset_offer = refresh_codex_usage_reset(
                    user_id=user_id, force=force
                )
            except Exception as error:
                logger.warning(
                    "[CodexUsageReset] daemon refresh failed before projection: %s",
                    error,
                )
            # The identity claim remains live through completion publication;
            # another request for this identity cannot duplicate the query
            # while its receipt is still being published.
            if reset_offer is not None:
                _publish_codex_usage_reset_update(
                    user_id=user_id,
                    reset_offer=reset_offer,
                )
        finally:
            with _state_lock:
                _refreshing_keys.discard(cache_key)

    worker = threading.Thread(target=_run, daemon=True,
                              name=f"codex-usage-reset-{cache_key[:8]}")
    try:
        worker.start()
    except BaseException:
        cancelled.set()
        if not worker.is_alive():
            with _state_lock:
                _refreshing_keys.discard(cache_key)
        raise
    finally:
        ready.set()
    return True


def codex_usage_reset_status(
    *, user_id: str = "", refresh_if_stale: bool = True,
    now: float | None = None, quota_refresh_due: bool = False,
) -> dict[str, Any]:
    """Return a non-blocking ``available|none|unknown`` status projection.

    ``quota_refresh_due`` marks the cached quota snapshot as missing or older
    than :data:`CODEX_USAGE_QUOTA_FRESH_S`; one extra refresh is then allowed
    once per freshness window even when the reset-credit entry is still fresh.
    """
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
    quota_due = (
        quota_refresh_due and entry is not None
        and current_time - _entry_recency(entry) >= CODEX_USAGE_QUOTA_FRESH_S
    )
    refresh_needed = refresh_if_stale and (
        _needs_refresh(entry, current_time) or quota_due)
    if refresh_needed:
        # A quota-driven trigger must reach the network even when the
        # reset-credit entry is fresh: the daemon's inner freshness check
        # would otherwise discard the one refresh this window allows, leaving
        # a restarted process (empty in-memory quota cache) without a quota
        # snapshot until the 30-minute entry TTL expires.
        trigger_codex_usage_reset_refresh(user_id=user_id, force=quota_due)
    projected = _project(
        entry,
        now=current_time,
        refreshing=_is_refreshing(identity["cache_key"]),
    )
    if (refresh_needed and not projected["refreshing"]
            and "retry_after_seconds" not in projected):
        # Covers an already-finished/changed projection, never admission pressure.
        projected["retry_after_seconds"] = _STATUS_RECONCILE_RETRY_S
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
        entries = _read_entries(strict=True)
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
    "CODEX_USAGE_QUOTA_FRESH_S",
    "CODEX_USAGE_RESET_CONSUME_TIMEOUT_S",
    "codex_usage_reset_status",
    "refresh_codex_usage_reset",
    "consume_codex_usage_reset",
    "trigger_codex_usage_reset_refresh",
    "clear_codex_usage_reset_cache",
]

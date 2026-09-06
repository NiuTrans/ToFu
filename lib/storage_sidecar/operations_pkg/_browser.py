"""Bounded owner-scoped storage for non-executable browser observations."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import _integer, _required_text


_MAX_OBSERVATION_BYTES = 4_096
_MAX_OBSERVATIONS_PER_OWNER = 200
_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_OUTCOMES = frozenset({
    "success", "not_observed", "structure_mismatch", "not_found",
    "auth_challenge", "rate_limited", "transient_failure", "policy_denied",
})
_STRATEGIES = frozenset({
    "token_gated_api", "hydrated_state", "captured_api", "rendered_dom",
})
_ROUTE_FAMILY_RE = re.compile(
    r"^/(?:[a-z][a-z_-]{0,39}|\{(?:segment|truncated)\})?"
    r"(?:/(?:[a-z][a-z_-]{0,39}|\{(?:segment|truncated)\}))*$"
)
_SENSITIVE_HINT_FIELD_RE = re.compile(
    r"(?:token|secret|password|passwd|authorization|cookie|credential|session|"
    r"ticket|sso|api[_-]?key)", re.I)
_SHAPE_DESCRIPTOR_RE = re.compile(
    r"(?:null|boolean|number|string(?:\(len=\d+\))?|array\(\d+\)|"
    r"object(?:\(empty\))?|reached \d+-entry budget)"
)
_IDENTIFIER_PARENT_SEGMENTS = frozenset({
    "account", "accounts", "document", "documents", "employee", "employees",
    "item", "items", "member", "members", "order", "orders", "org", "orgs",
    "organization", "organizations", "people", "person", "profile", "profiles",
    "project", "projects", "team", "teams", "user", "users",
})


def _route_family_valid(route_family: object) -> bool:
    if (not isinstance(route_family, str)
            or _ROUTE_FAMILY_RE.fullmatch(route_family) is None):
        return False
    segments = route_family.split("/")[1:]
    return all(
        index == 0 or segments[index - 1] not in _IDENTIFIER_PARENT_SEGMENTS
        or segment == "{segment}"
        for index, segment in enumerate(segments)
    )


def _protocol_error(message: str) -> StorageError:
    return StorageError("database_protocol_error", message)


def _identity(payload: Mapping[str, Any]) -> tuple[int, str, str, str]:
    owner_user_id = _integer(payload, "owner_user_id", minimum=1)
    origin = _required_text(payload, "origin", 512)
    route_family = _required_text(payload, "route_family", 512)
    operation = _required_text(payload, "operation", 64)
    try:
        origin_parts = urlsplit(origin)
        origin_valid = (
            origin_parts.scheme in {"http", "https"}
            and bool(origin_parts.hostname)
            and not origin_parts.username and not origin_parts.password
            and not origin_parts.path and not origin_parts.query
            and not origin_parts.fragment
        )
    except ValueError:
        origin_valid = False
    if not origin_valid:
        raise _protocol_error("Invalid site observation origin")
    if not _route_family_valid(route_family):
        raise _protocol_error("Invalid site observation route family")
    return owner_user_id, origin, route_family, operation


def _decode_hints(raw: object) -> list[dict]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError) as exc:
        raise StorageError("database_integrity", "Stored site observation hints are invalid") from exc
    if not isinstance(value, list):
        raise StorageError("database_integrity", "Stored site observation hints are invalid")
    return [dict(item) for item in value if isinstance(item, Mapping)][:5]


def _row_document(row: Mapping[str, Any]) -> dict:
    return {
        "schema_version": int(row["schema_version"]),
        "origin": str(row["origin"]),
        "route_family": str(row["route_family"]),
        "operation": str(row["operation"]),
        "strategy": str(row["strategy"]),
        "api_hints": _decode_hints(row["hints_json"]),
        "anti_bot_vendor": str(row["anti_bot_vendor"]),
        "auth_signal": str(row["auth_signal"]),
        "status": str(row["status"]),
        "confidence_milli": int(row["confidence_milli"]),
        "visit_count": int(row["visit_count"]),
        "successful_visits": int(row["successful_visits"]),
        "hinted_visits": int(row["hinted_visits"]),
        "hint_match_visits": int(row["hint_match_visits"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "last_outcome": str(row["last_outcome"]),
        "last_elapsed_ms": int(row["last_elapsed_ms"]),
        "last_verified_at_ms": int(row["last_verified_at_ms"]),
        "last_observed_at_ms": int(row["last_observed_at_ms"]),
        "expires_at_ms": int(row["expires_at_ms"]),
        "payload_bytes": int(row["payload_bytes"]),
    }


def _browser_site_observation_get(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    owner_user_id, origin, route_family, operation = _identity(payload)
    now_ms = _integer(payload, "now_ms", minimum=0)
    row = session.fetch_one(
        "SELECT * FROM storage_browser_site_observations WHERE "
        "owner_user_id=? AND origin=? AND route_family=? AND operation=?",
        (owner_user_id, origin, route_family, operation),
    )
    if row is None or int(row["expires_at_ms"]) <= now_ms:
        return None
    return _row_document(row)


def _validated_observation(payload: Mapping[str, Any]) -> dict:
    source = payload.get("observation")
    if not isinstance(source, Mapping):
        raise _protocol_error("A successful site observation requires an observation object")
    schema_version = source.get("schema_version")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise _protocol_error("Unsupported site observation schema version")
    strategy = str(source.get("strategy") or "")
    if strategy not in _STRATEGIES:
        raise _protocol_error("Invalid site observation strategy")
    hints = source.get("api_hints")
    if not isinstance(hints, list) or len(hints) > 5:
        raise _protocol_error("Invalid site observation API hints")
    normalized_hints: list[dict] = []
    for hint in hints:
        if not isinstance(hint, Mapping) or hint.get("passive_only") is not True:
            raise _protocol_error("Site observation hints must be passive-only")
        if set(hint) != {
                "method", "origin", "path_template", "shape_summary", "score",
                "passive_only"}:
            raise _protocol_error("Site observation hint fields are invalid")
        method = hint.get("method")
        hint_origin = hint.get("origin")
        hint_path = hint.get("path_template")
        shape = hint.get("shape_summary")
        score = hint.get("score")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise _protocol_error("Invalid site observation hint method")
        try:
            hint_parts = urlsplit(hint_origin) if isinstance(hint_origin, str) else None
            hint_origin_valid = (
                hint_parts is not None and hint_parts.scheme in {"http", "https"}
                and bool(hint_parts.hostname) and not hint_parts.username
                and not hint_parts.password and not hint_parts.path
                and not hint_parts.query and not hint_parts.fragment
            )
        except ValueError:
            hint_origin_valid = False
        if (not isinstance(hint_origin, str) or len(hint_origin) > 512
                or not hint_origin_valid):
            raise _protocol_error("Invalid site observation hint origin")
        if (not isinstance(hint_path, str) or len(hint_path) > 512
                or not _route_family_valid(hint_path)):
            raise _protocol_error("Site observation hints cannot retain query data")
        if (not isinstance(shape, Mapping) or len(shape) > 12
                or any(not isinstance(key, str) or not key or len(key) > 240
                       or (_SENSITIVE_HINT_FIELD_RE.search(key)
                           and key != "$.[sensitive]")
                       or not isinstance(value, str) or not value or len(value) > 80
                       or _SHAPE_DESCRIPTOR_RE.fullmatch(value) is None
                       for key, value in shape.items())):
            raise _protocol_error("Invalid site observation hint shape")
        if (not isinstance(score, (int, float)) or isinstance(score, bool)
                or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
            raise _protocol_error("Invalid site observation hint score")
        normalized_hints.append({
            "method": method, "origin": hint_origin,
            "path_template": hint_path, "shape_summary": dict(shape),
            "score": float(score), "passive_only": True,
        })
    encoded_hints = json.dumps(
        normalized_hints, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True)
    document_bytes = len(json.dumps(
        dict(source), ensure_ascii=False, separators=(",", ":"),
        sort_keys=True).encode("utf-8"))
    if document_bytes > _MAX_OBSERVATION_BYTES:
        raise _protocol_error("Site observation exceeds its byte budget")
    elapsed_ms = source.get("elapsed_ms", 0)
    if (not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool)
            or not 0 <= elapsed_ms <= 120_000):
        raise _protocol_error("Invalid site observation elapsed time")
    capture_hint_used = source.get("capture_hint_used", False)
    capture_hint_matched = source.get("capture_hint_matched", False)
    if not isinstance(capture_hint_used, bool) or not isinstance(capture_hint_matched, bool):
        raise _protocol_error("Invalid site observation capture-hint metrics")
    if capture_hint_matched and not capture_hint_used:
        raise _protocol_error("A capture hint cannot match when no hint was used")
    anti_bot_vendor = source.get("anti_bot_vendor", "")
    auth_signal = source.get("auth_signal", "none")
    if anti_bot_vendor not in {"", "aliyun_waf", "cloudflare", "akamai", "geetest"}:
        raise _protocol_error("Invalid site observation anti-bot vendor")
    if auth_signal not in {"none", "challenge"}:
        raise _protocol_error("Invalid site observation auth signal")
    return {
        "schema_version": 1,
        "strategy": strategy,
        "hints_json": encoded_hints,
        "anti_bot_vendor": anti_bot_vendor,
        "auth_signal": auth_signal,
        "last_elapsed_ms": elapsed_ms,
        "payload_bytes": document_bytes,
        "capture_hint_used": capture_hint_used,
        "capture_hint_matched": capture_hint_matched,
    }


def _prune_owner_lru(session: Session, owner_user_id: int, now_ms: int) -> None:
    expired = session.fetch_all(
        "SELECT origin, route_family, operation FROM storage_browser_site_observations "
        "WHERE owner_user_id=? AND expires_at_ms<=? "
        "ORDER BY expires_at_ms, origin, route_family, operation LIMIT 64",
        (owner_user_id, now_ms),
    )
    for row in expired:
        session.execute(
            "DELETE FROM storage_browser_site_observations WHERE owner_user_id=? "
            "AND origin=? AND route_family=? AND operation=?",
            (owner_user_id, row["origin"], row["route_family"], row["operation"]),
        )
    rows = session.fetch_all(
        "SELECT origin, route_family, operation FROM storage_browser_site_observations "
        "WHERE owner_user_id=? ORDER BY last_observed_at_ms DESC, origin, "
        "route_family, operation LIMIT ?",
        (owner_user_id, _MAX_OBSERVATIONS_PER_OWNER + 1),
    )
    for row in rows[_MAX_OBSERVATIONS_PER_OWNER:]:
        session.execute(
            "DELETE FROM storage_browser_site_observations WHERE owner_user_id=? "
            "AND origin=? AND route_family=? AND operation=?",
            (owner_user_id, row["origin"], row["route_family"], row["operation"]),
        )


def _browser_site_observation_record(
    session: Session, payload: Mapping[str, Any],
) -> Any:
    owner_user_id, origin, route_family, operation = _identity(payload)
    observed_at_ms = _integer(payload, "observed_at_ms", minimum=1)
    outcome = _required_text(payload, "outcome", 32)
    if outcome not in _OUTCOMES:
        raise _protocol_error("Invalid site observation outcome")
    # This row update is the backend-neutral per-owner write fence. SQLite's
    # sole writer and PostgreSQL's conflicting row update both serialize the
    # following count/prune sequence, so concurrent domains cannot exceed the
    # owner's hard 200-row budget.
    session.execute(
        "INSERT INTO storage_browser_site_observation_owners("
        "owner_user_id,touched_at_ms) VALUES (?,?) "
        "ON CONFLICT(owner_user_id) DO UPDATE SET "
        "touched_at_ms=excluded.touched_at_ms",
        (owner_user_id, observed_at_ms),
    )
    previous = session.fetch_one(
        "SELECT * FROM storage_browser_site_observations WHERE "
        "owner_user_id=? AND origin=? AND route_family=? AND operation=?",
        (owner_user_id, origin, route_family, operation),
    )

    if outcome == "success":
        current = _validated_observation(payload)
        same_strategy = previous is not None and previous["strategy"] == current["strategy"]
        confidence = min(1_000, int(previous["confidence_milli"]) + 100) \
            if same_strategy else (500 if previous is None else 400)
        successful_visits = int(previous["successful_visits"]) + 1 if previous else 1
        hinted_visits = (int(previous["hinted_visits"]) if previous else 0) + int(
            current["capture_hint_used"])
        hint_match_visits = (
            int(previous["hint_match_visits"]) if previous else 0) + int(
                current["capture_hint_matched"])
        consecutive_failures = 0
        status = "active"
        verified_at_ms = observed_at_ms
    else:
        if previous is None:
            return None
        current = {
            "schema_version": int(previous["schema_version"]),
            "strategy": str(previous["strategy"]),
            "hints_json": str(previous["hints_json"]),
            "anti_bot_vendor": str(previous["anti_bot_vendor"]),
            "auth_signal": (
                "challenge" if outcome == "auth_challenge"
                else str(previous["auth_signal"])),
            "last_elapsed_ms": int(previous["last_elapsed_ms"]),
            "payload_bytes": int(previous["payload_bytes"]),
            "capture_hint_used": False,
            "capture_hint_matched": False,
        }
        confidence = int(previous["confidence_milli"])
        successful_visits = int(previous["successful_visits"])
        hinted_visits = int(previous["hinted_visits"])
        hint_match_visits = int(previous["hint_match_visits"])
        consecutive_failures = int(previous["consecutive_failures"])
        if outcome in {"structure_mismatch", "not_found"}:
            confidence = max(0, confidence - 250)
            consecutive_failures += 1
        elif outcome == "not_observed":
            confidence = max(0, confidence - 100)
            consecutive_failures += 1
        status = "quarantined" if consecutive_failures >= 3 else str(previous["status"])
        verified_at_ms = int(previous["last_verified_at_ms"])

    visit_count = int(previous["visit_count"]) + 1 if previous else 1
    expires_at_ms = observed_at_ms + _RETENTION_MS
    session.execute(
        "INSERT INTO storage_browser_site_observations("
        "owner_user_id,origin,route_family,operation,schema_version,strategy,"
        "hints_json,anti_bot_vendor,auth_signal,status,confidence_milli,"
        "visit_count,successful_visits,hinted_visits,hint_match_visits,"
        "consecutive_failures,last_outcome,"
        "last_elapsed_ms,last_verified_at_ms,last_observed_at_ms,expires_at_ms,"
        "payload_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(owner_user_id,origin,route_family,operation) DO UPDATE SET "
        "schema_version=excluded.schema_version,strategy=excluded.strategy,"
        "hints_json=excluded.hints_json,anti_bot_vendor=excluded.anti_bot_vendor,"
        "auth_signal=excluded.auth_signal,status=excluded.status,"
        "confidence_milli=excluded.confidence_milli,visit_count=excluded.visit_count,"
        "successful_visits=excluded.successful_visits,"
        "hinted_visits=excluded.hinted_visits,"
        "hint_match_visits=excluded.hint_match_visits,"
        "consecutive_failures=excluded.consecutive_failures,"
        "last_outcome=excluded.last_outcome,last_elapsed_ms=excluded.last_elapsed_ms,"
        "last_verified_at_ms=excluded.last_verified_at_ms,"
        "last_observed_at_ms=excluded.last_observed_at_ms,"
        "expires_at_ms=excluded.expires_at_ms,payload_bytes=excluded.payload_bytes",
        (
            owner_user_id, origin, route_family, operation,
            current["schema_version"], current["strategy"], current["hints_json"],
            current["anti_bot_vendor"], current["auth_signal"], status, confidence,
            visit_count, successful_visits, hinted_visits, hint_match_visits,
            consecutive_failures, outcome,
            current["last_elapsed_ms"], verified_at_ms, observed_at_ms,
            expires_at_ms, current["payload_bytes"],
        ),
    )
    _prune_owner_lru(session, owner_user_id, observed_at_ms)
    row = session.fetch_one(
        "SELECT * FROM storage_browser_site_observations WHERE "
        "owner_user_id=? AND origin=? AND route_family=? AND operation=?",
        (owner_user_id, origin, route_family, operation),
    )
    return _row_document(row) if row is not None else None


__all__ = [
    "_browser_site_observation_get", "_browser_site_observation_record",
]

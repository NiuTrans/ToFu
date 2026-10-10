"""Owner-scoped, non-executable observations from generic browser research.

This module is the application/repository boundary for reconstructible site
observations.  It accepts only already-authorized analysis, removes URL query
data and dynamic path identifiers, and stores a bounded structural projection
through semantic Storage Sidecar operations.  Observations are advisory: they
never authorize access or cause an endpoint to be replayed.

Entry points: ``load_site_observation``, ``distill_site_observation``,
``record_site_observation`` and ``render_site_observation``.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import unquote, urlsplit

from lib.log import get_logger


logger = get_logger(__name__)

SITE_OBSERVATION_SCHEMA_VERSION = 1
SITE_OBSERVATION_OPERATION = "research"
SITE_OBSERVATION_MAX_BYTES = 4_096
SITE_OBSERVATION_MAX_HINTS = 5
SITE_OBSERVATION_MAX_SHAPE_ENTRIES = 12
_STORAGE_DEADLINE_SECONDS = 0.25

_STRATEGIES = frozenset({
    "token_gated_api", "hydrated_state", "captured_api", "rendered_dom",
})
_SAFE_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_DYNAMIC_SEGMENT = re.compile(
    r"(?:\d|@|^[0-9a-f]{8}(?:-[0-9a-f-]{8,})?$|^[A-Za-z0-9_-]{33,}$)", re.I)
_STATIC_SEGMENT = re.compile(r"[A-Za-z][A-Za-z_-]{0,39}")
_SENSITIVE_FIELD = re.compile(
    r"(?:token|secret|password|passwd|authorization|cookie|credential|session|"
    r"ticket|sso|api[_-]?key)", re.I)
_IDENTIFIER_PARENT_SEGMENTS = frozenset({
    "account", "accounts", "document", "documents", "employee", "employees",
    "item", "items", "member", "members", "order", "orders", "org", "orgs",
    "organization", "organizations", "people", "person", "profile", "profiles",
    "project", "projects", "team", "teams", "user", "users",
})


def _owner_id(owner_user_id: str | int) -> int:
    if isinstance(owner_user_id, bool):
        raise ValueError("owner_user_id must be a positive integer")
    value = int(str(owner_user_id or "").strip())
    if value < 1:
        raise ValueError("owner_user_id must be a positive integer")
    return value


def _origin(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("site observation URL must be HTTP(S)")
    host = parts.hostname.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("site observation URL has an invalid port") from exc
    default_port = 443 if parts.scheme.lower() == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host if port in (None, default_port) else f"{rendered_host}:{port}"
    return f"{parts.scheme.lower()}://{authority}"


def _template_path(url: str) -> str:
    parts = urlsplit(str(url or ""))
    segments: list[str] = []
    hide_next_segment = False
    prior_static_segment = ""
    for raw_segment in parts.path.split("/"):
        if not raw_segment:
            continue
        segment = unquote(raw_segment)[:160]
        sensitive_segment = bool(_SENSITIVE_FIELD.search(segment))
        if (hide_next_segment or prior_static_segment in _IDENTIFIER_PARENT_SEGMENTS
                or sensitive_segment or _DYNAMIC_SEGMENT.search(segment)
                or not _STATIC_SEGMENT.fullmatch(segment)):
            segments.append("{segment}")
            current_static_segment = ""
        else:
            current_static_segment = segment.lower()
            segments.append(current_static_segment)
        hide_next_segment = sensitive_segment
        prior_static_segment = current_static_segment
        if len(segments) >= 24:
            segments.append("{truncated}")
            break
    route = "/" + "/".join(segments)
    return route[:512] or "/"


def site_observation_identity(url: str, *, operation: str = SITE_OBSERVATION_OPERATION) -> dict:
    operation = str(operation or "").strip().lower()
    if not operation or len(operation) > 64 or not re.fullmatch(r"[a-z][a-z0-9_-]*", operation):
        raise ValueError("invalid site observation operation")
    return {
        "origin": _origin(url),
        "route_family": _template_path(url),
        "operation": operation,
    }


def _safe_shape(shape: object) -> dict[str, str]:
    if not isinstance(shape, dict):
        return {}
    result: dict[str, str] = {}
    for raw_path, raw_descriptor in shape.items():
        if len(result) >= SITE_OBSERVATION_MAX_SHAPE_ENTRIES:
            break
        path = str(raw_path or "")[:240]
        descriptor = str(raw_descriptor or "")[:80]
        if not path or not descriptor:
            continue
        if _SENSITIVE_FIELD.search(path):
            path = "$.[sensitive]"
        result[path] = descriptor
    return result


def _distill_hints(analysis: dict) -> list[dict]:
    network = analysis.get("network") if isinstance(analysis, dict) else None
    candidates = network.get("candidates") if isinstance(network, dict) else None
    hints: list[dict] = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("verdict") not in {"likely_data", "maybe_data"}:
            continue
        method = str(candidate.get("method") or "GET").upper()
        if method not in _SAFE_METHODS:
            continue
        try:
            endpoint_origin = _origin(str(candidate.get("url") or ""))
            path_template = _template_path(str(candidate.get("url") or ""))
        except ValueError:
            continue
        hints.append({
            "method": method,
            "origin": endpoint_origin,
            "path_template": path_template,
            "shape_summary": _safe_shape(candidate.get("shape")),
            "score": max(0.0, min(1.0, float(candidate.get("real_data_score") or 0.0))),
            "passive_only": True,
        })
        if len(hints) >= SITE_OBSERVATION_MAX_HINTS:
            break
    return hints


def distill_site_observation(analysis: dict, *, elapsed_ms: int = 0) -> dict:
    """Return the bounded persistence projection for authorized analysis."""
    strategy = str(analysis.get("strategy") or "") if isinstance(analysis, dict) else ""
    if strategy not in _STRATEGIES:
        raise ValueError("invalid site observation strategy")
    anti_bot = analysis.get("anti_bot") if isinstance(analysis.get("anti_bot"), dict) else {}
    network = analysis.get("network") if isinstance(analysis.get("network"), dict) else {}
    capture = network.get("capture") if isinstance(network.get("capture"), dict) else {}
    vendor = str(anti_bot.get("vendor") or "")[:64] if anti_bot.get("detected") else ""
    observation = {
        "schema_version": SITE_OBSERVATION_SCHEMA_VERSION,
        "strategy": strategy,
        "api_hints": _distill_hints(analysis),
        "anti_bot_vendor": vendor,
        "auth_signal": "challenge" if strategy == "token_gated_api" else "none",
        "elapsed_ms": max(0, min(120_000, int(elapsed_ms or 0))),
        "capture_hint_used": int(capture.get("priority_hint_count") or 0) > 0,
        "capture_hint_matched": int(capture.get("priority_body_matches") or 0) > 0,
    }
    while len(json.dumps(observation, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > SITE_OBSERVATION_MAX_BYTES:
        if observation["api_hints"]:
            observation["api_hints"].pop()
        else:
            raise ValueError("site observation exceeds its byte budget")
    return observation


def load_site_observation(
    owner_user_id: str | int,
    url: str,
    *,
    operation: str = SITE_OBSERVATION_OPERATION,
) -> dict | None:
    """Best-effort lookup; storage failure must never break page research."""
    try:
        from lib.storage import get_storage_client

        identity = site_observation_identity(url, operation=operation)
        return get_storage_client().query(
            "browser.site_observation.get",
            {"owner_user_id": _owner_id(owner_user_id), **identity,
             "now_ms": int(time.time() * 1000)},
            deadline=_STORAGE_DEADLINE_SECONDS,
        )
    except Exception as exc:
        logger.debug("[BrowserResearch] site observation lookup unavailable: %s", exc)
        return None


def record_site_observation(
    owner_user_id: str | int,
    url: str,
    *,
    observation: dict | None = None,
    outcome: str = "success",
    operation: str = SITE_OBSERVATION_OPERATION,
) -> dict | None:
    """Best-effort bounded update after a live research outcome."""
    try:
        from lib.storage import get_storage_client

        identity = site_observation_identity(url, operation=operation)
        payload = {
            "owner_user_id": _owner_id(owner_user_id),
            **identity,
            "outcome": str(outcome or ""),
            "observed_at_ms": int(time.time() * 1000),
        }
        if observation is not None:
            payload["observation"] = observation
        return get_storage_client(write=True).command(
            "browser.site_observation.record", payload, command_id=None,
            deadline=_STORAGE_DEADLINE_SECONDS,
        )
    except Exception as exc:
        logger.debug("[BrowserResearch] site observation write unavailable: %s", exc)
        return None


def render_site_observation(observation: dict | None) -> str:
    """Render a small advisory block; current live evidence always wins."""
    if not isinstance(observation, dict):
        return ""
    lines = [
        "Prior site observation (advisory; current access and evidence must be revalidated):",
        f'- strategy={observation.get("strategy") or "unknown"} '
        f'confidence={int(observation.get("confidence_milli") or 0) / 1000:.2f} '
        f'status={observation.get("status") or "unknown"}',
    ]
    vendor = str(observation.get("anti_bot_vendor") or "")
    if vendor:
        lines.append(f"- previously observed anti-bot vendor={vendor}; do not skip live detection")
    hints = observation.get("api_hints")
    if isinstance(hints, list) and hints:
        lines.append("- passive capture hints (never replay automatically):")
        for hint in hints[:SITE_OBSERVATION_MAX_HINTS]:
            if not isinstance(hint, dict):
                continue
            lines.append(
                f'  - {hint.get("method") or "GET"} '
                f'{hint.get("origin") or ""}{hint.get("path_template") or "/"} '
                f'score={float(hint.get("score") or 0.0):.2f}'
            )
    return "\n".join(lines)[:1_600]


def render_adapter_promotion(observation: dict | None) -> str:
    """Suggest human-reviewed promotion only after measured stable reuse."""
    if not isinstance(observation, dict) or observation.get("status") != "active":
        return ""
    visits = int(observation.get("visit_count") or 0)
    successes = int(observation.get("successful_visits") or 0)
    hinted = int(observation.get("hinted_visits") or 0)
    matched = int(observation.get("hint_match_visits") or 0)
    if (visits < 5 or successes / max(1, visits) < 0.8
            or int(observation.get("confidence_milli") or 0) < 900
            or hinted < 2 or matched / hinted < 0.8):
        return ""
    return (
        "Adapter promotion candidate: this route has stable, repeatedly verified "
        "passive data evidence. Generate only a non-executable adapter skeleton; "
        "a human must review and test any handler before registration."
    )


__all__ = [
    "SITE_OBSERVATION_MAX_BYTES", "SITE_OBSERVATION_SCHEMA_VERSION",
    "distill_site_observation", "load_site_observation",
    "record_site_observation", "render_site_observation",
    "render_adapter_promotion",
    "site_observation_identity",
]

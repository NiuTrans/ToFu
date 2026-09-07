"""Bound and sanitize caller-controlled outbound provider headers.

This is the transport-neutral authority shared by legacy-provider migration,
model-routing v2 persistence, and credential-envelope decoding. Keeping the
check below those adapters prevents a newly added ingestion path from
bypassing the same impersonation and memory bounds.
"""

from __future__ import annotations

from typing import Any


_FORBIDDEN_EXTRA_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "host",
    "content-length",
    "transfer-encoding",
    "proxy-authorization",
})
_MAX_EXTRA_HEADERS = 16
_MAX_HEADER_VALUE_LEN = 2048


def sanitise_extra_headers(raw: Any) -> tuple[dict[str, str], str | None]:
    """Return normalized scalar headers or an actionable validation error."""
    if raw is None or raw == {}:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "`extra_headers` must be an object"
    if len(raw) > _MAX_EXTRA_HEADERS:
        return {}, (
            f"`extra_headers` has too many entries (max {_MAX_EXTRA_HEADERS})")
    clean: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            return clean, "`extra_headers` keys must be non-empty strings"
        normalized_name = name.strip()
        if normalized_name.lower() in _FORBIDDEN_EXTRA_HEADERS:
            return clean, (
                f"`extra_headers[{name!r}]` is reserved; forbidden names: "
                f"{sorted(_FORBIDDEN_EXTRA_HEADERS)}")
        if not isinstance(value, (str, int, float, bool)):
            return clean, (
                f"`extra_headers[{name!r}]` must be a scalar "
                "(string/number/bool)")
        normalized_value = str(value)
        if len(normalized_value) > _MAX_HEADER_VALUE_LEN:
            return clean, (
                f"`extra_headers[{name!r}]` value too long "
                f"(max {_MAX_HEADER_VALUE_LEN})")
        clean[normalized_name] = normalized_value
    return clean, None


__all__ = ["sanitise_extra_headers"]

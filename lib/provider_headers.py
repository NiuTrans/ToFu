"""Bound and sanitize caller-controlled outbound provider headers.

This is the transport-neutral authority shared by legacy-provider migration,
model-routing v2 persistence, and credential-envelope decoding. Keeping the
check below those adapters prevents a newly added ingestion path from
bypassing the same impersonation and memory bounds.
"""

from __future__ import annotations

from typing import Any, Mapping


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

# Header names that conventionally carry credential material.  Such headers
# belong in the encrypted credential-secret envelope; every other header is
# routing/configuration metadata and belongs on the inspectable Connection,
# exactly where legacy plaintext provider config kept it.
_CREDENTIAL_HEADER_MARKERS = (
    "auth",
    "key",
    "token",
    "secret",
    "password",
    "cookie",
    "credential",
)


def is_credential_header(name: str) -> bool:
    """Return True when a header name conventionally carries secret material."""
    lowered = str(name or "").strip().lower()
    return any(marker in lowered for marker in _CREDENTIAL_HEADER_MARKERS)


def split_header_secrecy(headers: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split headers into (envelope, connection) buckets.

    Credential-named headers must stay in the encrypted secret envelope, and
    transport-reserved names stay there too because ``sanitise_extra_headers``
    rejects them on Connection metadata.  Every other header is routing or
    configuration metadata — historically stored in plaintext legacy config —
    and belongs on Connection ``extra_headers`` where users can inspect and
    edit it.  The classifier errs toward the envelope bucket.
    """
    envelope: dict[str, str] = {}
    connection: dict[str, str] = {}
    for name, value in (headers or {}).items():
        lowered = str(name or "").strip().lower()
        if is_credential_header(name) or lowered in _FORBIDDEN_EXTRA_HEADERS:
            envelope[str(name)] = str(value)
        else:
            connection[str(name)] = str(value)
    return envelope, connection


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


__all__ = [
    "is_credential_header",
    "sanitise_extra_headers",
    "split_header_secrecy",
]

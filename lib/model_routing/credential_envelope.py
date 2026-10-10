"""Shared reading of the ``tofu.credential-secret/v1`` plaintext envelope.

The envelope is the storage/dispatch encoding that carries one credential's
``api_key``/``oauth`` pair plus its encrypted ``extra_headers`` in a single
secret string.  Dispatch decodes it at request materialization; presentation
surfaces must never treat the raw JSON as the key itself.  In particular a
recognition hint derived from the envelope renders the JSON chrome
(``{"ap****…``) on Settings key cards instead of the key's own digits.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from lib.secret_envelope import secret_hint


ENVELOPE_FORMAT = "tofu.credential-secret/v1"


def decode_credential_envelope(plaintext: str) -> Mapping[str, Any] | None:
    """Return the envelope mapping when *plaintext* is a credential envelope."""
    try:
        decoded: Any = json.loads(str(plaintext or ""))
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(decoded, Mapping) and decoded.get("format") == ENVELOPE_FORMAT:
        return decoded
    return None


def credential_secret_display_hint(plaintext: str) -> str:
    """Derive a recognition hint from the usable key, never the envelope.

    Mirrors the migration plan's hint semantics: the API key's own digits when
    one exists, a generic ``headers`` marker for header-only gateway
    credentials, and an empty hint otherwise.  Raw (non-envelope) secrets keep
    their historical hint behavior.
    """
    envelope = decode_credential_envelope(plaintext)
    if envelope is None:
        return secret_hint(plaintext)
    api_key = str(envelope.get("api_key") or "").strip()
    if api_key:
        return secret_hint(api_key)
    if envelope.get("extra_headers"):
        return "headers"
    return ""


__all__ = [
    "ENVELOPE_FORMAT",
    "credential_secret_display_hint",
    "decode_credential_envelope",
]

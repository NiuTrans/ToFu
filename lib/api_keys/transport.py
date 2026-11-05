"""Select a bearer candidate from supplied HTTP or WebSocket transport values.

select_credential owns transport precedence and reports the selected source.
It does not authenticate, infer an owner or access request globals/storage.
WebSockets omit query values; HTTP may supply them for the first-link flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

SESSION_COOKIE = 'tofu_session'
TOKEN_PREFIXES = ('tofu_live_', 'tofu_admin_')


@dataclass(frozen=True, slots=True)
class CredentialCandidate:
    """Untrusted selection metadata; the bearer stays out of diagnostic repr."""

    token: str = field(repr=False)
    source: Literal['header', 'x-api-key', 'cookie', 'query', 'none']


def select_credential(
    headers: Mapping[str, str] | None,
    cookies: Mapping[str, str] | None,
    query: Mapping[str, str] | None = None,
) -> CredentialCandidate:
    """Keep explicit Bearer > x-api-key > session cookie > optional query.

    An explicit nonempty Bearer is definitive, including an unknown prefix:
    authentication must reject it instead of selecting another credential.
    Other transports accept only the existing Tofu credential prefixes.
    Headers retain the caller's case-insensitive transport mapping semantics.
    """
    authorization = (headers.get('Authorization') or '') if headers is not None else ''
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        token = parts[1].strip()
        if token:
            return CredentialCandidate(token, 'header')
    for source, values, key in (
        ('x-api-key', headers, 'x-api-key'),
        ('cookie', cookies, SESSION_COOKIE),
        ('query', query, 'token'),
    ):
        token = (values.get(key) or '').strip() if values is not None else ''
        if token.startswith(TOKEN_PREFIXES):
            return CredentialCandidate(token, source)
    return CredentialCandidate('', 'none')

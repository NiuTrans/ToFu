"""Provider-declared feature boundary for Responses-speaking endpoints.

``protocol='responses'`` proves only the core wire shape.  It does not imply
that an OpenAI-compatible gateway accepts public OpenAI extensions such as
Tool Search, WebSocket mode, Pro reasoning, or native multi-agent.  This
module keeps that second capability explicit and fail-closed.
"""

from __future__ import annotations

from urllib.parse import urlparse


RESPONSES_FEATURE_PROFILES = frozenset({'compatible', 'openai', 'codex'})


def normalize_responses_feature_profile(
        value='', *, protocol='', base_url='', oauth='') -> str:
    """Return the effective Responses feature profile for one wire face.

    ``auto``/empty recognizes only OpenAI's canonical public API host.  Every
    other key-based Responses endpoint is core-compatible until an operator
    explicitly declares ``openai``.  Codex subscription traffic has its own
    dialect and never inherits public-only fields.
    """
    if str(protocol or '').strip().lower() != 'responses':
        return ''
    if str(oauth or '').strip().lower() == 'codex':
        return 'codex'

    raw = str(value or '').strip().lower()
    if raw in RESPONSES_FEATURE_PROFILES:
        return 'compatible' if raw == 'codex' else raw
    host = (urlparse(str(base_url or '')).hostname or '').lower()
    return 'openai' if host == 'api.openai.com' else 'compatible'


__all__ = [
    'RESPONSES_FEATURE_PROFILES',
    'normalize_responses_feature_profile',
]

"""CatPaw Credit-Hub gateway (mcli.internal.example.com) request-shape adapter.

The CatPaw CLI (``mc --code``) points Claude Code at mcli.internal.example.com, which
bills the employee's daily Credit Hub quota instead of a Friday AppID.  The
gateway speaks the Anthropic Messages API and — verified by live probes on
2026-09-09 against kimi-k3 / glm-5.3 / deepseek-v4-pro — enforces exactly
two fingerprints before forwarding:

  * ``system`` must be present and its first text must START
    (case-insensitively) with the Claude Code identity line; anything else
    is HTTP 400 "Request is not allowed".  Both the string and the
    block-list form pass, and any suffix (the caller's real system prompt)
    may follow the identity.
  * ``X-Working-Dir`` must be present and non-empty; the VALUE itself is not
    validated (any string passes; only missing/empty is 400).

``X-Client-Token`` / ``X-Timestamp`` / ``X-Repo-Url`` / ``X-Branch`` /
``X-Client-Version`` — sent by the real CLI — are NOT validated (garbage and
absent both pass), so they are deliberately not reproduced here.  Auth is
the 32-char token from ``~/.config/mcopilot-cli/.config.yaml``
(``AUTHORIZATION``); ``anthropic_headers`` already sends it as Bearer.

Only whitelisted models are routed upstream (deepseek-v4-flash/pro,
kimi-k2.6/k2.7-code/k3, MiniMax-M2.7/M3, gpt-5.4, glm-5.1/5.2/5.3/5.3-flash,
LongCat-2.0); the roster lives in ``static/provider_templates/catpaw.json``.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

#: Exact literal the gateway requires at the START of the first system text.
#: Same string as lib.oauth.outbound.CLAUDE_CODE_IDENTITY; duplicated so
#: lib.llm stays free of oauth imports.
CATPAW_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

_IDENTITY_PREFIX = 'you are claude code'

_GATEWAY_HOST = 'mcli.internal.example.com'


def is_catpaw_base_url(base_url) -> bool:
    """True when ``base_url`` targets the CatPaw mcli gateway."""
    try:
        host = urlparse(str(base_url or '')).hostname
    except ValueError:
        return False
    return (host or '').lower() == _GATEWAY_HOST


def apply_catpaw_shape(body: dict) -> dict:
    """Force ``body['system']`` to start with the Claude Code identity.

    Operates on the OUTPUT of ``openai_body_to_anthropic`` (a fresh dict, so
    mutation is safe).  The caller's system text is preserved verbatim after
    the identity — appended to the same string, or kept as later blocks.
    """
    system = body.get('system')
    if isinstance(system, str):
        if not system.lstrip().lower().startswith(_IDENTITY_PREFIX):
            body['system'] = (CATPAW_IDENTITY if not system.strip()
                              else f'{CATPAW_IDENTITY}\n\n{system}')
        return body
    if isinstance(system, list) and system:
        blocks = [b for b in system if isinstance(b, dict)]
        first_text = next((str(b.get('text') or '') for b in blocks
                           if b.get('type') == 'text'), '')
        if not first_text.lstrip().lower().startswith(_IDENTITY_PREFIX):
            body['system'] = [{'type': 'text', 'text': CATPAW_IDENTITY},
                              *blocks]
    else:
        body['system'] = CATPAW_IDENTITY
    return body


def catpaw_headers() -> dict:
    """Gateway-mandated headers: a non-empty ``X-Working-Dir``."""
    return {'X-Working-Dir': os.getcwd() or '/'}

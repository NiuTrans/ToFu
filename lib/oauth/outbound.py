"""lib/oauth/outbound.py — Use a logged-in subscription as an LLM provider.

Turns a stored Claude Pro/Max or ChatGPT (Codex) OAuth subscription into a
usable outbound provider. A subscription access token is NOT a normal API
key: it expires hourly (so it must be resolved live, per request) and the
upstream only accepts it from a client that presents the right *identity*
headers (and, for Claude, a mandatory system-prompt prefix). This module
holds that spec in one place; the request pre-flight
(:func:`lib.llm._sse_core.prepare_request` for streaming and
:func:`lib.llm.chat.chat` for non-streaming) calls
:func:`resolve_oauth_request` when a dispatch slot is marked ``oauth=``.

Header / body requirements (verified against the open-source harnesses that
keep these working — opencode, CLIProxyAPI, the openai/codex CLI, and the
``earendil-works/pi`` reverse-engineering issues):

* **Codex** → ``POST https://chatgpt.com/backend-api/codex/responses``
  (Responses API; the body translation is handled separately by
  :func:`lib.oauth.codex.codex_translate_request`, auto-triggered by the
  base URL). Token rides ``Authorization: Bearer``. The backend whitelists
  first-party ``originator`` values, so ``originator: codex_cli_rs`` AND a
  matching ``User-Agent`` are BOTH required or it answers 403. The
  ChatGPT account id (parsed from the id_token JWT at login) goes in
  ``chatgpt-account-id``.

* **Claude** → ``POST https://api.anthropic.com/v1/messages?beta=true``.
  The 2026 block returns 401 for ``Authorization: Bearer`` on subscription
  tokens, so the token rides ``x-api-key`` and ``Authorization`` MUST be
  absent. ``anthropic-beta`` must carry ``claude-code-20250219`` +
  ``oauth-2025-04-20``. The ``system`` field MUST begin with the exact
  literal identity string as its OWN first block — concatenating it into
  another block returns 400.
"""

from __future__ import annotations

import uuid

from lib.log import get_logger

logger = get_logger(__name__)

#: Exact literal the Claude Messages API requires as the first system block
#: when authenticating with a Claude-Code subscription OAuth token.
CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

#: Mandatory beta flags for the subscription-OAuth path (order matters: the
#: two Claude-Code betas lead; any caller betas are appended after).
_CLAUDE_OAUTH_BETAS = ('claude-code-20250219', 'oauth-2025-04-20')

#: ``originator`` is whitelisted by the Codex backend; the User-Agent must
#: match (start with ``codex_cli_rs``) or the request is rejected with 403.
_CODEX_ORIGINATOR = 'codex_cli_rs'
_CODEX_USER_AGENT = 'codex_cli_rs/0.20.0 (external; Tofu)'
_CLAUDE_USER_AGENT = 'claude-cli/2.1.85 (external, cli)'

#: Provider-config ``oauth`` values this module knows how to bridge.
OAUTH_PROVIDERS = ('claude', 'codex')


def is_oauth_provider(oauth: str) -> bool:
    """True when ``oauth`` names a subscription provider we can bridge."""
    return oauth in OAUTH_PROVIDERS


def resolve_oauth_request(oauth: str, body: dict, extra_headers: dict | None):
    """Resolve the live token + identity headers (+ body shape) for a slot.

    Args:
        oauth: ``'claude'`` or ``'codex'`` — the subscription kind.
        body: the OpenAI-shaped request body (pre-translation).
        extra_headers: caller headers to merge the identity headers onto.

    Returns:
        ``(api_key, extra_headers, body)`` with the live token, merged
        identity headers, and (for Claude) the identity system block applied.

    Raises:
        RuntimeError: when no valid token is available (not logged in /
            refresh failed) — the dispatch layer treats this as a slot
            error and fails over.
    """
    hdrs = dict(extra_headers or {})

    if oauth == 'codex':
        from lib.oauth.codex import codex_get_valid_token
        from lib.oauth.token_store import load_token
        token = codex_get_valid_token()
        if not token:
            raise RuntimeError('Codex subscription not logged in '
                               '(no valid OAuth token)')
        stored = load_token('codex') or {}
        account_id = stored.get('account_id', '')
        hdrs['OpenAI-Beta'] = 'responses=experimental'
        hdrs['originator'] = _CODEX_ORIGINATOR
        hdrs['User-Agent'] = _CODEX_USER_AGENT
        hdrs['session_id'] = uuid.uuid4().hex
        if account_id:
            hdrs['chatgpt-account-id'] = account_id
        return token, hdrs, body

    if oauth == 'claude':
        from lib.oauth.claude import claude_get_valid_token
        token = claude_get_valid_token()
        if not token:
            raise RuntimeError('Claude subscription not logged in '
                               '(no valid OAuth token)')
        hdrs['anthropic-beta'] = _merge_betas(hdrs.get('anthropic-beta', ''))
        hdrs['x-app'] = 'cli'
        hdrs['User-Agent'] = _CLAUDE_USER_AGENT
        hdrs['anthropic-dangerous-direct-browser-access'] = 'true'
        body = _prepend_claude_identity(body)
        return token, hdrs, body

    return None, hdrs, body


def claude_oauth_url(url: str) -> str:
    """Append the ``?beta=true`` query the Claude-Code OAuth path expects."""
    if 'beta=' in url:
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}beta=true'


def _merge_betas(existing: str) -> str:
    """Lead with the two mandatory Claude-Code betas, then any caller betas."""
    out = list(_CLAUDE_OAUTH_BETAS)
    for b in (existing or '').split(','):
        b = b.strip()
        if b and b not in out:
            out.append(b)
    return ','.join(out)


def _prepend_claude_identity(body: dict) -> dict:
    """Ensure the body's system messages begin with the exact identity block.

    The Messages API rejects a subscription token unless ``system`` starts
    with :data:`CLAUDE_CODE_IDENTITY` as its own block. We inject it as a
    separate leading ``system`` message so ``openai_body_to_anthropic`` hoists
    it into ``system[0]`` (it keeps multiple system blocks as a list, never
    concatenating — which would trigger the 400).
    """
    msgs = body.get('messages') or []
    first = msgs[0] if msgs else None
    if (isinstance(first, dict) and first.get('role') == 'system'
            and isinstance(first.get('content'), str)
            and first['content'].startswith(CLAUDE_CODE_IDENTITY)):
        return body
    new = dict(body)
    new['messages'] = [{'role': 'system', 'content': CLAUDE_CODE_IDENTITY}] + list(msgs)
    return new


# ══════════════════════════════════════════════════════════
#  Managed provider provisioning (server_config.json)
# ══════════════════════════════════════════════════════════
#
# On a successful subscription login we register a synthetic provider in
# server_config.json so the model shows up in dispatch with no manual
# Settings work. The provider carries an ``oauth`` marker (resolved live at
# request time) and a SENTINEL api_key (never used — the real token is
# fetched per request) so the slot builder treats it as a normal cloud
# provider. ``managed_oauth: True`` lets us cleanly remove it on logout
# without touching user-curated providers.

#: provider id → spec used to build the managed server_config entry.
_MANAGED_SPECS = {
    'codex': {
        'id': 'oauth_codex',
        'name': 'ChatGPT (Codex subscription)',
        'base_url': 'https://chatgpt.com/backend-api/codex',
        'protocol': 'openai',
        'models': [
            {'model_id': 'gpt-5.1-codex', 'capabilities': ['text', 'vision']},
            {'model_id': 'gpt-5-codex', 'capabilities': ['text', 'vision']},
            {'model_id': 'gpt-5', 'capabilities': ['text', 'vision']},
        ],
        # The Codex path is Responses-API streaming only (no non-stream
        # translator), so every model must dispatch with stream=True.
        'stream_only': True,
    },
    'claude': {
        'id': 'oauth_claude',
        'name': 'Claude (Pro/Max subscription)',
        'base_url': 'https://api.anthropic.com/v1',
        'protocol': 'anthropic',
        'thinking_format': 'thinking_type',
        'models': [
            {'model_id': 'claude-sonnet-4-5-20250929', 'capabilities': ['text', 'vision', 'thinking']},
            {'model_id': 'claude-opus-4-1-20250805', 'capabilities': ['text', 'vision', 'thinking']},
        ],
        'stream_only': False,
    },
}

#: Sentinel key — the slot builder requires a non-empty api_key for cloud
#: providers, but the real subscription token is resolved live per request.
_OAUTH_SENTINEL_KEY = 'oauth-managed'


def provision_oauth_provider(provider: str) -> bool:
    """Add/refresh the managed server_config provider for a subscription.

    Idempotent: replaces any existing managed entry for this provider.
    Returns True when server_config.json was updated.
    """
    spec = _MANAGED_SPECS.get(provider)
    if not spec:
        return False
    from lib import _SERVER_CONFIG_PATH, reload_config
    from lib.json_store import update_json_atomic
    from lib.llm_dispatch import reset_dispatcher

    entry = {
        'id': spec['id'],
        'name': spec['name'],
        'base_url': spec['base_url'],
        'brand': 'oauth',
        'enabled': True,
        'oauth': provider,
        'api_keys': [_OAUTH_SENTINEL_KEY],
        'protocol': spec.get('protocol', ''),
        'thinking_format': spec.get('thinking_format', ''),
        'models': [dict(m, stream_only=spec.get('stream_only', False))
                   for m in spec['models']],
    }

    def _mutate(cfg):
        providers = [p for p in (cfg.get('providers') or [])
                     if p.get('id') != spec['id']]
        providers.append(entry)
        cfg['providers'] = providers
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    reload_config()
    reset_dispatcher()
    logger.info('[OAuth] Provisioned managed provider %s (%d models)',
                spec['id'], len(spec['models']))
    return True


def deprovision_oauth_provider(provider: str) -> bool:
    """Remove the managed server_config provider for a subscription (logout).

    Returns True when an entry was removed.
    """
    spec = _MANAGED_SPECS.get(provider)
    if not spec:
        return False
    from lib import _SERVER_CONFIG_PATH, reload_config
    from lib.json_store import update_json_atomic
    from lib.llm_dispatch import reset_dispatcher

    removed = {'n': 0}

    def _mutate(cfg):
        before = cfg.get('providers') or []
        after = [p for p in before if p.get('id') != spec['id']]
        removed['n'] = len(before) - len(after)
        cfg['providers'] = after
        return cfg

    update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
    if removed['n']:
        reload_config()
        reset_dispatcher()
        logger.info('[OAuth] Deprovisioned managed provider %s', spec['id'])
    return bool(removed['n'])


__all__ = [
    'CLAUDE_CODE_IDENTITY',
    'OAUTH_PROVIDERS',
    'is_oauth_provider',
    'resolve_oauth_request',
    'claude_oauth_url',
    'provision_oauth_provider',
    'deprovision_oauth_provider',
]

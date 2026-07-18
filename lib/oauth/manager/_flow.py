"""lib/oauth/manager/_flow.py — OAuth flow lifecycle: start + status.

``start_oauth_flow`` generates PKCE + auth URL and (for localhost-redirect
providers) spawns the relay server thread. ``get_oauth_status`` /
``get_all_oauth_status`` report flow + token state. Shared flow state comes
BY REFERENCE from ``._state``; the relay entrypoint from ``._relay``.
"""

import threading
import time

from lib.log import get_logger

from lib.oauth.manager._state import (
    _active_flows,
    _flows_lock,
    _FLOW_TIMEOUT,
)
from lib.oauth.manager._relay import _run_relay_server

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════

def start_oauth_flow(provider: str) -> dict:
    """Start an OAuth login flow.

    Generates PKCE codes and auth URL, starts relay server on
    the registered callback port. The frontend opens the auth URL
    in a popup and listens for postMessage with the code.

    Args:
        provider: 'claude' or 'codex'.

    Returns:
        dict with 'auth_url', 'status', 'provider', 'callback_port'.
    """
    if provider == 'claude':
        from lib.oauth.claude import claude_build_auth_url
        flow = claude_build_auth_url()
    elif provider == 'codex':
        from lib.oauth.codex import codex_build_auth_url
        flow = codex_build_auth_url()
    else:
        return {'error': f'Unknown provider: {provider}'}

    # Store flow state
    with _flows_lock:
        _active_flows[provider] = {
            'status': 'started',
            'auth_url': flow['auth_url'],
            'state': flow['state'],
            'pkce': flow['pkce'],
            'started_at': time.time(),
            'error': None,
            'email': None,
        }

    # Start relay server in background thread (only for providers that
    # redirect to localhost — Claude redirects to console.anthropic.com,
    # so the user must manually copy the code#state back)
    if provider != 'claude':
        thread = threading.Thread(
            target=_run_relay_server,
            args=(provider, flow['callback_port'], flow['state']),
            daemon=True,
            name=f'oauth-relay-{provider}',
        )
        thread.start()
        logger.info('[OAuth] Started %s flow — relay on :%d, auth URL ready',
                     provider, flow['callback_port'])
    else:
        logger.info('[OAuth] Started %s flow — auth URL ready (manual code paste required)',
                     provider)
    return {
        'auth_url': flow['auth_url'],
        'status': 'started',
        'provider': provider,
        'callback_port': flow['callback_port'],
        # Browser-side exchange params (B1): lets the frontend POST the token
        # exchange from the user's own network when the server is geo-blocked.
        'exchange': flow.get('exchange', {}),
    }


def get_oauth_status(provider: str) -> dict:
    """Get current OAuth status for a provider."""
    from lib.oauth.token_store import load_token

    with _flows_lock:
        flow = _active_flows.get(provider, {})
        # Auto-expire stale flows that have been waiting too long
        if flow and flow.get('status') in ('started', 'waiting_callback'):
            started_at = flow.get('started_at', 0)
            if started_at and (time.time() - started_at) > _FLOW_TIMEOUT:
                logger.info('[OAuth] Auto-expiring stale %s flow (started %.0fs ago)',
                            provider, time.time() - started_at)
                _active_flows.pop(provider, None)
                flow = {}

    stored = load_token(provider)
    authenticated = bool(stored and stored.get('access_token'))

    return {
        'provider': provider,
        'status': flow.get('status', 'not_started'),
        'error': flow.get('error'),
        'email': flow.get('email') or (stored.get('email', '') if stored else ''),
        'authenticated': authenticated,
        'expire': stored.get('expire') if stored else None,
    }


def get_all_oauth_status() -> dict:
    """Get OAuth status for all supported providers."""
    return {
        'claude': get_oauth_status('claude'),
        'codex': get_oauth_status('codex'),
    }

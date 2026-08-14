"""lib/oauth/manager/_flow.py — OAuth flow lifecycle: start + status.

``start_oauth_flow`` generates PKCE + auth URL and (for localhost-redirect
providers) spawns the relay server thread. ``get_oauth_status`` /
``get_all_oauth_status`` report flow + token state. Shared flow state comes
BY REFERENCE from ``._state``; the relay entrypoint from ``._relay``.
"""

import os
import sys
import threading
import time

from lib.log import get_logger

from lib.oauth.manager._state import (
    _active_flows,
    _flows_lock,
    _FLOW_TIMEOUT,
)
from lib.oauth.manager._relay import _run_relay_server, _bind_relay

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════

def _loopback_callback_ok() -> bool:
    """Whether a loopback ``redirect_uri`` can reach THIS process's relay.

    True only for the packaged desktop app. The relay binds 127.0.0.1 on the
    SERVER, so a loopback callback only resolves when the browser runs on
    that same machine — guaranteed in the desktop build (launcher.py pins
    ``BIND_HOST=127.0.0.1`` and opens the browser itself), and not knowable
    otherwise.

    ``sys.frozen`` is the load-bearing signal rather than the peer address,
    for the reason ``routes.api_v1.desktop._setup_state`` already documents:
    a same-host reverse proxy makes every public request present as
    loopback, so the peer address cannot distinguish "the user is on this
    box" from "the user is behind nginx". A frozen process IS the tray app
    by construction. Guessing wrong here is expensive in one direction
    only — a remote user would be sent to a localhost URL on THEIR machine,
    where nothing is listening — so the default stays console+paste.

    ``TOFU_OAUTH_LOOPBACK=1|0`` forces the decision for operators who run a
    source checkout on their own laptop (1) or front the desktop build with
    a proxy (0).
    """
    override = (os.environ.get('TOFU_OAUTH_LOOPBACK') or '').strip()
    if override in ('0', '1'):
        return override == '1'
    return bool(getattr(sys, 'frozen', False))


def start_oauth_flow(provider: str, prefer_console: bool = False) -> dict:
    """Start an OAuth login flow.

    Generates PKCE codes and auth URL, starts relay server on
    the registered callback port. The frontend opens the auth URL
    in a popup and listens for postMessage with the code.

    Args:
        provider: 'claude' or 'codex'.
        prefer_console: Force Claude onto the console callback (manual code
            paste) even when the loopback callback would be available. This
            is the USER'S escape hatch, not a debug flag: whether Anthropic
            accepts the loopback redirect for this client is an EXTERNAL
            fact we cannot verify locally, and a desktop user cannot set an
            environment variable to get out of a broken flow. Ignored for
            codex, whose only registered redirect IS the loopback.

    Returns:
        dict with 'auth_url', 'status', 'provider', 'callback_port' and
        'redirect_mode' ('loopback' | 'console'). ``redirect_mode`` is what
        lets the UI describe the flow truthfully — the manual-paste
        instructions are a LIE during a loopback flow, because the provider
        redirects to localhost instead of rendering a code.
    """
    # ── Claude: bind BEFORE building the URL, because the bind decides it ──
    # Claude accepts either the console callback (user copies code#state back)
    # or the loopback callback the relay can capture silently. The loopback is
    # only advertisable if we actually own the port, and only meaningful if the
    # browser is on this machine — so the bind is attempted first and its
    # RESULT picks the redirect. A failed bind degrades to the console flow,
    # which is exactly the behaviour every non-desktop deployment already has.
    relay_server = None
    if provider == 'claude':
        from lib.oauth.claude import CLAUDE_OAUTH_CONFIG
        from lib.oauth.claude import claude_build_auth_url
        if prefer_console:
            logger.info('[OAuth] claude flow forced onto the console callback '
                        'by an explicit caller request')
        elif _loopback_callback_ok():
            relay_server = _bind_relay('claude',
                                       CLAUDE_OAUTH_CONFIG['callback_port'],
                                       '')
            if relay_server is None:
                logger.info('[OAuth] claude loopback port busy — using the '
                            'console callback (manual code paste)')
        flow = claude_build_auth_url(use_loopback=relay_server is not None)
        if relay_server is not None:
            # _bind_relay could not know the state before the URL existed.
            relay_server.RequestHandlerClass.expected_state = flow['state']
    elif provider == 'codex':
        from lib.oauth.codex import codex_build_auth_url
        flow = codex_build_auth_url()
    else:
        return {'error': f'Unknown provider: {provider}'}

    # Which callback the user is actually about to walk — decided HERE, BEFORE
    # the flow is stored, because the STATUS projection reads it back out of
    # the stored flow: a page reload mid-flow re-renders the card from
    # get_oauth_status, never from this function's return value. A mode that
    # only lives in the login response dies with that response.
    redirect_mode = 'loopback'
    if provider == 'claude':
        redirect_mode = 'loopback' if relay_server is not None else 'console'

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
            # The redirect actually advertised — the exchange MUST echo this
            # exact string or the token endpoint answers invalid_grant.
            'redirect_uri': flow.get('redirect_uri', ''),
            'redirect_mode': redirect_mode,
            # Browser-side exchange params (token_url / code_verifier / …).
            # Same rule as redirect_mode: on the desktop build the SERVER is
            # the user's machine, so a geo-blocked server exchange leaves the
            # BROWSER exchange as the only path — and a reload wipes the
            # frontend's copy unless it can be restored from this projection.
            'exchange': flow.get('exchange'),
        }

    # Start the relay thread for providers whose callback lands on localhost:
    # codex always, claude only when the loopback bind above succeeded.
    # Claude on the console callback has no relay to run — the code is shown
    # on Anthropic's page and pasted back by hand.
    if provider != 'claude' or relay_server is not None:
        thread = threading.Thread(
            target=_run_relay_server,
            args=(provider, flow['callback_port'], flow['state']),
            kwargs={'server': relay_server},
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
        'redirect_mode': redirect_mode,
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
    from lib.oauth.outbound import managed_oauth_provider_status
    provider_status = managed_oauth_provider_status(provider)

    return {
        'provider': provider,
        'status': flow.get('status', 'not_started'),
        'error': flow.get('error'),
        'email': flow.get('email') or (stored.get('email', '') if stored else ''),
        'authenticated': authenticated,
        **provider_status,
        'expire': stored.get('expire') if stored else None,
        # A page reload mid-flow re-renders the card from THIS payload alone.
        # Without the mode the UI cannot restore truthful instructions or the
        # console escape hatch (the cancel/retry button re-runs the SAME
        # callback decision); without the URL it cannot re-open the popup.
        'redirect_mode': flow.get('redirect_mode'),
        'auth_url': flow.get('auth_url'),
        # Lets the reloaded page restore _oauthExchangeParams — without these,
        # the browser exchange rejects with no-exchange-params and the curl
        # helper cannot even build its command, leaving an unburned code with
        # no path to redemption. The code_verifier is no new exposure: it was
        # already handed to this same browser in the login response, and the
        # status endpoint is same-origin on the same auth surface.
        'exchange': flow.get('exchange'),
    }


def get_all_oauth_status() -> dict:
    """Get OAuth status for all supported providers."""
    return {
        'claude': get_oauth_status('claude'),
        'codex': get_oauth_status('codex'),
    }

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
import uuid

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


def try_reuse_codex_login(*, owner_user_id: int) -> dict | None:
    """Explicit Login prefers an existing Codex login without a new ceremony."""
    from lib.oauth.codex_shared_login import local_owner, reconcile_shared_login
    if local_owner(owner_user_id) is None:
        return None
    from lib.oauth.token_store import load_token
    reconcile_shared_login(owner_user_id=owner_user_id, explicit=True)
    token = load_token('codex') or {}
    if not token.get('codex_shared_reference'):
        return None
    if not token.get('access_token'):
        return {'provider': 'codex', 'status': 'error',
                'error': 'Existing Codex login is unavailable; update it in Codex, then retry',
                'auth_issue': token.get('shared_login_issue', '')}
    flow_id = uuid.uuid4().hex
    with _flows_lock:
        _active_flows['codex'] = {'flow_id': flow_id, 'owner_user_id': owner_user_id,
                                  'status': 'started', 'started_at': time.time()}
    from lib.oauth.manager._exchange import _finalize_login_success
    result = _finalize_login_success('codex', token, via='shared_codex',
                                     flow_id=flow_id, owner_user_id=owner_user_id)
    return {**result, 'credential_source': token.get('credential_source', '')}


def start_oauth_flow(provider: str, *, owner_user_id: int,
                     prefer_console: bool = False) -> dict:
    """Start an OAuth login flow.

    Generates PKCE codes and auth URL, starts relay server on
    the registered callback port. The frontend opens the auth URL
    in a popup and listens for postMessage with the code.

    Args:
        provider: 'claude' or 'codex'.
        owner_user_id: Authenticated repository owner that started the flow.
            It is captured once so a later callback cannot invent an egress
            scope or complete another owner's in-memory flow.
        prefer_console: Force Claude onto the console callback (manual code
            paste) even when the loopback callback would be available. This
            is the USER'S escape hatch, not a debug flag: whether Anthropic
            accepts the loopback redirect for this client is an EXTERNAL
            fact we cannot verify locally, and a desktop user cannot set an
            environment variable to get out of a broken flow. Ignored for
            codex, whose only registered redirect IS the loopback.

    Returns:
        dict with 'auth_url', 'status', 'provider', 'callback_port' and
        'redirect_mode' ('loopback' | 'manual' | 'console'). ``manual`` means
        the provider still redirects to its registered localhost URI, but the
        browser and this process are not known to share a machine, so the user
        copies that complete callback URL back into Tofu. ``redirect_mode`` is what
        lets the UI describe the flow truthfully — the manual-paste
        instructions are a LIE during a loopback flow, because the provider
        redirects to localhost instead of rendering a code.
    """
    from lib.identity import require_user_id
    owner_user_id = require_user_id(
        owner_user_id, context='OAuth flow owner')
    flow_id = uuid.uuid4().hex

    # A pending device flow for this provider must not outlive the new
    # loopback flow — they share _active_flows[provider].
    from lib.oauth.manager._device import stop_device_flow
    stop_device_flow(provider)
    if provider == 'codex':
        reused = try_reuse_codex_login(owner_user_id=owner_user_id)
        if reused is not None:
            return reused

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
        # OpenAI's public Codex client has one fixed redirect:
        # localhost:1455. Only advertise automatic capture when the browser
        # is known to run beside this process, and bind synchronously before
        # returning the URL (CLIProxyAPI starts its callback listener before
        # opening the browser for the same reason). Remote/server deployments
        # keep the valid PKCE flow but capture the complete callback URL via
        # the Settings paste box instead of binding a useless server loopback.
        if _loopback_callback_ok():
            relay_server = _bind_relay(
                'codex', flow['callback_port'], flow['state'])
            if relay_server is None:
                logger.info('[OAuth] codex loopback port busy — using manual '
                            'callback URL capture')
        else:
            logger.info('[OAuth] codex browser is not known to be local — '
                        'using manual callback URL capture')
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
    elif provider == 'codex':
        redirect_mode = 'loopback' if relay_server is not None else 'manual'

    # Store flow state
    with _flows_lock:
        _active_flows[provider] = {
            'flow_id': flow_id,
            'status': 'started',
            'auth_url': flow['auth_url'],
            'state': flow['state'],
            'pkce': flow['pkce'],
            'started_at': time.time(),
            'error': None,
            'email': None,
            'owner_user_id': owner_user_id,
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

    # Start the relay thread only after a synchronous bind proved that this
    # process owns the callback port. Console/manual modes have no relay: the
    # authorization result is pasted back by the user.
    if relay_server is not None:
        thread = threading.Thread(
            target=_run_relay_server,
            args=(provider, flow['callback_port'], flow['state']),
            kwargs={'server': relay_server, 'flow_id': flow_id},
            daemon=True,
            name=f'oauth-relay-{provider}',
        )
        thread.start()
        logger.info('[OAuth] Started %s flow — relay on :%d, auth URL ready',
                     provider, flow['callback_port'])
    else:
        logger.info('[OAuth] Started %s flow — auth URL ready (manual callback capture required)',
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


def get_oauth_status(
    provider: str, *, owner_user_id: int | None = None,
) -> dict:
    """Get current OAuth status for a provider."""
    from lib.oauth.token_store import load_token

    with _flows_lock:
        flow = _active_flows.get(provider, {})
        # Auto-expire stale flows that have been waiting too long. Device
        # flows carry their own (longer) expires_at — the user code stays
        # valid for 15 minutes, well past the loopback flow's 5.
        if flow and flow.get('status') in ('started', 'waiting_callback'):
            started_at = flow.get('started_at', 0)
            expires_at = flow.get('expires_at') or (
                started_at + _FLOW_TIMEOUT if started_at else 0)
            if expires_at and time.time() > expires_at:
                logger.info('[OAuth] Auto-expiring stale %s flow (started %.0fs ago)',
                            provider, time.time() - started_at)
                _active_flows.pop(provider, None)
                flow = {}

    stored = load_token(provider)
    if (stored or {}).get('codex_shared_reference'):
        from lib.oauth.codex_shared_login import local_owner
        if local_owner(owner_user_id) != stored['codex_shared_reference'].get('owner_user_id'):
            stored = None
    # A stored access_token alone does NOT mean usable auth: refresh tokens
    # are blanked on terminal rejection while the access_token is kept for
    # its remaining lifetime, so the card must reflect the REAL failure
    # modes instead of staying green while every request dies.
    access_token = bool(stored and stored.get('access_token'))
    expire = (stored or {}).get('expire') or 0
    try:
        expire = float(expire)
    except (TypeError, ValueError):
        expire = 0
    now = time.time()
    access_expired = bool(access_token and expire and now >= expire)
    refresh_invalidated = bool((stored or {}).get('refresh_invalidated_at'))
    has_refresh = bool((stored or {}).get('refresh_token'))
    authenticated = bool(
        access_token
        and not refresh_invalidated
        and (not access_expired or has_refresh))
    shared_issue = (stored or {}).get('shared_login_issue') or ''
    auth_issue = shared_issue
    if access_token:
        if refresh_invalidated:
            auth_issue = 'refresh_invalidated'
        elif access_expired and not has_refresh:
            auth_issue = 'access_expired_no_refresh'
        elif access_expired:
            auth_issue = 'access_expired'
    from lib.oauth.outbound import managed_oauth_provider_status
    provider_status = managed_oauth_provider_status(
        provider, owner_user_id=owner_user_id)

    return {
        'provider': provider,
        'status': flow.get('status', 'not_started'),
        'error': flow.get('error') or (
            ('Update the existing login in Codex; Tofu will follow it'
             if shared_issue else 'Session expired — sign in again') if auth_issue else None),
        'credential_source': (stored or {}).get('credential_source', 'tofu'),
        'shared_login_reuse_supported': provider == 'codex',
        'auth_issue': auth_issue,
        'access_token_expired': access_expired,
        'refresh_invalidated': refresh_invalidated,
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

        # Device-flow projection (user_code + verification_url only — the
        # device_auth_id never leaves the poll thread). Lets a reloaded page
        # re-render the code card mid-flow, like redirect_mode above.
        'device': ({'user_code': flow['device']['user_code'],
                    'verification_url': flow['device']['verification_url']}
                   if flow.get('device') else None),
        # Lets the reloaded page restore _oauthExchangeParams — without these,
        # the browser exchange rejects with no-exchange-params and the curl
        # helper cannot even build its command, leaving an unburned code with
        # no path to redemption. The code_verifier is no new exposure: it was
        # already handed to this same browser in the login response, and the
        # status endpoint is same-origin on the same auth surface.
        'exchange': flow.get('exchange'),
    }


def get_all_oauth_status(*, owner_user_id: int | None = None) -> dict:
    """Get OAuth status for all supported providers."""
    return {
        'claude': get_oauth_status('claude', owner_user_id=owner_user_id),
        'codex': get_oauth_status('codex', owner_user_id=owner_user_id),
    }

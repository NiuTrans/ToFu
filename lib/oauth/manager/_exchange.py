"""lib/oauth/manager/_exchange.py — token exchange, store, and logout.

``exchange_code`` (server-side code→token), ``store_token`` (browser-side B1
token persist), and ``logout_oauth`` (delete token + shut relay server).
All flow + server state comes BY REFERENCE from ``._state`` — these
functions mutate the shared dicts in place.
"""

from lib.log import get_logger, audit_log

from lib.oauth.manager._state import (
    _active_flows,
    _flows_lock,
    _active_servers,
    _servers_lock,
)

logger = get_logger(__name__)


def exchange_code(provider: str, code: str, state: str = '') -> dict:
    """Exchange an authorization code for tokens.

    Called by the frontend after receiving the code via postMessage
    from the relay page, or via manual paste.

    Args:
        provider: 'claude' or 'codex'.
        code: Authorization code from OAuth callback.
        state: OAuth state parameter for CSRF validation.

    Returns:
        dict with status info.
    """
    if not code:
        return {'error': 'No authorization code provided'}

    # Get PKCE verifier from the active flow
    with _flows_lock:
        flow = _active_flows.get(provider, {})
    pkce = flow.get('pkce', {})
    pkce_verifier = pkce.get('code_verifier', '')
    flow_state = flow.get('state', '')
    # The redirect advertised at authorize time. OAuth requires the exchange
    # to echo it byte-for-byte, and for Claude it now varies per flow
    # (loopback on the desktop build, console elsewhere), so it is READ from
    # the flow instead of recomputed.
    flow_redirect_uri = flow.get('redirect_uri', '')

    if not pkce_verifier:
        return {'error': 'No active OAuth flow found. Please start a new login first.'}

    # ── CSRF state validation ──
    # If the caller supplied a `state` AND the flow recorded one, they MUST
    # match — a mismatch means the code/state pair did not originate from the
    # flow this server started (classic OAuth CSRF: an attacker feeds a victim
    # their own auth code). Reject rather than exchange. When the caller omits
    # state we fall back to the flow's own state (the manual copy-paste flow
    # has no channel to echo it back); this preserves that path while closing
    # the hole for any caller that DOES present a forged state.
    if state and flow_state and state != flow_state:
        logger.warning('[OAuth] state mismatch for %s — rejecting code exchange '
                       '(possible CSRF)', provider)
        with _flows_lock:
            if provider in _active_flows:
                _active_flows[provider]['status'] = 'error'
                _active_flows[provider]['error'] = 'state_mismatch'
        return {'error': 'OAuth state mismatch — possible CSRF; start a new login.'}

    # Use the state from the active flow if not explicitly provided
    if not state:
        state = flow_state

    logger.info('[OAuth] Exchanging code for %s tokens (code_len=%d)', provider, len(code))

    with _flows_lock:
        if provider in _active_flows:
            _active_flows[provider]['status'] = 'exchanging'

    from lib.oauth.token_store import OAuthExchangeError
    try:
        if provider == 'claude':
            from lib.oauth.claude import claude_exchange_code
            token = claude_exchange_code(code, pkce_verifier, state=state,
                                         redirect_uri=flow_redirect_uri)
        elif provider == 'codex':
            from lib.oauth.codex import codex_exchange_code
            token = codex_exchange_code(code, pkce_verifier)
        else:
            token = None
    except OAuthExchangeError as e:
        # Surface the REAL upstream reason (e.g. a 403 geo/edge block) instead
        # of the misleading generic "code may have expired".
        with _flows_lock:
            if provider in _active_flows:
                _active_flows[provider]['status'] = 'error'
                _active_flows[provider]['error'] = str(e)
        return {'error': str(e), 'status_code': e.status_code, 'detail': e.detail}

    if token:
        with _flows_lock:
            _active_flows[provider]['status'] = 'success'
            _active_flows[provider]['email'] = token.get('email', '')
        audit_log('oauth_login', provider=provider, email=token.get('email', ''))
        provider_ready = False
        provision_warning = ''
        try:
            from lib.oauth.outbound import provision_oauth_provider
            provision_oauth_provider(provider)
            from lib.oauth.outbound import managed_oauth_provider_status
            provider_ready = managed_oauth_provider_status(
                provider).get('provider_ready', False)
            if provider == 'codex':
                from lib.oauth.codex_catalog import trigger_codex_catalog_refresh
                trigger_codex_catalog_refresh()
        except Exception as e:
            provision_warning = str(e)[:300]
            logger.error('[OAuth] Failed to provision provider for %s: %s',
                         provider, e, exc_info=True)
    else:
        with _flows_lock:
            _active_flows[provider]['status'] = 'error'
            _active_flows[provider]['error'] = 'Token exchange failed'
        return {'error': 'Token exchange failed. The code may have expired.'}

    return {
        'ok': True,
        'provider': provider,
        'email': token.get('email', ''),
        'status': 'success',
        'authenticated': True,
        'provider_ready': provider_ready,
        'warning': provision_warning,
    }


def store_token(provider: str, token_response: dict) -> dict:
    """Persist a token response the BROWSER obtained itself (B1 flow).

    The browser exchanges the auth code against the provider from its own
    (VPN-enabled) network — bypassing the server's geo-blocked egress — and
    POSTs the resulting token JSON here. We validate, persist, and provision
    the managed dispatch provider, exactly like the server-side success path.

    Args:
        provider: 'claude' or 'codex'.
        token_response: Raw JSON the browser received from the token endpoint.

    Returns:
        ``{ok, provider, email, status}`` on success, or ``{error, ...}``.
    """
    from lib.oauth.token_store import OAuthExchangeError
    try:
        if provider == 'claude':
            from lib.oauth.claude import claude_store_token
            token = claude_store_token(token_response)
        elif provider == 'codex':
            from lib.oauth.codex import codex_store_token
            token = codex_store_token(token_response)
        else:
            return {'error': f'Unknown provider: {provider}'}
    except OAuthExchangeError as e:
        with _flows_lock:
            if provider in _active_flows:
                _active_flows[provider]['status'] = 'error'
                _active_flows[provider]['error'] = str(e)
        return {'error': str(e), 'status_code': e.status_code, 'detail': e.detail}

    with _flows_lock:
        if provider in _active_flows:
            _active_flows[provider]['status'] = 'success'
            _active_flows[provider]['email'] = token.get('email', '')
    audit_log('oauth_login', provider=provider, email=token.get('email', ''),
              via='browser_exchange')
    provider_ready = False
    provision_warning = ''
    try:
        from lib.oauth.outbound import provision_oauth_provider
        provision_oauth_provider(provider)
        from lib.oauth.outbound import managed_oauth_provider_status
        provider_ready = managed_oauth_provider_status(
            provider).get('provider_ready', False)
        if provider == 'codex':
            from lib.oauth.codex_catalog import trigger_codex_catalog_refresh
            trigger_codex_catalog_refresh()
    except Exception as e:
        provision_warning = str(e)[:300]
        logger.error('[OAuth] Failed to provision provider for %s: %s',
                     provider, e, exc_info=True)

    return {
        'ok': True,
        'provider': provider,
        'email': token.get('email', ''),
        'status': 'success',
        'authenticated': True,
        'provider_ready': provider_ready,
        'warning': provision_warning,
    }


def logout_oauth(provider: str) -> dict:
    """Logout from an OAuth provider (delete stored token)."""
    from lib.oauth.token_store import delete_token

    deleted = delete_token(provider)

    try:
        from lib.oauth.outbound import deprovision_oauth_provider
        deprovision_oauth_provider(provider)
    except Exception as e:
        logger.error('[OAuth] Failed to deprovision provider for %s: %s',
                     provider, e, exc_info=True)

    with _flows_lock:
        _active_flows.pop(provider, None)

    # Shut down any running relay server
    with _servers_lock:
        old = _active_servers.pop(provider, None)
    if old:
        try:
            old.server_close()
        except Exception as e:
            logger.debug('[OAuth] Error closing relay server for %s: %s', provider, e)

    if not deleted:
        audit_log('oauth_logout_failed', provider=provider,
                  reason='credential_delete_failed')
        logger.error('[OAuth] Runtime session for %s was cleared, but the '
                     'credential file could not be deleted', provider)
        return {
            'ok': False,
            'provider': provider,
            'error': 'credential_delete_failed',
        }

    audit_log('oauth_logout', provider=provider)
    logger.info('[OAuth] Logged out from %s', provider)
    return {'ok': True, 'provider': provider}

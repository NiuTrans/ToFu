"""lib/oauth/manager/_exchange.py — token exchange, store, and logout.

``exchange_code`` (server-side code→token), ``store_token`` (browser-side B1
token persist), and ``logout_oauth`` (delete token + shut relay server).
All flow + server state comes BY REFERENCE from ``._state`` — these
functions mutate the shared dicts in place.
"""

from lib.log import get_logger, audit_log
from lib.error_envelope import make_envelope

from lib.oauth.manager._state import (
    _active_flows,
    _flows_lock,
    _active_servers,
    _servers_lock,
    _update_active_flow,
)

logger = get_logger(__name__)


def _exchange_error_envelope(exc: BaseException) -> dict:
    """Preserve the provider's real OAuth reason in the shared error shape."""
    status_code = int(getattr(exc, 'status_code', 0) or 0)
    if status_code == 429:
        kind = 'ratelimit'
    elif status_code in (401, 403):
        kind = 'permission'
    elif status_code in (400, 404, 409, 422):
        kind = 'bad_request'
    elif status_code == 0:
        kind = 'network'
    else:
        kind = 'upstream_error'
    reason = str(exc)
    detail = str(getattr(exc, 'detail', '') or reason)
    return make_envelope(
        kind,
        message=reason,
        detail=detail,
        context='oauth-exchange',
        source='oauth-manager',
        raw=reason,
    )


def _finalize_login_success(
    provider: str,
    token: dict,
    via: str = '',
    *,
    flow_id: str,
    owner_user_id: int,
) -> dict:
    """Shared success tail for every login path (code exchange, B1 browser
    store, device flow): mark the flow, audit, provision the managed
    provider, and shape the result dict.

    Args:
        provider: 'claude' or 'codex'.
        token: Stored token dict (carries email).
        via: audit trail marker for non-default paths ('browser_exchange',
            'device_flow').
        flow_id: Opaque generation that may receive the terminal status.

    Returns:
        The standard success payload.
    """
    _update_active_flow(
        provider,
        flow_id,
        status='success',
        email=token.get('email', ''),
    )
    audit_kwargs = {'provider': provider, 'email': token.get('email', '')}
    if via:
        audit_kwargs['via'] = via
    audit_log('oauth_login', **audit_kwargs)
    provider_ready = False
    provision_warning = ''
    try:
        from lib.oauth.outbound import provision_oauth_provider
        provision_oauth_provider(provider, owner_user_id=owner_user_id)
        from lib.oauth.outbound import managed_oauth_provider_status
        provider_ready = managed_oauth_provider_status(
            provider, owner_user_id=owner_user_id).get('provider_ready', False)
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


def exchange_code(provider: str, code: str, state: str = '', *,
                  owner_user_id: int, manual: bool = False) -> dict:
    """Exchange an authorization code for tokens.

    Called by the frontend after receiving the code via postMessage
    from the relay page, or via manual paste.

    Args:
        provider: 'claude' or 'codex'.
        code: Authorization code from OAuth callback.
        state: OAuth state parameter for CSRF validation.
        owner_user_id: Authenticated owner completing the flow.
        manual: True only for the explicit manual-paste path. A raw pasted
            code has no channel to echo the flow's state back, so an omitted
            state falls back to the flow's own state; every non-manual
            callback MUST present the exact flow state.

    Returns:
        dict with status info.
    """
    if not code:
        return {'error': 'No authorization code provided'}

    from lib.identity import require_user_id
    owner_user_id = require_user_id(
        owner_user_id, context='OAuth exchange owner')

    # Get PKCE verifier from the active flow. A provider is still a
    # process-global subscription today, but pending authorization state is
    # owner-bound so another authenticated caller cannot consume it or use
    # its desktop egress selection.
    with _flows_lock:
        flow = _active_flows.get(provider, {})
    if flow.get('owner_user_id') != owner_user_id:
        return {'error': 'No active OAuth flow found. Please start a new login first.'}
    pkce = flow.get('pkce', {})
    flow_id = str(flow.get('flow_id') or '')
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
    # their own auth code). Reject rather than exchange.
    if state and flow_state and state != flow_state:
        logger.warning('[OAuth] state mismatch for %s — rejecting code exchange '
                       '(possible CSRF)', provider)
        _update_active_flow(
            provider, flow_id, status='error', error='state_mismatch')
        return {'error': 'OAuth state mismatch — possible CSRF; start a new login.'}

    # A stateless NON-manual callback is rejected outright: the relay page
    # always has the state channel, so only an injected message (any window
    # can postMessage us) arrives without one. Unlike the mismatch branch
    # this does NOT touch the flow's status — the legitimate pending flow is
    # unharmed, and an injected stateless request must not DoS it. The
    # explicit manual-paste path is the one exception: a raw pasted code has
    # no state channel, so it falls back to the flow's own state (a pasted
    # URL / code#state DOES carry a state and is match-checked above).
    if flow_state and not state:
        if not manual:
            logger.warning('[OAuth] stateless non-manual callback for %s — '
                           'rejecting (possible injection)', provider)
            return {'error': 'OAuth state missing — the automatic callback '
                             'always carries one; start a new login.'}
        state = flow_state
    elif not state:
        state = flow_state

    logger.info('[OAuth] Exchanging code for %s tokens (code_len=%d)', provider, len(code))

    _update_active_flow(provider, flow_id, status='exchanging')

    from lib.oauth.token_store import OAuthExchangeError
    try:
        if provider == 'claude':
            from lib.oauth.claude import claude_exchange_code
            token = claude_exchange_code(code, pkce_verifier, state=state,
                                         user_id=str(owner_user_id),
                                         redirect_uri=flow_redirect_uri)
        elif provider == 'codex':
            from lib.oauth.codex import codex_exchange_code
            token = codex_exchange_code(
                code, pkce_verifier, user_id=str(owner_user_id))
        else:
            token = None
    except OAuthExchangeError as e:
        # Surface the REAL upstream reason (e.g. a 403 geo/edge block) instead
        # of the misleading generic "code may have expired".
        envelope = _exchange_error_envelope(e)
        _update_active_flow(
            provider, flow_id, status='error', error=envelope)
        return {'error': envelope, 'status_code': e.status_code, 'detail': e.detail}

    if token:
        return _finalize_login_success(
            provider, token, flow_id=flow_id, owner_user_id=owner_user_id)
    _update_active_flow(
        provider, flow_id, status='error', error='Token exchange failed')
    return {'error': 'Token exchange failed. The code may have expired.'}


def store_token(provider: str, token_response: dict, *,
                owner_user_id: int) -> dict:
    """Persist a token response the BROWSER obtained itself (B1 flow).

    The browser exchanges the auth code against the provider from its own
    (VPN-enabled) network — bypassing the server's geo-blocked egress — and
    POSTs the resulting token JSON here. We validate, persist, and provision
    the managed dispatch provider, exactly like the server-side success path.

    Args:
        provider: 'claude' or 'codex'.
        token_response: Raw JSON the browser received from the token endpoint.
        owner_user_id: Authenticated owner of the active browser flow.

    Returns:
        ``{ok, provider, email, status}`` on success, or ``{error, ...}``.
    """
    if provider not in ('claude', 'codex'):
        return {'error': f'Unknown provider: {provider}'}
    from lib.identity import require_user_id
    owner_user_id = require_user_id(
        owner_user_id, context='OAuth browser exchange owner')
    with _flows_lock:
        flow = _active_flows.get(provider, {})
    if flow.get('owner_user_id') != owner_user_id:
        return {'error': 'No active OAuth flow found. Please start a new login first.'}
    flow_id = str(flow.get('flow_id') or '')

    from lib.oauth.token_store import OAuthExchangeError
    try:
        if provider == 'claude':
            from lib.oauth.claude import claude_store_token
            token = claude_store_token(token_response)
        elif provider == 'codex':
            from lib.oauth.codex import codex_store_token
            token = codex_store_token(token_response)
    except OAuthExchangeError as e:
        envelope = _exchange_error_envelope(e)
        _update_active_flow(
            provider, flow_id, status='error', error=envelope)
        return {'error': envelope, 'status_code': e.status_code, 'detail': e.detail}

    return _finalize_login_success(
        provider, token, via='browser_exchange', flow_id=flow_id,
        owner_user_id=owner_user_id)


def logout_oauth(provider: str, *, owner_user_id: int | None = None) -> dict:
    """Logout and invalidate every projection derived from the credential."""
    from lib.oauth.token_store import delete_token, load_token

    # Capture the account identity before deleting the credential so derived
    # account-scoped caches can be invalidated without ever storing the raw id.
    stored = load_token(provider) or {}
    deleted = delete_token(provider)

    try:
        from lib.oauth.outbound import deprovision_oauth_provider
        deprovision_oauth_provider(
            provider, owner_user_id=owner_user_id)
    except Exception as deprovision_error:
        logger.error('[OAuth] Failed to deprovision provider for %s: %s',
                     provider, deprovision_error, exc_info=True)

    # Signal before removing the flow: ``stop_device_flow`` obtains the
    # cancel_event from that record. Popping first made logout claim success
    # while the old poll thread could continue exchanging a credential.
    from lib.oauth.manager._device import stop_device_flow
    stop_device_flow(provider)
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

    if provider == 'codex':
        # Derived state is invalidated only after credential deletion succeeds;
        # a failed logout leaves the credential live and must preserve it.
        try:
            from lib.subscription_quota import clear_subscription_quota
            clear_subscription_quota(provider, cache_key='oauth_codex')
        except Exception as quota_cleanup_error:
            logger.warning('[OAuth] Codex quota-cache cleanup failed: %s',
                           quota_cleanup_error)
        account_id = str(stored.get('account_id') or '').strip()
        if account_id:
            try:
                from lib.oauth.codex_usage import clear_codex_usage_reset_cache
                clear_codex_usage_reset_cache(account_id=account_id)
            except Exception as reset_cleanup_error:
                logger.warning('[OAuth] Codex reset-cache cleanup failed: %s',
                               reset_cleanup_error)
        else:
            logger.debug('[OAuth] Codex reset-cache cleanup skipped: '
                         'credential had no account identity')

    audit_event = 'oauth_logout'
    audit_log(audit_event, provider=provider)
    logger.info('[OAuth] Logged out from %s', provider)
    return {'ok': True, 'provider': provider}

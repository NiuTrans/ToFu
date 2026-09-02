"""lib/oauth/manager/_device.py — device-authorization login flow (Codex).

The loopback callback (localhost:1455) only works when the browser and the
Tofu server share a machine. For every other deployment — remote server,
container, SSH box — OpenAI's deviceauth API is the only first-party login
path that never touches a localhost redirect: the server mints a user code,
the user enters it at https://auth.openai.com/codex/device in ANY browser
(phone included), and a background poll thread here collects the resulting
authorization code and exchanges it.

Flow state lives in ``_active_flows[provider]`` like every other flow, with
``flow_type='device'`` and a ``cancel_event`` so logout / re-login stops the
poll thread. CLIProxyAPI parity: sdk/auth/codex_device.go.
"""

import threading
import time

from lib.log import get_logger

from lib.oauth.manager._state import _active_flows, _flows_lock

logger = get_logger(__name__)


def start_device_flow(provider: str, user_id: str = '') -> dict:
    """Start a device-authorization login flow.

    Mints a user code from OpenAI and spawns the background poll thread;
    the frontend displays the code and polls the regular status endpoint.

    Args:
        provider: Only 'codex' — OpenAI is the only supported provider with
            a deviceauth API.
        user_id: caller's tenant for egress routing.

    Returns:
        dict with 'user_code', 'verification_url', 'interval',
        'expires_in' — or {'error': ...} when the code could not be minted.
    """
    if provider != 'codex':
        return {'error': f'Device login is not available for {provider}'}

    from lib.oauth.codex import (
        CODEX_OAUTH_CONFIG,
        codex_device_request_user_code,
    )
    from lib.oauth.manager._relay import _close_previous

    # A device flow and a loopback flow cannot coexist for one provider —
    # they share _active_flows[provider]. Stop a previous poll thread and
    # release any relay still bound to the callback port.
    stop_device_flow(provider)
    _close_previous(provider)

    from lib.oauth.token_store import OAuthExchangeError
    try:
        req = codex_device_request_user_code(user_id=user_id)
    except OAuthExchangeError as e:
        return {'error': str(e), 'status_code': e.status_code,
                'detail': e.detail}

    timeout = CODEX_OAUTH_CONFIG['device_flow_timeout']
    cancel = threading.Event()
    verification_url = CODEX_OAUTH_CONFIG['device_verification_url']
    with _flows_lock:
        _active_flows[provider] = {
            'status': 'started',
            'flow_type': 'device',
            'auth_url': verification_url,
            'state': '',
            'pkce': {},
            'started_at': time.time(),
            'expires_at': time.time() + timeout,
            'error': None,
            'email': None,
            'redirect_uri': '',
            'redirect_mode': 'device',
            'exchange': None,
            'device': {
                'device_auth_id': req['device_auth_id'],
                'user_code': req['user_code'],
                'verification_url': verification_url,
                'interval': req['interval'],
            },
            'cancel_event': cancel,
        }

    thread = threading.Thread(
        target=_device_poll_loop,
        args=(provider, req['device_auth_id'], req['user_code'],
              req['interval'], cancel),
        kwargs={'user_id': user_id},
        daemon=True,
        name=f'oauth-device-{provider}',
    )
    thread.start()
    logger.info('[OAuth] Started %s device flow (poll=%ds, ttl=%ds)',
                provider, req['interval'], timeout)
    return {
        'status': 'started',
        'provider': provider,
        'user_code': req['user_code'],
        'verification_url': verification_url,
        'interval': req['interval'],
        'expires_in': timeout,
    }


def stop_device_flow(provider: str) -> None:
    """Signal a running device-flow poll thread to stop (no-op otherwise)."""
    with _flows_lock:
        flow = _active_flows.get(provider)
        cancel = flow.get('cancel_event') if flow else None
    if cancel is not None:
        cancel.set()
        logger.info('[OAuth] Signalled %s device flow to stop', provider)


def _mark(provider: str, status: str, error: str = '') -> None:
    with _flows_lock:
        if provider in _active_flows:
            _active_flows[provider]['status'] = status
            _active_flows[provider]['error'] = error


def _device_poll_loop(provider: str, device_auth_id: str, user_code: str,
                      interval: int, cancel: threading.Event, *,
                      user_id: str = '') -> None:
    """Poll the device-token endpoint until authorized / timeout / cancel."""
    from lib.oauth.codex import (
        CODEX_OAUTH_CONFIG,
        codex_device_poll_token,
        codex_exchange_code,
    )
    from lib.oauth.token_store import OAuthExchangeError

    deadline = time.time() + CODEX_OAUTH_CONFIG['device_flow_timeout']
    _mark(provider, 'waiting_callback')
    try:
        while time.time() < deadline and not cancel.is_set():
            try:
                result = codex_device_poll_token(
                    device_auth_id, user_code, user_id=user_id)
            except OAuthExchangeError as e:
                # A network wobble (status 0) must not kill a 15-minute flow
                # on the first failure; a real upstream rejection must.
                if e.status_code == 0:
                    logger.warning('[OAuth] %s device poll network error, '
                                   'retrying: %s', provider, e)
                    cancel.wait(interval)
                    continue
                logger.error('[OAuth] %s device poll rejected: %s',
                             provider, e)
                _mark(provider, 'error', str(e))
                return
            if result is None:
                cancel.wait(interval)
                continue

            # Authorized — the device flow's PKCE verifier is issued BY the
            # server in the poll response, and the exchange must echo the
            # deviceauth redirect, not the localhost one.
            try:
                token = codex_exchange_code(
                    result['authorization_code'],
                    result['code_verifier'],
                    user_id=user_id,
                    redirect_uri=CODEX_OAUTH_CONFIG['device_redirect_uri'])
            except OAuthExchangeError as e:
                logger.error('[OAuth] %s device exchange failed: %s',
                             provider, e)
                _mark(provider, 'error', str(e))
                return
            if not token:
                _mark(provider, 'error', 'Token exchange failed')
                return
            from lib.oauth.manager._exchange import _finalize_login_success
            _finalize_login_success(provider, token, via='device_flow')
            logger.info('[OAuth] %s device flow completed', provider)
            return

        if not cancel.is_set():
            logger.warning('[OAuth] %s device flow timed out', provider)
            _mark(provider, 'timeout', 'Timeout — device code expired')
    except Exception as e:
        logger.error('[OAuth] %s device poll loop crashed: %s',
                     provider, e, exc_info=True)
        _mark(provider, 'error', 'Device login failed unexpectedly')

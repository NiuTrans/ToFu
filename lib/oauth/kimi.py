"""lib/oauth/kimi.py — Kimi Code membership sign-in (OAuth 2.0 device grant).

Kimi Code is the coding benefit of a Kimi membership (Plus and above). Its
official CLI (MoonshotAI/kimi-cli, MIT; design note KLIP-14) signs a member in
with the RFC 8628 Device Authorization Grant against ``auth.kimi.com`` using
the CLI's public client (no client secret). The access token then rides
``Authorization: Bearer`` on the Kimi Code endpoint
``https://api.kimi.com/coding/v1`` (OpenAI Chat Completions compatible).
CLIProxyAPI (MIT, ``internal/auth/kimi/kimi.go``) runs the same grant.

Identity is honest. Kimi Code's documentation allows third-party tools but
treats a tampered client identifier as a violation, so every request names
Tofu (``User-Agent: Tofu/<version>``, ``X-Msh-Platform: tofu``) and the device
headers the backend requires describe this host. Never copy the official
CLI's platform string or User-Agent here.

Entry points: :func:`kimi_device_request_code`, :func:`kimi_device_poll_token`,
:func:`kimi_save_login_token`, :func:`kimi_refresh_token`,
:func:`kimi_get_valid_token`, :func:`kimi_identity_headers`.
Tokens persist only through ``lib.oauth.token_store``; nothing here logs a
token, a device code or an upstream body.
"""

from __future__ import annotations

import math
import platform
import socket
import time
import uuid

from lib.http_transport.client import http_post
from lib.log import get_logger
from lib.oauth.token_store import OAuthExchangeError, load_token, save_token

logger = get_logger(__name__)

__all__ = [
    'KIMI_OAUTH_CONFIG',
    'DEVICE_PENDING',
    'DEVICE_SLOW_DOWN',
    'kimi_identity_headers',
    'kimi_new_device_id',
    'kimi_device_request_code',
    'kimi_device_poll_token',
    'kimi_save_login_token',
    'kimi_refresh_token',
    'kimi_get_valid_token',
]

# Endpoints and the public client come from KLIP-14 and CLIProxyAPI's
# internal/auth/kimi/kimi.go; the API base is the documented Kimi Code
# OpenAI-compatible base (kimi.com/coding/docs).
KIMI_OAUTH_CONFIG = {
    'provider': 'kimi',
    'client_id': '17e5f671-d194-4dfb-9706-5516cb48c098',
    'device_authorization_url': 'https://auth.kimi.com/api/oauth/device_authorization',
    'token_url': 'https://auth.kimi.com/api/oauth/token',
    'api_base': 'https://api.kimi.com/coding/v1',
    'platform': 'tofu',
    # RFC 8628 §3.2: polling interval when the server omits one.
    'device_poll_interval': 5,
    # Same 15-minute ceiling as the Codex device flow (CLIProxyAPI
    # maxPollDuration); a shorter upstream expires_in wins.
    'device_flow_timeout': 900,
}

#: KLIP-14 refreshes when under five minutes remain (CLIProxyAPI
#: refreshThresholdSeconds = 300).
_TOKEN_REFRESH_BUFFER = 300

_DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
DEVICE_PENDING = 'authorization_pending'
DEVICE_SLOW_DOWN = 'slow_down'
#: Token-endpoint error codes after which a refresh token is never replayed.
_TERMINAL_REFRESH_ERRORS = frozenset({
    'invalid_grant', 'invalid_refresh_token', 'unauthorized_client',
})


def _header_text(value: object, fallback: str) -> str:
    text = ''.join(ch for ch in str(value or '') if 32 <= ord(ch) < 127).strip()
    return text[:128] or fallback


def kimi_new_device_id() -> str:
    """A fresh device identifier; persisted with the token it signs in."""
    return str(uuid.uuid4())


def kimi_identity_headers(device_id: str) -> dict[str, str]:
    """Honest Tofu identity plus the device facts Kimi's backend requires."""
    import lib as _lib
    version = _header_text(getattr(_lib, '__version__', ''), '0.0.0-dev')
    model = ' '.join(part for part in (
        platform.system(), platform.release(), platform.machine()) if part)
    return {
        'User-Agent': f'Tofu/{version}',
        'X-Msh-Platform': KIMI_OAUTH_CONFIG['platform'],
        'X-Msh-Version': version,
        'X-Msh-Device-Name': _header_text(
            platform.node() or socket.gethostname(), 'unknown'),
        'X-Msh-Device-Model': _header_text(model, 'unknown'),
        'X-Msh-Os-Version': _header_text(platform.version(), 'unknown'),
        'X-Msh-Device-Id': _header_text(device_id, 'unknown'),
    }


def _post_form(url: str, form: dict, *, device_id: str):
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        **kimi_identity_headers(device_id),
    }
    return http_post(url, data=form, headers=headers, timeout=30)


def _json_object(resp) -> dict:
    try:
        data = resp.json()
    except Exception as exc:
        logger.debug('[Kimi OAuth] non-JSON response (HTTP %s): %s',
                     getattr(resp, 'status_code', '?'), type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _oauth_error(data: dict) -> tuple[str, str]:
    error = data.get('error')
    if isinstance(error, dict):
        code = error.get('code') or error.get('type') or ''
        description = error.get('message') or ''
    else:
        code = error or ''
        description = data.get('error_description') or ''
    return (str(code).strip().lower()[:64],
            _header_text(description, '')[:200])


def _positive_seconds(value: object) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(seconds) or seconds <= 0:
        return 0
    return int(seconds)


def _https_url(value: object) -> str:
    text = str(value or '').strip()
    return text if text.startswith('https://') and len(text) <= 2048 else ''


def kimi_device_request_code(*, device_id: str) -> dict:
    """Mint a device code (RFC 8628 §3.1–3.2).

    Returns ``device_code`` (server-side only), ``user_code``,
    ``verification_url`` (the complete URL when offered), ``interval`` and
    ``expires_in``. Raises :class:`OAuthExchangeError`; ``status_code=0``
    means the service could not be reached.
    """
    try:
        resp = _post_form(KIMI_OAUTH_CONFIG['device_authorization_url'],
                          {'client_id': KIMI_OAUTH_CONFIG['client_id']},
                          device_id=device_id)
    except Exception as exc:
        logger.warning('[Kimi OAuth] device authorization unreachable: %s',
                       type(exc).__name__)
        raise OAuthExchangeError(
            'Could not reach Kimi sign-in (auth.kimi.com)', status_code=0) from exc
    data = _json_object(resp)
    if not 200 <= resp.status_code < 300:
        code, description = _oauth_error(data)
        logger.warning('[Kimi OAuth] device authorization HTTP %d (%s)',
                       resp.status_code, code or 'no error code')
        raise OAuthExchangeError(
            'Kimi sign-in refused the device request (HTTP %d%s)' % (
                resp.status_code, f': {code}' if code else ''),
            status_code=resp.status_code, detail=description or code)
    device_code = str(data.get('device_code') or '').strip()
    user_code = str(data.get('user_code') or '').strip()
    verification_url = (_https_url(data.get('verification_uri_complete'))
                        or _https_url(data.get('verification_uri')))
    if not device_code or not user_code or not verification_url:
        raise OAuthExchangeError(
            'Kimi sign-in did not return a usable device code',
            status_code=resp.status_code)
    interval = (_positive_seconds(data.get('interval'))
                or KIMI_OAUTH_CONFIG['device_poll_interval'])
    expires_in = min(_positive_seconds(data.get('expires_in'))
                     or KIMI_OAUTH_CONFIG['device_flow_timeout'],
                     KIMI_OAUTH_CONFIG['device_flow_timeout'])
    logger.info('[Kimi OAuth] device flow started (interval=%ds, ttl=%ds)',
                interval, expires_in)
    return {'device_code': device_code, 'user_code': user_code,
            'verification_url': verification_url, 'interval': interval,
            'expires_in': expires_in}


def _token_record(data: dict, *, device_id: str) -> dict:
    expires_in = _positive_seconds(data.get('expires_in'))
    return {
        'type': 'kimi',
        'access_token': str(data.get('access_token') or ''),
        'refresh_token': str(data.get('refresh_token') or ''),
        'token_type': str(data.get('token_type') or 'Bearer'),
        'scope': str(data.get('scope') or ''),
        # Unknown lifetime is recorded as 0: the token is used until the
        # API rejects it, and the 401 path forces a refresh.
        'expire': time.time() + expires_in if expires_in else 0,
        'expires_in': expires_in,
        'device_id': device_id,
        'email': '',
    }


def kimi_device_poll_token(device_code: str, *, device_id: str) -> dict | str:
    """One token-endpoint poll (RFC 8628 §3.4–3.5).

    Returns the unsaved token record on success, or :data:`DEVICE_PENDING` /
    :data:`DEVICE_SLOW_DOWN`. The caller saves the record only while its
    login generation is current. Raises :class:`OAuthExchangeError` for a
    denied or expired code; ``status_code=0`` marks a transient failure the
    caller may poll through.
    """
    try:
        resp = _post_form(KIMI_OAUTH_CONFIG['token_url'], {
            'client_id': KIMI_OAUTH_CONFIG['client_id'],
            'device_code': device_code,
            'grant_type': _DEVICE_GRANT,
        }, device_id=device_id)
    except Exception as exc:
        raise OAuthExchangeError(
            'Could not reach Kimi sign-in (auth.kimi.com)', status_code=0) from exc
    data = _json_object(resp)
    code, description = _oauth_error(data)
    # Kimi answers pending polls with HTTP 200 and an error field; standard
    # RFC 8628 servers use 400. Both carry the same error vocabulary.
    if code == DEVICE_PENDING:
        return DEVICE_PENDING
    if code == DEVICE_SLOW_DOWN:
        return DEVICE_SLOW_DOWN
    status = resp.status_code
    if code == 'expired_token':
        raise OAuthExchangeError(
            'The Kimi device code expired — start the sign-in again',
            status_code=status or 400, detail=code)
    if code == 'access_denied':
        raise OAuthExchangeError(
            'Kimi sign-in was denied in the browser', status_code=status or 400,
            detail=code)
    if code:
        raise OAuthExchangeError(
            f'Kimi sign-in failed ({code})', status_code=status or 400,
            detail=description or code)
    if status == 429 or status >= 500:
        raise OAuthExchangeError(
            'Kimi sign-in is temporarily unavailable (HTTP %d)' % status,
            status_code=0)
    if not 200 <= status < 300 or not data.get('access_token'):
        raise OAuthExchangeError(
            'Kimi sign-in returned no access token (HTTP %d)' % status,
            status_code=status)
    return _token_record(data, device_id=device_id)


def kimi_save_login_token(record: dict) -> dict:
    """Commit a completed login after any in-flight refresh, never under it."""
    from lib.json_store import locked_path
    from lib.oauth.token_store import token_path
    with locked_path(token_path('kimi') + '.refresh'):
        saved = save_token('kimi', record)
    if not saved:
        raise OAuthExchangeError(
            'Kimi authorized successfully, but the credentials could not be '
            'saved securely. Check data-directory permissions or free disk '
            'space.', status_code=500)
    logger.info('[Kimi OAuth] login stored (expires_in=%ss)',
                record.get('expires_in') or 'unknown')
    return record


def kimi_refresh_token(refresh_tok: str = '', user_id: str = '') -> dict | None:
    """Refresh the stored login; concurrent refreshes merge (single-use RT).

    ``user_id`` is accepted for parity with the other subscription
    providers; Kimi's endpoints are reached directly.
    """
    stored = load_token('kimi') or {}
    refresh_tok = refresh_tok or stored.get('refresh_token', '')
    if not refresh_tok:
        logger.info('[Kimi OAuth] no refresh token available')
        return None
    from lib.oauth.token_store import refresh_singleflight, token_path
    return refresh_singleflight(
        'kimi', refresh_tok, _kimi_refresh_upstream,
        load=lambda: load_token('kimi'),
        lock_path=token_path('kimi') + '.refresh')


def _invalidate_rejected_refresh_token(refresh_tok: str, reason: str) -> None:
    stored = load_token('kimi') or {}
    if stored.get('refresh_token') != refresh_tok:
        return
    stored['refresh_token'] = ''
    stored['refresh_invalidated_at'] = time.time()
    stored['refresh_invalidated_reason'] = reason[:64]
    if not save_token('kimi', stored):
        logger.error('[Kimi OAuth] could not persist the refresh rejection')


def _kimi_refresh_upstream(refresh_tok: str) -> dict | None:
    """The upstream refresh, called under the singleflight lock."""
    stored = load_token('kimi') or {}
    device_id = stored.get('device_id') or kimi_new_device_id()
    # Same three-attempt refresh budget as the Codex and Claude logins.
    for attempt in range(3):
        try:
            resp = _post_form(KIMI_OAUTH_CONFIG['token_url'], {
                'client_id': KIMI_OAUTH_CONFIG['client_id'],
                'grant_type': 'refresh_token',
                'refresh_token': refresh_tok,
            }, device_id=device_id)
        except Exception as exc:
            logger.warning('[Kimi OAuth] refresh unreachable (attempt %d): %s',
                           attempt + 1, type(exc).__name__)
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        data = _json_object(resp)
        code, _description = _oauth_error(data)
        if resp.status_code in (401, 403) or code in _TERMINAL_REFRESH_ERRORS:
            logger.warning('[Kimi OAuth] refresh rejected (HTTP %d, %s); '
                           'sign-in required', resp.status_code,
                           code or 'no error code')
            _invalidate_rejected_refresh_token(
                refresh_tok, code or f'http_{resp.status_code}')
            return None
        if resp.status_code != 200 or not data.get('access_token'):
            logger.warning('[Kimi OAuth] refresh failed (HTTP %d, attempt %d)',
                           resp.status_code, attempt + 1)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None
        renewed = _token_record(data, device_id=device_id)
        current = load_token('kimi') or {}
        current.update({
            'access_token': renewed['access_token'],
            'refresh_token': renewed['refresh_token'] or refresh_tok,
            'expire': renewed['expire'],
            'expires_in': renewed['expires_in'],
            'device_id': device_id,
            'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        })
        if renewed['scope']:
            current['scope'] = renewed['scope']
        current.pop('refresh_invalidated_at', None)
        current.pop('refresh_invalidated_reason', None)
        if not save_token('kimi', current):
            # The old refresh token may already be consumed; never retry it.
            logger.error('[Kimi OAuth] refreshed token could not be persisted')
            return None
        logger.info('[Kimi OAuth] token refreshed (expires_in=%ss)',
                    renewed['expires_in'] or 'unknown')
        return current
    return None


def _expiry(token: dict) -> float:
    try:
        value = float(token.get('expire') or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def kimi_get_valid_token(user_id: str = '') -> str | None:
    """A usable access token, refreshing when under five minutes remain.

    A failed refresh never returns a token past its recorded expiry: the
    caller then raises the typed sign-in-required error.
    """
    stored = load_token('kimi')
    if not stored or not stored.get('access_token'):
        return None
    expire = _expiry(stored)
    if not expire or time.time() <= expire - _TOKEN_REFRESH_BUFFER:
        return stored['access_token']
    if not stored.get('refresh_invalidated_at'):
        refreshed = kimi_refresh_token(stored.get('refresh_token', ''),
                                       user_id=user_id)
        if refreshed and refreshed.get('access_token'):
            return refreshed['access_token']
    # Refresh can outlive expiry, logout or a replacement login: re-read
    # both the credential and the clock instead of trusting the snapshot.
    current = load_token('kimi') or {}
    current_expire = _expiry(current)
    if current.get('access_token') and (
            not current_expire or time.time() < current_expire):
        return current['access_token']
    logger.warning('[Kimi OAuth] access token expired and refresh is '
                   'unavailable; sign-in required')
    return None

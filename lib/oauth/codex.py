"""lib/oauth/codex.py — OpenAI Codex (ChatGPT Plus) OAuth PKCE authentication.

OAuth flow is identical to Claude, but the API usage is different:
  • URL: chatgpt.com/backend-api/codex/responses (NOT api.openai.com/v1)
  • Format: Responses API (NOT Chat Completions)

The Chat Completions ↔ Responses API translation layer was EXTRACTED to
``lib/llm/responses_outbound/`` (2026-07-31, epic pt_b7a29ea7) — this module
keeps only the OAuth flow + re-export facades for the legacy names
(``codex_translate_request`` / ``CodexSSETranslator`` /
``codex_translate_sse_event``).
"""

import base64
import json
import time
import uuid

import requests

from lib.log import get_logger
from lib.oauth.pkce import generate_pkce_codes
from lib.oauth.token_store import load_token, save_token, OAuthExchangeError
from lib.http_client import http_post

logger = get_logger(__name__)

__all__ = [
    'CODEX_OAUTH_CONFIG',
    'codex_build_auth_url',
    'codex_exchange_code',
    'codex_store_token',
    'codex_refresh_token',
    'codex_get_valid_token',
    'codex_translate_request',
    'codex_translate_sse_event',
]

# ══════════════════════════════════════════════════════════
#  OAuth Configuration Constants
#  (from CLIProxyAPI v6.9.10 / OpenAI Codex CLI official)
# ══════════════════════════════════════════════════════════

CODEX_OAUTH_CONFIG = {
    'auth_url': 'https://auth.openai.com/oauth/authorize',
    'token_url': 'https://auth.openai.com/oauth/token',
    'client_id': 'app_EMoamEEZ73f0CkXaXp7hrann',
    'callback_port': 1455,
    'redirect_uri': 'http://localhost:1455/auth/callback',
    'scope': 'openid email profile offline_access',
    'provider': 'codex',
    'api_base': 'https://chatgpt.com/backend-api/codex',
}

_TOKEN_REFRESH_BUFFER = 300  # 5 minutes


def _oauth_http_post(url: str, payload: dict, *, timeout: float = 30,
                     user_id: str = ''):
    """Token-endpoint POST (form-encoded) — direct when reachable, desktop
    egress otherwise. Mirrors lib/oauth/claude.py's helper; raises
    ``EgressUnavailable`` when direct is blocked AND no agent is online."""
    from lib.desktop import egress as _eg
    route = _eg.route_request(url, user_id=user_id)
    if route == 'direct':
        return http_post(
            url, data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=timeout)
    from urllib.parse import urlencode
    return _eg.egress_http(
        url, method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        body=urlencode(payload).encode(),
        timeout=timeout, user_id=user_id)


def codex_build_auth_url() -> dict:
    """Build the Codex OAuth authorization URL with PKCE.

    Returns:
        dict with 'auth_url', 'state', 'pkce', 'callback_port', 'provider'.
    """
    pkce = generate_pkce_codes()
    state = uuid.uuid4().hex

    params = {
        'response_type': 'code',
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
        'redirect_uri': CODEX_OAUTH_CONFIG['redirect_uri'],
        'scope': CODEX_OAUTH_CONFIG['scope'],
        'state': state,
        'code_challenge': pkce['code_challenge'],
        'code_challenge_method': 'S256',
        'audience': 'https://api.openai.com/v1',
    }

    query = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}' for k, v in params.items())
    auth_url = f"{CODEX_OAUTH_CONFIG['auth_url']}?{query}"

    logger.info('[Codex OAuth] Built auth URL (state=%s)', state[:8])
    return {
        'auth_url': auth_url,
        'state': state,
        'pkce': pkce,
        'callback_port': CODEX_OAUTH_CONFIG['callback_port'],
        'provider': 'codex',
        # Params for browser-side token exchange (B1 flow).
        'exchange': {
            'token_url': CODEX_OAUTH_CONFIG['token_url'],
            'client_id': CODEX_OAUTH_CONFIG['client_id'],
            'redirect_uri': CODEX_OAUTH_CONFIG['redirect_uri'],
            'code_verifier': pkce['code_verifier'],
            'state': state,
            'style': 'form',  # OpenAI token endpoint expects form-urlencoded
        },
    }


def codex_exchange_code(code: str, pkce_verifier: str,
                        user_id: str = '') -> dict | None:
    """Exchange authorization code for Codex tokens.

    Args:
        code: Authorization code from OAuth callback.
        pkce_verifier: PKCE code verifier.

    Returns:
        Token dict or None on failure.
    """
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': CODEX_OAUTH_CONFIG['redirect_uri'],
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
        'code_verifier': pkce_verifier,
    }

    try:
        token_url = CODEX_OAUTH_CONFIG['token_url']
        resp = _oauth_http_post(token_url, payload, timeout=30,
                                user_id=user_id)

        if resp.status_code != 200:
            logger.error('[Codex OAuth] Token exchange failed (HTTP %d): %.500s',
                         resp.status_code, resp.text)
            raise OAuthExchangeError(
                _explain_exchange_failure(resp.status_code, resp.text, 'codex'),
                status_code=resp.status_code,
                detail=resp.text[:500],
            )

        data = resp.json()
        access_token = data.get('access_token', '')
        refresh_token = data.get('refresh_token', '')
        id_token = data.get('id_token', '')
        expires_in = data.get('expires_in', 3600)

        if not access_token:
            logger.error('[Codex OAuth] No access_token in response')
            raise OAuthExchangeError(
                'OpenAI returned no access_token', status_code=resp.status_code)

        # Parse JWT to get account info + subscription plan
        email, account_id, plan_type = _parse_jwt_claims(id_token)

        token_data = {
            'type': 'codex',
            'access_token': access_token,
            'refresh_token': refresh_token,
            'id_token': id_token,
            'account_id': account_id,
            'email': email,
            'plan_type': plan_type,
            'expire': time.time() + expires_in,
            'expires_in': expires_in,
        }

        save_token('codex', token_data)
        logger.info('[Codex OAuth] Token exchange successful (email=%s, account=%s, expires_in=%ds)',
                     email, account_id[:8] if account_id else '?', expires_in)
        return token_data

    except OAuthExchangeError:
        raise
    except Exception as e:
        from lib.desktop.egress import EgressUnavailable
        if isinstance(e, EgressUnavailable):
            logger.error('[Codex OAuth] egress unavailable: %s', e)
            raise OAuthExchangeError(str(e), status_code=0) from e
        logger.error('[Codex OAuth] Token exchange error: %s', e, exc_info=True)
        raise OAuthExchangeError(
            'Network error reaching OpenAI: %s' % e, status_code=0) from e


def codex_store_token(data: dict) -> dict:
    """Persist a token response the BROWSER already obtained (B1 flow).

    Args:
        data: Raw JSON response from OpenAI's token endpoint.

    Returns:
        The stored token dict.

    Raises:
        OAuthExchangeError: when the response carries no access_token.
    """
    if not isinstance(data, dict):
        raise OAuthExchangeError('Invalid token response (not an object)', status_code=0)
    access_token = data.get('access_token', '')
    if not access_token:
        raise OAuthExchangeError(
            'Token response from the browser contained no access_token',
            status_code=0, detail=json.dumps(data)[:300])
    id_token = data.get('id_token', '')
    expires_in = data.get('expires_in', 3600)
    email, account_id, plan_type = _parse_jwt_claims(id_token)
    token_data = {
        'type': 'codex',
        'access_token': access_token,
        'refresh_token': data.get('refresh_token', ''),
        'id_token': id_token,
        'account_id': account_id,
        'email': email,
        'plan_type': plan_type,
        'expire': time.time() + expires_in,
        'expires_in': expires_in,
    }
    save_token('codex', token_data)
    logger.info('[Codex OAuth] Stored browser-exchanged token (email=%s, account=%s, expires_in=%ds)',
                email, account_id[:8] if account_id else '?', expires_in)
    return token_data


def codex_refresh_token(refresh_tok: str = None,
                        user_id: str = '') -> dict | None:
    """Refresh the Codex access token.

    Args:
        refresh_tok: Refresh token. If None, loads from stored token.
        user_id: caller's tenant for egress routing.

    Returns:
        Updated token dict or None.
    """
    if not refresh_tok:
        stored = load_token('codex')
        if not stored:
            logger.warning('[Codex OAuth] No stored token to refresh')
            return None
        refresh_tok = stored.get('refresh_token', '')

    if not refresh_tok:
        logger.warning('[Codex OAuth] No refresh token available')
        return None

    # Singleflight: refresh tokens are single-use — concurrent refreshes of
    # the SAME token merge into one upstream call (see claude.py).
    from lib.oauth.token_store import refresh_singleflight
    return refresh_singleflight(
        'codex', refresh_tok,
        lambda rt: _codex_refresh_upstream(rt, user_id=user_id),
        load=lambda: load_token('codex'))


def _codex_refresh_upstream(refresh_tok: str, *, user_id: str = '') -> dict | None:
    """The actual upstream refresh (called under the singleflight lock)."""
    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_tok,
        'client_id': CODEX_OAUTH_CONFIG['client_id'],
    }

    for attempt in range(3):
        try:
            token_url = CODEX_OAUTH_CONFIG['token_url']
            resp = _oauth_http_post(token_url, payload, timeout=30,
                                    user_id=user_id)

            if resp.status_code != 200:
                logger.warning('[Codex OAuth] Refresh failed (HTTP %d, attempt %d): %.300s',
                               resp.status_code, attempt + 1, resp.text)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None

            data = resp.json()
            access_token = data.get('access_token', '')
            new_refresh = data.get('refresh_token', refresh_tok)
            id_token = data.get('id_token', '')
            expires_in = data.get('expires_in', 3600)

            if not access_token:
                logger.error('[Codex OAuth] No access_token in refresh response')
                return None

            email, account_id, plan_type = _parse_jwt_claims(id_token)

            stored = load_token('codex') or {}
            old_plan = stored.get('plan_type', '')
            stored.update({
                'access_token': access_token,
                'refresh_token': new_refresh,
                'id_token': id_token,
                'account_id': account_id or stored.get('account_id', ''),
                'email': email or stored.get('email', ''),
                'plan_type': plan_type or old_plan,
                'expire': time.time() + expires_in,
                'expires_in': expires_in,
                'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            save_token('codex', stored)
            if plan_type and plan_type != old_plan:
                # Plan changed on refresh (upgrade/downgrade) — re-gate the
                # managed provider's model table (idempotent).
                try:
                    from lib.oauth.outbound import provision_oauth_provider
                    provision_oauth_provider('codex', plan_type=plan_type)
                    logger.info('[Codex OAuth] Plan changed %s → %s, re-provisioned',
                                old_plan or '?', plan_type)
                except Exception as e:
                    logger.warning('[Codex OAuth] plan-change re-provision failed: %s', e)
            logger.info('[Codex OAuth] Token refreshed (expires_in=%ds)', expires_in)
            return stored

        except Exception as e:
            from lib.desktop.egress import EgressUnavailable
            if isinstance(e, EgressUnavailable):
                logger.warning('[Codex OAuth] refresh egress unavailable: %s', e)
                return None
            logger.warning('[Codex OAuth] Refresh error (attempt %d): %s', attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return None


def codex_get_valid_token(user_id: str = '') -> str | None:
    """Get a valid Codex access token, refreshing if needed."""
    stored = load_token('codex')
    if not stored:
        return None

    access_token = stored.get('access_token', '')
    expire = stored.get('expire', 0)

    if not access_token:
        return None

    if time.time() > expire - _TOKEN_REFRESH_BUFFER:
        logger.info('[Codex OAuth] Token expiring soon, refreshing…')
        refreshed = codex_refresh_token(stored.get('refresh_token', ''),
                                        user_id=user_id)
        if refreshed:
            return refreshed.get('access_token')
        logger.warning('[Codex OAuth] Refresh failed, using potentially expired token')

    return access_token


# ══════════════════════════════════════════════════════════
#  Request Translator: Chat Completions → Responses API
#  EXTRACTED to lib/llm/responses_outbound/ (2026-07-31, epic pt_b7a29ea7)
#  — the shared boundary for EVERY Responses-speaking provider. The Codex
#  OAuth path is now just the ``profile='codex'`` caller; these re-exports
#  keep the legacy import surface working. Semantics changes belong in
#  lib/llm/responses_outbound/, NOT here.
# ══════════════════════════════════════════════════════════

from lib.llm.responses_outbound import (  # noqa: E402  (re-export facade)
    ResponsesSSETranslator as CodexSSETranslator,
    openai_body_to_responses,
)


def codex_translate_request(body: dict) -> dict:
    """Translate Chat Completions request body to Responses API format.

    Back-compat wrapper — the implementation lives in
    ``lib.llm.responses_outbound.openai_body_to_responses``
    (``profile='codex'``). Returns the body ONLY; callers needing the
    truncation reverse map call the underlying converter directly.
    """
    out, _reverse = openai_body_to_responses(body, profile='codex',
                                             stream=True)
    return out


# ══════════════════════════════════════════════════════════
#  Response Translator: Responses API SSE → Chat Completions SSE
#  EXTRACTED to lib/llm/responses_outbound/_sse.py — see the note above.
# ══════════════════════════════════════════════════════════


def codex_translate_sse_event(raw_line: str, translator: CodexSSETranslator) -> list[str]:
    """Convenience wrapper around ResponsesSSETranslator.translate().

    The unified translator emits chunk DICTS; this wrapper preserves the
    legacy string-returning contract for external callers.
    """
    return [json.dumps(c, ensure_ascii=False) if isinstance(c, dict) else c
            for c in translator.translate(raw_line)]


# ── Internal helpers ──

def _explain_exchange_failure(status: int, body: str, provider: str) -> str:
    """Turn an upstream non-200 token-exchange response into a clear message.

    A 403 ``unsupported_country_region_territory`` from OpenAI is a region
    block on the SERVER's egress IP, not a bad code.
    """
    upstream = ''
    try:
        parsed = json.loads(body) if body else {}
        err = parsed.get('error', parsed)
        if isinstance(err, dict):
            upstream = err.get('message') or err.get('error_description') or err.get('type') or ''
        elif isinstance(err, str):
            upstream = err
    except Exception as e:
        logger.debug('[Codex OAuth] error-body parse failed, using raw prefix: %s', e)
        upstream = (body or '')[:200]

    if status == 403:
        return ('OpenAI refused the token exchange (HTTP 403: %s). This is a '
                'region block on the SERVER\u2019s network \u2014 not an expired code. '
                'This server cannot reach OpenAI\u2019s token endpoint from its '
                'current network.' % (upstream or 'unsupported_country_region_territory'))
    if status in (400, 401):
        return ('OpenAI rejected the authorization code (HTTP %d: %s). The code may '
                'have expired or already been used \u2014 start a fresh login.'
                % (status, upstream or 'invalid_grant'))
    if status == 0:
        return upstream or 'Could not reach OpenAI.'
    return 'Token exchange failed (HTTP %d: %s).' % (status, upstream or 'unknown error')


def _parse_jwt_claims(id_token: str) -> tuple[str, str, str]:
    """Parse JWT ID token to extract email, account_id and subscription plan.

    Returns:
        (email, account_id, plan_type) triple — plan_type is OpenAI's
        ``chatgpt_plan_type`` claim (free/plus/pro/team/business/…), '' when
        absent. Drives the managed provider's model gating (CLIProxyAPI
        parity).
    """
    if not id_token:
        return '', '', ''
    try:
        parts = id_token.split('.')
        if len(parts) < 2:
            return '', '', ''
        payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        email = claims.get('email', '')
        # OpenAI stores account info in custom claim
        auth_info = claims.get('https://api.openai.com/auth', {})
        account_id = auth_info.get('chatgpt_account_id', claims.get('sub', ''))
        plan_type = auth_info.get('chatgpt_plan_type', '')
        return email, account_id, plan_type
    except Exception as e:
        logger.debug('[Codex OAuth] Failed to parse JWT: %s', e)
        return '', '', ''

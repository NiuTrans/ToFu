"""lib/oauth/claude.py — Claude (Anthropic) OAuth PKCE authentication.

OAuth flow (console callback):
  1. Generate PKCE codes + state
  2. Build auth URL → user opens in popup browser
  3. User authenticates on claude.ai
  4. Redirect to console.anthropic.com/oauth/code/callback which shows code#state
  5. User copies code#state and pastes into Tofu input box
  6. Exchange code for access_token / refresh_token via console.anthropic.com/v1/oauth/token
  7. Token is a standard Anthropic API key (sk-ant-oat01-...)
     → use with Authorization: Bearer header on api.anthropic.com/v1/messages

Important: Anthropic uses JSON (not form-urlencoded) for token exchange.
The redirect_uri must be the registered console callback URL.
"""

import json
import time
import uuid

import requests

from lib.log import get_logger
from lib.oauth.pkce import generate_pkce_codes
from lib.oauth.token_store import load_token, save_token
from lib.http_client import http_post

logger = get_logger(__name__)

__all__ = [
    'CLAUDE_OAUTH_CONFIG',
    'claude_build_auth_url',
    'claude_exchange_code',
    'claude_refresh_token',
    'claude_get_valid_token',
]

# ══════════════════════════════════════════════════════════
#  OAuth Configuration Constants
#  (from CLIProxyAPI v6.9.10 / Claude Code official client)
# ══════════════════════════════════════════════════════════

CLAUDE_OAUTH_CONFIG = {
    'auth_url': 'https://claude.ai/oauth/authorize',
    'token_url': 'https://console.anthropic.com/v1/oauth/token',
    'client_id': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
    'callback_port': 54545,  # kept for relay server (auto-callback)
    'redirect_uri': 'https://console.anthropic.com/oauth/code/callback',
    'redirect_uri_local': 'http://localhost:54545/callback',  # for relay server auto-callback
    'scope': 'org:create_api_key user:profile user:inference',
    'provider': 'claude',
}

# Access token validity — refresh if less than this many seconds remain
_TOKEN_REFRESH_BUFFER = 300  # 5 minutes


def claude_build_auth_url() -> dict:
    """Build the Claude OAuth authorization URL with PKCE.

    Returns:
        dict with 'auth_url', 'state', 'pkce' (verifier/challenge),
        and 'callback_port'.
    """
    pkce = generate_pkce_codes()
    state = uuid.uuid4().hex

    params = {
        'code': 'true',  # tell Anthropic to return code on the callback page
        'response_type': 'code',
        'client_id': CLAUDE_OAUTH_CONFIG['client_id'],
        'redirect_uri': CLAUDE_OAUTH_CONFIG['redirect_uri'],
        'scope': CLAUDE_OAUTH_CONFIG['scope'],
        'state': state,
        'code_challenge': pkce['code_challenge'],
        'code_challenge_method': 'S256',
    }

    # Build URL manually to avoid encoding issues
    query = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}' for k, v in params.items())
    auth_url = f"{CLAUDE_OAUTH_CONFIG['auth_url']}?{query}"

    logger.info('[Claude OAuth] Built auth URL (state=%s)', state[:8])
    return {
        'auth_url': auth_url,
        'state': state,
        'pkce': pkce,
        'callback_port': CLAUDE_OAUTH_CONFIG['callback_port'],
        'provider': 'claude',
    }


def claude_exchange_code(code: str, pkce_verifier: str, state: str = '') -> dict | None:
    """Exchange authorization code for tokens.

    Args:
        code: Authorization code from OAuth callback.
        pkce_verifier: The PKCE code verifier used in the auth request.
        state: The OAuth state parameter (for CSRF validation).

    Returns:
        Token dict with access_token, refresh_token, email, expire, etc.
        None on failure.
    """
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'state': state,
        'redirect_uri': CLAUDE_OAUTH_CONFIG['redirect_uri'],
        'client_id': CLAUDE_OAUTH_CONFIG['client_id'],
        'code_verifier': pkce_verifier,
    }

    try:
        token_url = CLAUDE_OAUTH_CONFIG['token_url']
        resp = http_post(
            token_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error('[Claude OAuth] Token exchange failed (HTTP %d): %.500s',
                         resp.status_code, resp.text)
            return None

        data = resp.json()
        access_token = data.get('access_token', '')
        refresh_token = data.get('refresh_token', '')
        expires_in = data.get('expires_in', 28800)  # default 8 hours

        if not access_token:
            logger.error('[Claude OAuth] No access_token in response')
            return None

        # Build token storage
        token_data = {
            'type': 'claude',
            'access_token': access_token,
            'refresh_token': refresh_token,
            'expire': time.time() + expires_in,
            'expires_in': expires_in,
            'email': _extract_email_from_token(data),
            'id_token': data.get('id_token', ''),
        }

        save_token('claude', token_data)
        logger.info('[Claude OAuth] Token exchange successful (email=%s, expires_in=%ds)',
                     token_data['email'], expires_in)
        return token_data

    except Exception as e:
        logger.error('[Claude OAuth] Token exchange error: %s', e, exc_info=True)
        return None


def claude_refresh_token(refresh_tok: str = None) -> dict | None:
    """Refresh the Claude access token using the refresh token.

    Args:
        refresh_tok: Refresh token string. If None, loads from stored token.

    Returns:
        Updated token dict, or None on failure.
    """
    if not refresh_tok:
        stored = load_token('claude')
        if not stored:
            logger.warning('[Claude OAuth] No stored token to refresh')
            return None
        refresh_tok = stored.get('refresh_token', '')

    if not refresh_tok:
        logger.warning('[Claude OAuth] No refresh token available')
        return None

    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_tok,
        'client_id': CLAUDE_OAUTH_CONFIG['client_id'],
    }

    for attempt in range(3):
        try:
            token_url = CLAUDE_OAUTH_CONFIG['token_url']
            resp = http_post(
                token_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning('[Claude OAuth] Refresh failed (HTTP %d, attempt %d): %.300s',
                               resp.status_code, attempt + 1, resp.text)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None

            data = resp.json()
            access_token = data.get('access_token', '')
            new_refresh = data.get('refresh_token', refresh_tok)
            expires_in = data.get('expires_in', 28800)

            if not access_token:
                logger.error('[Claude OAuth] No access_token in refresh response')
                return None

            # Update stored token
            stored = load_token('claude') or {}
            stored.update({
                'access_token': access_token,
                'refresh_token': new_refresh,
                'expire': time.time() + expires_in,
                'expires_in': expires_in,
                'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            save_token('claude', stored)
            logger.info('[Claude OAuth] Token refreshed (expires_in=%ds)', expires_in)
            return stored

        except Exception as e:
            logger.warning('[Claude OAuth] Refresh error (attempt %d): %s',
                           attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return None


def claude_get_valid_token() -> str | None:
    """Get a valid Claude access token, refreshing if needed.

    Returns:
        Access token string, or None if not authenticated.
    """
    stored = load_token('claude')
    if not stored:
        return None

    access_token = stored.get('access_token', '')
    expire = stored.get('expire', 0)

    if not access_token:
        return None

    # Check if token needs refresh
    if time.time() > expire - _TOKEN_REFRESH_BUFFER:
        logger.info('[Claude OAuth] Token expiring soon, refreshing…')
        refreshed = claude_refresh_token(stored.get('refresh_token', ''))
        if refreshed:
            return refreshed.get('access_token')
        logger.warning('[Claude OAuth] Refresh failed, using potentially expired token')

    return access_token


def _extract_email_from_token(token_response: dict) -> str:
    """Extract email from token response or ID token JWT."""
    # Try direct field first
    if token_response.get('email'):
        return token_response['email']

    # Try parsing ID token JWT (base64-decode the payload)
    id_token = token_response.get('id_token', '')
    if id_token:
        try:
            import base64
            parts = id_token.split('.')
            if len(parts) >= 2:
                # Add padding
                payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                return claims.get('email', '')
        except Exception as e:
            logger.debug('[Claude OAuth] Failed to parse ID token: %s', e)

    return ''

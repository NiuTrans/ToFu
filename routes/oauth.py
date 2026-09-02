"""routes/oauth.py — OAuth authentication API endpoints.

Browser-centric flow:
  1. POST /api/oauth/login   → returns auth_url, starts relay server
  2. Browser opens auth_url in popup → user authenticates
  3. OAuth redirects to localhost:PORT → relay server serves HTML page
  4. Relay page uses postMessage() to send code back to opener window
  5. POST /api/oauth/callback → frontend sends code, server exchanges for tokens
  6. GET  /api/oauth/status   → poll auth state
  7. POST /api/oauth/logout   → delete tokens
"""

from quart import Blueprint, request

from lib.log import get_logger
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_ok, api_payload,
)
from lib.request_parser import parse_body

logger = get_logger(__name__)

oauth_bp = Blueprint('oauth', __name__)



def _truthy(v) -> bool:
    """Parse a flag that may arrive as a JSON bool or a query string.

    The login route is reached by BOTH transports (the frontend falls back to
    GET when a proxy refuses POST to an unknown path), so a flag that only
    parsed one of them would be silently inert on exactly the deployments
    that need the fallback most.
    """
    if isinstance(v, bool):
        return v
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


@oauth_bp.route('/api/oauth/login', methods=['GET', 'POST'])
def oauth_login():
    """Start an OAuth login flow.

    Generates PKCE codes, auth URL, and starts a relay server on the
    registered callback port. The frontend should open auth_url in a
    popup and listen for postMessage('oauth_callback', ...) to receive
    the authorization code.

    POST Body: { "provider": "claude" | "codex" }
    GET Query: ?provider=claude|codex
    Returns: { "auth_url": "...", "status": "started", "provider": "...", "callback_port": N }
    """
    try:
        from lib.oauth.manager import start_oauth_flow

        logger.info('[OAuth API] %s /api/oauth/login from %s', request.method, request.remote_addr)

        # Support both GET (query params) and POST (JSON body)
        if request.method == 'GET':
            provider = request.args.get('provider', '')
            prefer_console = _truthy(request.args.get('prefer_console'))
        else:
            data = parse_body(force=True)
            provider = data.get('provider', '')
            prefer_console = _truthy(data.get('prefer_console'))

        if provider not in ('claude', 'codex'):
            return api_error('Invalid provider. Use "claude" or "codex".', status=400)

        result = start_oauth_flow(provider, prefer_console=prefer_console)

        if 'error' in result:
            return api_payload(result, 400)

        return api_ok(result)

    except Exception as e:
        logger.error('[OAuth API] Login failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')


@oauth_bp.route('/api/oauth/callback', methods=['GET', 'POST'])
def oauth_callback():
    """Exchange an authorization code for tokens.

    Called by the frontend after receiving the code via postMessage
    from the relay page, or via manual URL paste.

    POST Body: { "provider": "claude" | "codex", "code": "XXX" }
      or: { "provider": "claude" | "codex", "callback_url": "http://localhost:.../callback?code=XXX" }
    GET Query: ?provider=claude|codex&code=XXX or ?provider=...&callback_url=...
    """
    try:
        from lib.oauth.manager import exchange_code
        from urllib.parse import urlparse, parse_qs

        logger.info('[OAuth API] %s /api/oauth/callback from %s', request.method, request.remote_addr)

        # Support both GET (query params) and POST (JSON body)
        if request.method == 'GET':
            provider = request.args.get('provider', '')
            code = request.args.get('code', '')
            callback_url = request.args.get('callback_url', '')
            state = request.args.get('state', '')
        else:
            data = parse_body(force=True)
            provider = data.get('provider', '')
            code = data.get('code', '')
            callback_url = data.get('callback_url', '')
            state = data.get('state', '')

        if provider not in ('claude', 'codex'):
            return api_bad_request('Invalid provider')

        # Extract code from callback URL if provided
        if callback_url and not code:
            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)
            code = params.get('code', [None])[0]
            if not code:
                return api_bad_request('No authorization code found in the URL')

        if not code:
            return api_bad_request('No authorization code provided')

        result = exchange_code(provider, code, state=state)

        if 'error' in result:
            return api_payload(result, 400)
        return api_ok(result)

    except Exception as e:
        logger.error('[OAuth API] Callback failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')


@oauth_bp.route('/api/oauth/store-token', methods=['POST'])
def oauth_store_token():
    """Persist a token the BROWSER exchanged itself (B1 geo-block workaround).

    When the server's egress is geo-blocked from the provider's token
    endpoint, the frontend performs the token exchange from the user's own
    (VPN-enabled) network and POSTs the raw token JSON here.

    POST Body: { "provider": "claude"|"codex", "token": { ...token JSON... } }
    """
    try:
        from lib.oauth.manager import store_token

        logger.info('[OAuth API] POST /api/oauth/store-token from %s', request.remote_addr)
        data = parse_body(force=True)
        provider = data.get('provider', '')
        token_response = data.get('token')

        if provider not in ('claude', 'codex'):
            return api_bad_request('Invalid provider')
        if not isinstance(token_response, dict):
            return api_bad_request('Missing or invalid token payload')

        result = store_token(provider, token_response)
        if 'error' in result:
            return api_payload(result, 400)
        return api_ok(result)

    except Exception as e:
        logger.error('[OAuth API] store-token failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')


# OAuth status + test routes moved to routes/api_v1/oauth.py.
# login/callback/logout stay here because they mix GET form-redirects
# (geo-block fallback) and don't fit the v1 JSON contract.


@oauth_bp.route('/api/oauth/logout', methods=['GET', 'POST'])
def oauth_logout():
    """Logout from an OAuth provider.

    POST Body: { "provider": "claude" | "codex" }
    GET Query: ?provider=claude|codex
    """
    try:
        from lib.oauth.manager import logout_oauth

        logger.info('[OAuth API] %s /api/oauth/logout from %s', request.method, request.remote_addr)

        # Support both GET (query params) and POST (JSON body)
        if request.method == 'GET':
            provider = request.args.get('provider', '')
        else:
            data = parse_body(force=True)
            provider = data.get('provider', '')

        if provider not in ('claude', 'codex'):
            return api_bad_request('Invalid provider')

        result = logout_oauth(provider)
        if not result.get('ok'):
            return api_internal_error(result.get('error', 'internal_error'))
        return api_ok(result)

    except Exception as e:
        logger.error('[OAuth API] Logout failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')

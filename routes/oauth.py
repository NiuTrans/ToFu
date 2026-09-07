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

from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

from quart import Blueprint, request

from lib.log import get_logger
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_ok, api_payload,
)
from lib.request_parser import BadRequest, optional_str, parse_body

logger = get_logger(__name__)

oauth_bp = Blueprint('oauth', __name__)


def _request_owner_user_id() -> int:
    """Resolve browser-flow ownership at the shared auth boundary."""
    from routes.api_v1.auth import request_principal

    return request_principal().require_owner(context='OAuth browser flow')


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


def _oauth_request_data() -> Mapping[str, object]:
    """Select the legacy GET fallback or the canonical JSON body once.

    These endpoints keep GET for proxies that refuse POST to an unfamiliar
    path.  POST is still a mutation: strict parsing prevents malformed JSON
    from turning into an empty/default OAuth action.  Callers invoke this
    before their operational ``try`` block so field errors remain 400s rather
    than being relabeled as internal OAuth failures.
    """
    if request.method == 'GET':
        return request.args
    return parse_body(force=True, strict=True)


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
    data = _oauth_request_data()
    provider = optional_str(data, 'provider', default='', max_len=16)
    prefer_console = _truthy(data.get('prefer_console'))
    if provider not in ('claude', 'codex'):
        return api_error('Invalid provider. Use "claude" or "codex".', status=400)

    owner_user_id = _request_owner_user_id()
    try:
        from lib.oauth.manager import start_oauth_flow
        logger.info('[OAuth API] %s /api/oauth/login from %s',
                    request.method, request.remote_addr)
        result = start_oauth_flow(
            provider,
            owner_user_id=owner_user_id,
            prefer_console=prefer_console,
        )

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
    data = _oauth_request_data()
    provider = optional_str(data, 'provider', default='', max_len=16)
    code = optional_str(
        data, 'code', default='', strip=False, max_len=16_384)
    callback_url = optional_str(
        data, 'callback_url', default='', max_len=16_384)
    state = optional_str(
        data, 'state', default='', strip=False, max_len=4096)
    manual = _truthy(data.get('manual'))
    if provider not in ('claude', 'codex'):
        return api_bad_request('Invalid provider')

    # A pasted callback URL is the manual path by definition: pick up its
    # state when the caller did not pass one separately.  URL decoding is
    # input validation, so failures stay outside the operational 500 handler.
    if callback_url and not code:
        try:
            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)
        except ValueError as error:
            raise BadRequest(
                'callback_url is invalid', field='callback_url') from error
        code = params.get('code', [None])[0]
        if not code:
            return api_bad_request('No authorization code found in the URL')
        state = state or params.get('state', [''])[0]
        manual = True
    if not code:
        return api_bad_request('No authorization code provided')

    owner_user_id = _request_owner_user_id()
    try:
        from lib.oauth.manager import exchange_code
        logger.info('[OAuth API] %s /api/oauth/callback from %s',
                    request.method, request.remote_addr)
        result = exchange_code(
            provider, code, state=state,
            owner_user_id=owner_user_id, manual=manual,
        )

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
    data = _oauth_request_data()
    provider = optional_str(data, 'provider', default='', max_len=16)
    token_response = data.get('token')
    if provider not in ('claude', 'codex'):
        return api_bad_request('Invalid provider')
    if not isinstance(token_response, dict):
        return api_bad_request('Missing or invalid token payload')

    owner_user_id = _request_owner_user_id()
    try:
        from lib.oauth.manager import store_token
        logger.info('[OAuth API] POST /api/oauth/store-token from %s',
                    request.remote_addr)
        result = store_token(
            provider, token_response,
            owner_user_id=owner_user_id,
        )
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
    data = _oauth_request_data()
    provider = optional_str(data, 'provider', default='', max_len=16)
    if provider not in ('claude', 'codex'):
        return api_bad_request('Invalid provider')

    try:
        from lib.oauth.manager import logout_oauth
        owner_user_id = _request_owner_user_id()
        logger.info('[OAuth API] %s /api/oauth/logout from %s',
                    request.method, request.remote_addr)
        result = logout_oauth(provider, owner_user_id=owner_user_id)
        if not result.get('ok'):
            return api_internal_error(result.get('error', 'internal_error'))
        return api_ok(result)

    except Exception as e:
        logger.error('[OAuth API] Logout failed: %s', e, exc_info=True)
        return api_internal_error('internal_error')

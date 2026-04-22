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

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

oauth_bp = Blueprint('oauth', __name__)


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
        else:
            data = request.get_json(force=True, silent=True) or {}
            provider = data.get('provider', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider. Use "claude" or "codex".'}), 400

        result = start_oauth_flow(provider)

        if 'error' in result:
            return jsonify(result), 400

        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Login failed: %s', e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500


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
            data = request.get_json(force=True, silent=True) or {}
            provider = data.get('provider', '')
            code = data.get('code', '')
            callback_url = data.get('callback_url', '')
            state = data.get('state', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider'}), 400

        # Extract code from callback URL if provided
        if callback_url and not code:
            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)
            code = params.get('code', [None])[0]
            if not code:
                return jsonify({'error': 'No authorization code found in the URL'}), 400

        if not code:
            return jsonify({'error': 'No authorization code provided'}), 400

        result = exchange_code(provider, code, state=state)

        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Callback failed: %s', e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500


@oauth_bp.route('/api/oauth/status')
def oauth_status():
    """Get OAuth status for all providers or a specific one.

    Query: ?provider=claude (optional)
    Returns: { "claude": {...}, "codex": {...} } or single provider dict.
    """
    try:
        from lib.oauth.manager import get_oauth_status, get_all_oauth_status

        provider = request.args.get('provider', '')

        if provider:
            if provider not in ('claude', 'codex'):
                return jsonify({'error': 'Invalid provider'}), 400
            return jsonify(get_oauth_status(provider))

        return jsonify(get_all_oauth_status())

    except Exception as e:
        logger.error('[OAuth API] Status check failed: %s', e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500


@oauth_bp.route('/api/oauth/test')
def oauth_test():
    """Test server-side connectivity to OAuth endpoints.

    Returns which endpoints are reachable from the server (for diagnosing
    geo-blocking issues in China).
    """
    import requests as req
    from lib.proxy import proxies_for

    results = {}
    endpoints = {
        'claude_token': 'https://console.anthropic.com/v1/oauth/token',
        'claude_auth': 'https://claude.ai/',
        'codex_token': 'https://auth.openai.com/oauth/token',
        'codex_auth': 'https://auth.openai.com/',
    }

    for name, url in endpoints.items():
        try:
            r = req.get(url, proxies=proxies_for(url), timeout=8,
                        allow_redirects=False)
            blocked = (
                r.status_code == 302 and 'unavailable-in-region' in (r.headers.get('Location', ''))
                or 'unsupported_country_region_territory' in r.text[:500]
            )
            results[name] = {
                'url': url, 'status': r.status_code,
                'reachable': not blocked,
                'blocked': blocked,
                'detail': r.headers.get('Location', '')[:200] if r.status_code == 302
                          else r.text[:200],
            }
        except Exception as e:
            results[name] = {
                'url': url, 'status': 0, 'reachable': False,
                'blocked': True, 'detail': str(e)[:200],
            }

    return jsonify(results)


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
            data = request.get_json(force=True, silent=True) or {}
            provider = data.get('provider', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider'}), 400

        result = logout_oauth(provider)
        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Logout failed: %s', e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500

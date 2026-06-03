"""routes/api_v1/oauth.py — OAuth status / diagnostics surface.

Two routes:
  GET /api/v1/oauth/status — auth state for all providers (or one with
                              ``?provider=claude|codex``)
  GET /api/v1/oauth/test   — server-side reachability probe of OAuth
                              endpoints (admin-scoped)

The browser-redirect flows themselves stay at their legacy paths because
they mix GET form-redirects (geo-block fallback) and don't fit the v1
JSON contract:

  POST/GET /api/oauth/login    — kicks off PKCE + relay server
  POST/GET /api/oauth/callback — exchanges authorization code for tokens
  POST/GET /api/oauth/logout   — revokes stored tokens
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from lib.api_response import api_bad_request, api_internal_error
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_oauth_bp = Blueprint('api_v1_oauth', __name__)


@api_v1_oauth_bp.route('/api/v1/oauth/status', methods=['GET'])
@require_auth
@api_meta(
    summary='OAuth login status (per provider)',
    description=(
        'Returns ``{<provider>: {logged_in, expires_at, ...}}`` for all '
        'providers, or just the requested one when ``?provider=`` is set. '
        '"Provider" here refers to the *upstream subscription provider* '
        '(Claude Pro, ChatGPT Codex), NOT the v1 LLM provider config.'
    ),
    tags=['capabilities'],
)
def oauth_status():
    try:
        from lib.oauth.manager import get_all_oauth_status, get_oauth_status

        provider = request.args.get('provider', '')
        if provider:
            if provider not in ('claude', 'codex'):
                return api_bad_request('Invalid provider', field='provider')
            return jsonify(get_oauth_status(provider))
        return jsonify(get_all_oauth_status())
    except Exception as e:
        logger.error('[OAuth.v1] status check failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.status')


@api_v1_oauth_bp.route('/api/v1/oauth/test', methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='Test server-side OAuth endpoint reachability (admin)',
    description=(
        'Probes the four OAuth endpoints (``claude_token``, '
        '``claude_auth``, ``codex_token``, ``codex_auth``) from the '
        'server and returns reachability + geo-block detection. '
        'Mainly useful for diagnosing the China geo-block / corporate '
        'proxy situation. Admin-scoped because the response leaks '
        'partial response bodies.'
    ),
    tags=['capabilities'], scope='admin',
)
def oauth_test():
    import requests as req
    from lib.proxy import proxies_for

    endpoints = {
        'claude_token': 'https://console.anthropic.com/v1/oauth/token',
        'claude_auth':  'https://claude.ai/',
        'codex_token':  'https://auth.openai.com/oauth/token',
        'codex_auth':   'https://auth.openai.com/',
    }

    results: dict[str, dict] = {}
    for name, url in endpoints.items():
        try:
            r = req.get(url, proxies=proxies_for(url), timeout=8,
                        allow_redirects=False)
            blocked = (
                (r.status_code == 302 and 'unavailable-in-region'
                 in (r.headers.get('Location', '')))
                or 'unsupported_country_region_territory' in r.text[:500]
            )
            results[name] = {
                'url': url,
                'status': r.status_code,
                'reachable': not blocked,
                'blocked': blocked,
                'detail': (r.headers.get('Location', '')[:200]
                           if r.status_code == 302 else r.text[:200]),
            }
        except req.RequestException as e:
            logger.debug('[OAuth.v1] reachability probe %s (%s) failed: %s',
                         name, url, e)
            results[name] = {
                'url': url, 'status': 0, 'reachable': False,
                'blocked': True, 'detail': str(e)[:200],
            }
    return jsonify(results)


__all__ = ['api_v1_oauth_bp']

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

from quart import Blueprint, request

from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_oauth_bp = Blueprint('api_v1_oauth', __name__)


# provider → the API host its subscription traffic targets (egress probe key)
_PROVIDER_EGRESS_HOST = {
    'claude': 'api.anthropic.com',
    'codex': 'chatgpt.com',
}


def _with_egress_state(status: dict, provider: str, user_id: str) -> dict:
    """Attach the desktop-egress state to one provider's status payload.

    NEVER probes inline (page-load path, design §6.2 A4) — egress_status
    reads the 300s probe cache only and fires a background warm-up.
    """
    host = _PROVIDER_EGRESS_HOST.get(provider)
    if not host:
        return status
    try:
        from lib.desktop.egress import egress_status
        status = dict(status)
        status['egress'] = egress_status(host, user_id=user_id)
    except Exception as e:
        logger.debug('[OAuth.v1] egress status failed for %s: %s', provider, e)
    return status


def _with_quota_state(status: dict, provider: str) -> dict:
    """Attach the latest successful Codex quota snapshot, when available."""
    if provider != 'codex' or not status.get('authenticated'):
        return status
    try:
        from lib.subscription_quota import latest_subscription_quota
        quota = latest_subscription_quota(
            provider, cache_key='oauth_codex')
        if quota:
            status = dict(status)
            status['quota'] = quota
    except Exception as e:
        logger.debug('[OAuth.v1] quota status failed for %s: %s', provider, e)
    return status


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
        from lib.oauth.outbound import reconcile_oauth_providers
        reconcile_oauth_providers()
        from lib.oauth.manager import get_all_oauth_status, get_oauth_status
        from .auth import current_auth
        _auth = current_auth()
        _uid = (_auth.user_id
                if _auth and getattr(_auth, 'user_id', '') else '')

        provider = request.args.get('provider', '')
        if provider:
            if provider not in ('claude', 'codex'):
                return api_bad_request('Invalid provider', field='provider')
            return api_ok(_with_quota_state(_with_egress_state(
                get_oauth_status(provider), provider, _uid), provider))
        all_status = get_all_oauth_status()
        return api_ok({p: _with_quota_state(
                           _with_egress_state(s, p, _uid), p)
                       for p, s in all_status.items()})
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
        'claude_token': 'https://platform.claude.com/v1/oauth/token',
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
    return api_ok(results)


# ── Egress agent pin selector (multi-agent deployments) ─────────────


def _pins_path() -> str:
    from lib.config_dir import config_path
    return config_path('oauth_egress_agents.json')


@api_v1_oauth_bp.route('/api/v1/oauth/egress-agent', methods=['GET'])
@require_auth
@api_meta(
    summary='Egress agent pin state + online agents',
    description=(
        'Returns ``{pinned, agents}`` — the caller\'s pinned desktop egress '
        'agent and the online agents (with capabilities) eligible for '
        'subscription egress routing.'
    ),
    tags=['capabilities'],
)
def oauth_egress_agent_get():
    try:
        from lib.desktop import list_agents
        from lib.desktop.egress import _pinned_agent
        from .auth import current_auth
        auth = current_auth()
        uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
        pinned = _pinned_agent(uid)
        agents = [
            {'agent_id': a.get('agent_id'), 'name': a.get('name'),
             'platform': a.get('platform'),
             'capabilities': a.get('capabilities') or {},
             'online': a.get('online', False)}
            for a in list_agents(user_id=uid or None)
        ]
        return api_ok({'pinned': pinned, 'agents': agents})
    except Exception as e:
        logger.error('[OAuth.v1] egress-agent GET failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.egress_agent')


@api_v1_oauth_bp.route('/api/v1/oauth/egress-agent', methods=['POST'])
@require_auth
@api_meta(
    summary='Pin the desktop egress agent for this user',
    description=(
        'Body ``{agent_id}`` — pins the caller\'s subscription egress to one '
        'online agent. Empty agent_id clears the pin. Persisted to '
        '``data/config/oauth_egress_agents.json`` keyed by user.'
    ),
    tags=['capabilities'],
)
def oauth_egress_agent_set():
    try:
        from quart import request as _req
        from lib.json_store import update_json_atomic
        from .auth import current_auth
        auth = current_auth()
        uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
        body = _req.get_json(silent=True) or {}
        agent_id = str(body.get('agent_id') or '').strip()
        if agent_id:
            from lib.desktop import list_agents
            known = {a.get('agent_id') for a in list_agents(user_id=uid or None)}
            if agent_id not in known:
                return api_bad_request('unknown agent_id')

        def _mutate(data):
            data = dict(data or {})
            if agent_id:
                data[uid] = agent_id
            else:
                data.pop(uid, None)
            return data

        update_json_atomic(_pins_path(), _mutate, default={})
        from lib.log import audit_log
        audit_log('oauth_egress_agent_pinned', user_id=uid,
                  agent_id=agent_id or '(cleared)')
        return api_ok({'pinned': agent_id})
    except Exception as e:
        logger.error('[OAuth.v1] egress-agent POST failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.egress_agent')


__all__ = ['api_v1_oauth_bp']

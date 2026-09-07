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

from lib.api_response import (
    api_bad_request, api_internal_error, api_ok, api_payload,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import optional_str, parse_body

from .auth import request_principal, require_auth, require_scope

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


def _with_quota_state(status: dict, provider: str,
                      user_id: str = '') -> dict:
    """Attach passive quota plus the explicit earned-reset entitlement.

    This function never performs upstream I/O.  The reset owner returns its
    last account-scoped snapshot and starts one daemon refresh when stale.
    """
    if provider != 'codex' or not status.get('authenticated'):
        return status
    status = dict(status)
    try:
        from lib.subscription_quota import latest_subscription_quota
        quota = latest_subscription_quota(
            provider, cache_key='oauth_codex')
        if quota:
            status['quota'] = quota
    except Exception as e:
        logger.debug('[OAuth.v1] quota status failed for %s: %s', provider, e)
    try:
        from lib.oauth.codex_usage import codex_usage_reset_status
        status['reset_offer'] = codex_usage_reset_status(user_id=user_id)
    except Exception as e:
        # Failure stays explicitly unknown; it must never be projected as zero.
        logger.debug('[OAuth.v1] reset-offer status failed for %s: %s',
                     provider, e)
        status['reset_offer'] = {
            'state': 'unknown', 'available_count': None,
            'source': 'codex_usage_api', 'captured_at': None,
            'stale': False, 'refreshing': False,
            'reason': 'status_unavailable',
        }
    return status


@api_v1_oauth_bp.route('/api/v1/oauth/status', methods=['GET'])
@require_auth
@api_meta(
    summary='OAuth login status (per provider)',
    description=(
        'Returns ``{<provider>: {logged_in, expires_at, ...}}`` for all '
        'providers, or just the requested one when ``?provider=`` is set. '
        '"Provider" here refers to the *upstream subscription provider* '
        '(Claude Pro, ChatGPT Codex), NOT the v1 LLM provider config. '
        'Authenticated Codex status also carries a non-blocking '
        '``reset_offer`` state (available, none, or unknown); unknown is '
        'never inferred as zero.'
    ),
    tags=['capabilities'],
)
def oauth_status():
    owner_user_id = request_principal().require_owner(
        context='OAuth status')
    owner_scope = str(owner_user_id)
    try:
        from lib.oauth.outbound import reconcile_oauth_providers
        reconcile_oauth_providers(owner_user_id=owner_user_id)
        from lib.oauth.manager import get_all_oauth_status, get_oauth_status
        provider = request.args.get('provider', '')
        if provider:
            if provider not in ('claude', 'codex'):
                return api_bad_request('Invalid provider', field='provider')
            return api_ok(_with_quota_state(_with_egress_state(
                get_oauth_status(provider, owner_user_id=owner_user_id),
                provider, owner_scope),
                provider, owner_scope))
        all_status = get_all_oauth_status(owner_user_id=owner_user_id)
        return api_ok({p: _with_quota_state(
                           _with_egress_state(s, p, owner_scope),
                           p, owner_scope)
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


@api_v1_oauth_bp.route('/api/v1/oauth/device-login', methods=['POST', 'GET'])
@require_auth
@api_meta(
    summary='Start a device-authorization login flow (Codex)',
    description=(
        'Mints a user code from OpenAI\'s deviceauth API and starts the '
        'server-side poll thread. This path never touches the localhost:1455 '
        'redirect: the user '
        'enters the displayed code at the verification URL in ANY browser. '
        'It requires one working server proxy/direct/desktop-agent route; a '
        'transport outage is returned as 503 so the UI can fall back to the '
        'browser callback-copy flow. '
        'Completion is observed via the regular /api/v1/oauth/status '
        'projection (device.user_code + status transitions).'
    ),
    tags=['capabilities'],
)
def oauth_device_login():
    if request.method == 'GET':
        provider = request.args.get('provider', '')
    else:
        from lib.request_parser import optional_str, parse_body
        body = parse_body(force=True, strict=True)
        provider = optional_str(body, 'provider', default='', max_len=16)
    if provider != 'codex':
        return api_bad_request(
            'Device login is only available for provider=codex',
            field='provider')

    owner_user_id = request_principal().require_owner(
        context='OAuth device flow')
    try:
        from lib.oauth.manager import start_device_flow
        logger.info('[OAuth.v1] %s /api/v1/oauth/device-login from %s',
                    request.method, request.remote_addr)
        result = start_device_flow(
            provider, owner_user_id=owner_user_id)
        if 'error' in result:
            # A transport outage is not a malformed login request. Preserve
            # the upstream status_code=0 detail and use 503 so clients can
            # select the browser-network fallback without string matching.
            http_status = 503 if result.get('status_code') in (0, '0') else 400
            return api_payload(result, http_status)
        return api_ok(result)
    except Exception as e:
        logger.error('[OAuth.v1] device-login failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.oauth.device_login')

# ── Egress agent pin selector (multi-agent deployments) ─────────────


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
    from lib.desktop.egress import egress_agent_selection

    owner_user_id = request_principal().require_owner(
        context='desktop egress selection')
    pinned, eligible_agents = egress_agent_selection(owner_user_id)
    agents = [
        {'agent_id': agent.get('agent_id'), 'name': agent.get('name'),
         'platform': agent.get('platform'),
         'capabilities': agent.get('capabilities') or {},
         'online': True}
        for agent in eligible_agents
    ]
    return api_ok({'pinned': pinned, 'agents': agents})


@api_v1_oauth_bp.route('/api/v1/oauth/egress-agent', methods=['POST'])
@require_auth
@api_meta(
    summary='Pin the desktop egress agent for this user',
    description=(
        'Body ``{agent_id}`` — pins the caller\'s subscription egress to one '
        'online egress-capable agent owned by the caller. Empty agent_id '
        'clears the pin. The owner-scoped preference is persisted by the '
        'Storage Sidecar.'
    ),
    tags=['capabilities'],
)
def oauth_egress_agent_set():
    # Parse before domain work. BadRequest is a client 400 at the shared HTTP
    # boundary and must never reach storage or clear an existing selection.
    body = parse_body(strict=True)
    agent_id = optional_str(
        body, 'agent_id', default='', max_len=128)
    from lib.desktop.egress import UnknownEgressAgent, pin_egress_agent
    from lib.log import audit_log

    owner_user_id = request_principal().require_owner(
        context='desktop egress selection')
    try:
        pinned = pin_egress_agent(owner_user_id, agent_id)
    except UnknownEgressAgent as exc:
        return api_bad_request(str(exc), field='agent_id')
    audit_log(
        'oauth_egress_agent_pinned',
        owner_user_id=owner_user_id,
        agent_id=pinned or '(cleared)',
    )
    return api_ok({'pinned': pinned})


__all__ = ['api_v1_oauth_bp']

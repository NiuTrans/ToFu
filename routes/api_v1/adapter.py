"""routes/api_v1/adapter.py — 订阅适配器（CLIProxyAPI sidecar）管理面（E4）。

Endpoints (charter#0 envelope):

  GET  /api/v1/adapter/status          — online egress-capable agents +
                                         their adapter state / ensure tasks
  POST /api/v1/adapter/ensure          — bring the sidecar up on one agent
                                         (admin; provisions the managed
                                         provider on success, background)
  POST /api/v1/adapter/stop            — stop it and deprovision (admin)

The actual mechanics live in lib/desktop/adapter.py (policy store,
loopback relay, provider provisioning); this is the thin REST surface.
"""

from __future__ import annotations

from flask import Blueprint

from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, parse_body, require_str

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_adapter_bp = Blueprint('api_v1_adapter', __name__)


def _caller_uid() -> str:
    from .auth import current_auth
    auth = current_auth()
    return (auth.user_id if auth and getattr(auth, 'user_id', '') else '')


@api_v1_adapter_bp.route('/api/v1/adapter/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Subscription-adapter state per online agent',
    description=(
        'Lists the online egress-capable desktop agents with their live '
        'CLIProxyAPI sidecar state (via the bridge, 10s-cached), the '
        'server-side ensure tasks, and the redacted per-agent policy. The '
        'settings card polls this to render install/running/version.'
    ),
    tags=['capabilities'],
)
def adapter_status_route():
    try:
        from lib.desktop import list_agents
        from lib.desktop.adapter import (
            adapter_policy_public,
            adapter_status,
            ensure_task_state,
        )
        uid = _caller_uid()
        agents = []
        for a in list_agents(user_id=uid or None):
            caps = a.get('capabilities') or {}
            if not caps.get('egress'):
                continue
            aid = a.get('agent_id')
            if not aid:
                continue
            agents.append({
                'agent_id': aid,
                'name': a.get('name', ''),
                'platform': a.get('platform', ''),
                'online': a.get('online', False),
                'adapter': adapter_status(aid, user_id=uid),
                'policy': adapter_policy_public(aid),
            })
        return api_ok({'agents': agents, 'ensure_tasks': ensure_task_state()})
    except Exception as e:
        logger.error('[Adapter.v1] status failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.status')


@api_v1_adapter_bp.route('/api/v1/adapter/ensure', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Bring the subscription adapter up on one agent (admin)',
    description=(
        'Body ``{agent_id}``. Mints/reuses the per-agent policy (random '
        'api-key + management secret), kicks ``adapter_ensure`` on the '
        'agent in the background (first run downloads ~20 MB from GitHub '
        'Releases, SHA-256-verified), and on success provisions the '
        'managed ``adapter_<id>`` provider from the adapter\'s /v1/models. '
        'Returns the ensure task snapshot; poll /status for completion.'
    ),
    tags=['capabilities'], scope='admin',
)
def adapter_ensure_route():
    body = parse_body()
    try:
        agent_id = require_str(body, 'agent_id')
    except BadRequest as e:
        return api_bad_request(str(e), field=getattr(e, 'field', '') or 'agent_id')
    try:
        from lib.desktop import list_agents
        from lib.desktop.adapter import ensure_adapter
        uid = _caller_uid()
        known = {a.get('agent_id'): a for a in list_agents(user_id=uid or None)}
        if agent_id not in known:
            return api_bad_request('unknown agent_id', field='agent_id')
        task = ensure_adapter(agent_id,
                              agent_name=known[agent_id].get('name', ''),
                              user_id=uid)
        return api_ok({'task': task})
    except Exception as e:
        logger.error('[Adapter.v1] ensure failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.ensure')


@api_v1_adapter_bp.route('/api/v1/adapter/stop', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Stop the subscription adapter on one agent (admin)',
    description=(
        'Body ``{agent_id}``. Stops the sidecar on the agent and removes '
        'the managed provider so no slot keeps routing to a dead adapter.'
    ),
    tags=['capabilities'], scope='admin',
)
def adapter_stop_route():
    body = parse_body()
    try:
        agent_id = require_str(body, 'agent_id')
    except BadRequest as e:
        return api_bad_request(str(e), field=getattr(e, 'field', '') or 'agent_id')
    try:
        from lib.desktop.adapter import stop_adapter
        uid = _caller_uid()
        out = stop_adapter(agent_id, user_id=uid)
        return api_ok(out)
    except Exception as e:
        logger.error('[Adapter.v1] stop failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.stop')


__all__ = ['api_v1_adapter_bp']

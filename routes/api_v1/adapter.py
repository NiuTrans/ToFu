"""routes/api_v1/adapter.py — 订阅适配器（CLIProxyAPI sidecar）管理面（E4）。

Endpoints (charter#0 envelope):

  GET  /api/v1/adapter/status          — online egress-capable agents +
                                         their adapter state / ensure tasks
  POST /api/v1/adapter/ensure          — bring the sidecar up on one agent
                                         (admin; provisions the managed
                                         provider on success, background)
  POST /api/v1/adapter/stop            — stop it and deprovision (admin)
  POST /api/v1/adapter/oauth/start     — start Claude/ChatGPT login on agent
  GET  /api/v1/adapter/oauth/status    — poll the agent-owned OAuth session
  POST /api/v1/adapter/oauth/callback  — manual callback fallback
  DELETE /api/v1/adapter/accounts      — remove one agent-local account

The actual mechanics live in lib/desktop/adapter.py (policy store,
loopback relay, provider provisioning); this is the thin REST surface.
"""

from __future__ import annotations

import re

from quart import Blueprint, request

from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, parse_body, require_str

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_adapter_bp = Blueprint('api_v1_adapter', __name__)


def _caller_uid() -> str:
    from .auth import current_auth
    auth = current_auth()
    return str(auth.owner_user_id or '') if auth else ''


def _known_agent(agent_id: str, uid: str) -> dict:
    from lib.desktop import list_agents
    return next((a for a in list_agents(user_id=uid or None)
                 if a.get('agent_id') == agent_id), None)


def _valid_oauth_state(state: str) -> bool:
    return bool(len(state) <= 128 and '..' not in state
                and re.fullmatch(r'[A-Za-z0-9_.-]+', state))


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
                'adapter': adapter_status(
                    aid, agent_name=a.get('name', ''), user_id=uid),
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


@api_v1_adapter_bp.route('/api/v1/adapter/oauth/start', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Start a subscription login inside an agent-local adapter',
    description=(
        'Body ``{agent_id, provider}``, where provider is ``claude`` or '
        '``codex``. Returns the CLIProxyAPI authorization URL and opaque '
        'state. Credentials remain in the selected desktop agent.'
    ),
    tags=['capabilities'], scope='admin',
)
def adapter_oauth_start_route():
    body = parse_body()
    try:
        agent_id = require_str(body, 'agent_id')
        provider = require_str(body, 'provider').lower()
    except BadRequest as e:
        return api_bad_request(str(e), field=getattr(e, 'field', '') or '')
    if provider not in ('claude', 'codex'):
        return api_bad_request('provider must be claude or codex', field='provider')
    uid = _caller_uid()
    if not _known_agent(agent_id, uid):
        return api_bad_request('unknown agent_id', field='agent_id')
    try:
        from lib.desktop.adapter import start_adapter_oauth
        return api_ok(start_adapter_oauth(agent_id, provider, user_id=uid))
    except (RuntimeError, ValueError) as e:
        logger.warning('[Adapter.v1] OAuth start failed: %s', e)
        return api_error(str(e), status=502)
    except Exception as e:
        logger.error('[Adapter.v1] OAuth start failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.oauth_start')


@api_v1_adapter_bp.route('/api/v1/adapter/oauth/status', methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='Poll an agent-local subscription OAuth session',
    description=(
        'Query ``agent_id`` + opaque ``state``. On success the backend '
        'refreshes the managed adapter provider from /v1/models before '
        'returning, so model pickers can be refreshed immediately.'
    ),
    tags=['capabilities'], scope='admin',
)
def adapter_oauth_status_route():
    agent_id = str(request.args.get('agent_id') or '').strip()
    state = str(request.args.get('state') or '').strip()
    if not agent_id:
        return api_bad_request('agent_id is required', field='agent_id')
    if not _valid_oauth_state(state):
        return api_bad_request('invalid OAuth state', field='state')
    uid = _caller_uid()
    agent = _known_agent(agent_id, uid)
    if not agent:
        return api_bad_request('unknown agent_id', field='agent_id')
    try:
        from lib.desktop.adapter import adapter_oauth_status
        return api_ok(adapter_oauth_status(
            agent_id, state, agent_name=agent.get('name', ''), user_id=uid))
    except (RuntimeError, ValueError) as e:
        logger.warning('[Adapter.v1] OAuth status failed: %s', e)
        return api_error(str(e), status=502)
    except Exception as e:
        logger.error('[Adapter.v1] OAuth status failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.oauth_status')


@api_v1_adapter_bp.route('/api/v1/adapter/oauth/callback', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Submit a manual callback to an agent-local OAuth session',
    description=(
        'Fallback for a browser that cannot reach the desktop callback '
        'port. Body accepts ``redirect_url`` or ``code`` plus the state.'
    ),
    tags=['capabilities'], scope='admin',
)
def adapter_oauth_callback_route():
    body = parse_body()
    try:
        agent_id = require_str(body, 'agent_id')
        provider = require_str(body, 'provider').lower()
        state = require_str(body, 'state')
    except BadRequest as e:
        return api_bad_request(str(e), field=getattr(e, 'field', '') or '')
    if provider not in ('claude', 'codex'):
        return api_bad_request('provider must be claude or codex', field='provider')
    if not _valid_oauth_state(state):
        return api_bad_request('invalid OAuth state', field='state')
    code = str(body.get('code') or '').strip()
    redirect_url = str(body.get('redirect_url') or '').strip()
    error = str(body.get('error') or '').strip()
    if not code and not redirect_url and not error:
        return api_bad_request('code, redirect_url, or error is required')
    if max(len(code), len(redirect_url), len(error)) > 8192:
        return api_bad_request('OAuth callback payload is too long')
    uid = _caller_uid()
    if not _known_agent(agent_id, uid):
        return api_bad_request('unknown agent_id', field='agent_id')
    try:
        from lib.desktop.adapter import submit_adapter_oauth_callback
        return api_ok(submit_adapter_oauth_callback(
            agent_id, provider, state, code=code, redirect_url=redirect_url,
            error=error, user_id=uid))
    except (RuntimeError, ValueError) as e:
        logger.warning('[Adapter.v1] OAuth callback failed: %s', e)
        return api_error(str(e), status=502)
    except Exception as e:
        logger.error('[Adapter.v1] OAuth callback failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.oauth_callback')


@api_v1_adapter_bp.route('/api/v1/adapter/accounts', methods=['DELETE'])
@require_scope('admin')
@api_meta(
    summary='Delete one account from an agent-local subscription adapter',
    description='Body ``{agent_id, name, auth_index?}``.',
    tags=['capabilities'], scope='admin',
)
def adapter_account_delete_route():
    body = parse_body()
    try:
        agent_id = require_str(body, 'agent_id')
        name = require_str(body, 'name')
    except BadRequest as e:
        return api_bad_request(str(e), field=getattr(e, 'field', '') or '')
    uid = _caller_uid()
    agent = _known_agent(agent_id, uid)
    if not agent:
        return api_bad_request('unknown agent_id', field='agent_id')
    try:
        from lib.desktop.adapter import delete_adapter_account
        out = delete_adapter_account(
            agent_id, name, auth_index=body.get('auth_index'),
            agent_name=agent.get('name', ''), user_id=uid)
        return api_ok(out)
    except ValueError as e:
        return api_bad_request(str(e))
    except RuntimeError as e:
        logger.warning('[Adapter.v1] account delete failed: %s', e)
        return api_error(str(e), status=502)
    except Exception as e:
        logger.error('[Adapter.v1] account delete failed: %s', e, exc_info=True)
        return api_internal_error(e, source='api_v1.adapter.account_delete')


__all__ = ['api_v1_adapter_bp']

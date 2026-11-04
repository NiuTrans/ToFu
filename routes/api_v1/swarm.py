"""routes/api_v1/swarm.py — Swarm UI surface.

Routes the in-app debug panel, the background-agent dock and external clients use:

  GET  /api/v1/swarm/status/<task_id>   — current swarm state for a task
  POST /api/v1/swarm/abort/<task_id>    — request abort of all sub-agents
  POST /api/v1/swarm/runs/<swarm_id>/agents/<agent_id>/stop
                                        — stop one running agent of a live run
  GET  /api/v1/swarm/config             — registry: roles

Status/abort are also mirrored at ``/api/v1/agents/swarm/{status,abort}/<id>``
in :mod:`routes.api_v1.agents` for the headless SDK; the routes here are
the UI-shaped duplicates (bare-dict status response) and add the
``/config`` registry endpoint that the SDK alias doesn't expose.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import (
    api_conflict, api_internal_error, api_not_found, api_ok, safe_route,
)
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import request_user_id, require_auth, require_scope

logger = get_logger(__name__)

api_v1_swarm_bp = Blueprint('api_v1_swarm', __name__)


@api_v1_swarm_bp.route('/api/v1/swarm/status/<task_id>', methods=['GET'])
@require_auth
@api_meta(
    summary='Get swarm status for a task (UI shape)',
    description=(
        'Returns the swarm state (active flag, agent list, master '
        'progress, etc.) under the standard ``api_ok`` envelope \u2014 the '
        'UI debug panel reads ``data.active`` / ``data.agents`` by name, '
        'identical to the headless SDK alias at '
        '``/api/v1/agents/swarm/status/<id>``.'
    ),
    tags=['agents'],
)
def swarm_status(task_id):
    try:
        from lib.swarm.integration import get_swarm_status
    except ImportError as e:
        logger.error('[Swarm.v1] integration unavailable: %s', e,
                     exc_info=True)
        return api_internal_error(e, context='Swarm unavailable',
                                  source='api_v1.swarm.status')
    try:
        status = get_swarm_status(
            task_id,
            user_id=int(request_user_id()),
        )
    except Exception as e:
        logger.error('[Swarm.v1] status lookup failed task=%s: %s',
                     task_id, e, exc_info=True)
        return api_internal_error(e, context='swarm_status',
                                  source='api_v1.swarm.status')
    if status is None:
        # No in-memory session AND no durable row: genuinely unknown, NOT a
        # settle signal. ``active: None`` + ``known: False`` tells the
        # frontend reconciler to keep probing (restart/rehydrate windows)
        # instead of false-settling the panel into the Unconfirmed limbo.
        return api_ok({'active': None, 'known': False,
                       'message': 'No swarm record for this task'})
    return api_ok(status)


@api_v1_swarm_bp.route('/api/v1/swarm/abort/<task_id>', methods=['POST'])
@require_scope('agents:swarm')
@api_meta(
    summary='Abort all sub-agents in a swarm',
    tags=['agents'], scope='agents:swarm',
)
def swarm_abort(task_id):
    try:
        from lib.swarm.integration import abort_swarm
    except ImportError as e:
        return api_internal_error(e, context='Swarm unavailable',
                                  source='api_v1.swarm.abort')
    try:
        result = abort_swarm(
            task_id,
            user_id=int(request_user_id()),
        )
    except Exception as e:
        logger.warning('[Swarm.v1] abort failed task=%s: %s', task_id, e,
                       exc_info=True)
        return api_internal_error(e, context='swarm_abort',
                                  source='api_v1.swarm.abort',
                                  log_traceback=False)
    if not result.get('success'):
        return api_not_found('Swarm not found')
    return api_ok({'message': 'Swarm abort requested'})


@api_v1_swarm_bp.route('/api/v1/swarm/runs/<swarm_id>/agents/<agent_id>/stop',
                       methods=['POST'])
@require_scope('agents:swarm')
@api_meta(
    summary='Stop one running sub-agent of a live swarm run',
    description=(
        'Sends the stop signal of the model-facing ``kill`` tool: the agent '
        'settles ``cancelled`` while its siblings and coordinator continue. '
        '``swarm_id`` is the run identity from ``GET /api/v4/activity/background`` '
        '(``swarms[].swarm_id``), not the reusable session key. 404 '
        '``swarm_agent_not_found`` when the caller owns no live run with that '
        'agent; 409 ``swarm_agent_not_running`` when the agent has no live '
        'invocation to signal (pending, settled, or starting/finishing an attempt).'
    ),
    tags=['agents'], scope='agents:swarm',
)
@safe_route
def swarm_agent_stop(swarm_id, agent_id):
    from lib.swarm.integration import stop_swarm_agent

    result = stop_swarm_agent(swarm_id, agent_id, user_id=int(request_user_id()))
    if result['outcome'] == 'not_found':
        return api_not_found('No live swarm agent with this run and agent ID',
                             error_code='swarm_agent_not_found')
    if result['outcome'] == 'not_running':
        return api_conflict(
            f"Agent {agent_id!r} has no live invocation to stop (status: {result['status']})",
            error_code='swarm_agent_not_running')
    return api_ok({'stopped': True, 'swarmId': swarm_id, 'agentId': agent_id})


@api_v1_swarm_bp.route('/api/v1/swarm/config', methods=['GET'])
@require_auth
@api_meta(
    summary='Swarm role registry',
    description=(
        'Returns ``{available, version, roles}``. Accepted sub-agents start '
        'on demand, so there is no concurrency limit to report.'
    ),
    tags=['agents'],
)
def swarm_config():
    from lib.swarm.registry import AGENT_ROLES
    return api_ok({
        'available': True,
        'version': '1.0.0',
        'roles': list(AGENT_ROLES.keys()),
    })


__all__ = ['api_v1_swarm_bp']

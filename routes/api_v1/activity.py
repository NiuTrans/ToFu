"""routes/api_v1/activity.py — cross-surface background activity snapshot.

One lightweight read for the chrome that answers "is anything still working
now that my turn ended?":

  GET /api/v1/activity/background — live swarm sessions (sub-agents still
      executing after the spawning turn settled) + headless background
      commands still running, owner-scoped.

Both sources are process-local registries (lib/swarm/integration/_state.py,
lib/tasks_pkg/background_commands.py); the payload is deliberately small and
cheap to assemble so the client can poll it on a few-second cadence.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_activity_bp = Blueprint('api_v1_activity', __name__)


@api_v1_activity_bp.route('/api/v1/activity/background', methods=['GET'])
@require_auth
@api_meta(
    summary='Live background activity (swarms + background commands)',
    description=(
        'Returns ``{swarms, backgroundCommands}`` for the authenticated '
        'owner. ``swarms`` lists non-terminated swarm sessions with '
        'per-agent status (keyed by the stable swarm key + conv_id for '
        'deep-links); ``backgroundCommands`` lists detached run_command '
        'executions still running. Both lists are empty in the steady '
        'state — the endpoint exists so chrome can show what is still '
        'executing after the foreground turn has ended.'
    ),
    tags=['activity'],
)
def background_activity():
    user_id = int(request_user_id())
    try:
        from lib.swarm.integration import list_active_swarms
        swarms = list_active_swarms(user_id=user_id)
    except Exception as e:
        logger.error('[activity.v1] swarm snapshot failed: %s', e,
                     exc_info=True)
        return api_internal_error(e, context='background_activity swarms',
                                  source='api_v1.activity.background')
    try:
        from lib.tasks_pkg.background_commands import list_background_commands
        commands = [
            item for item in list_background_commands(user_id=user_id)
            if item.get('status') == 'running'
        ]
    except Exception as e:
        logger.error('[activity.v1] background-command snapshot failed: %s',
                     e, exc_info=True)
        return api_internal_error(e, context='background_activity commands',
                                  source='api_v1.activity.background')
    return api_ok({'swarms': swarms, 'backgroundCommands': commands})


__all__ = ['api_v1_activity_bp']

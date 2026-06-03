"""routes/api_v1/agent_backends.py — Agent backend discovery.

Single read-only route. Returns the list of registered backends
(builtin / claude-code / codex) with availability + auth state +
capability flags, so the UI can grey-out unavailable selectors and
the headless surface can advertise what it can route to.

The legacy ``POST /api/agent-backends/set`` had no actual server-side
effect (active backend lives in conversation settings, persisted via
``PATCH /api/v1/chat/tool-state/{conv_id}``); it is intentionally not
re-exposed here.
"""

from __future__ import annotations

from flask import Blueprint

from lib.api_response import api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_agent_backends_bp = Blueprint('api_v1_agent_backends', __name__)


@api_v1_agent_backends_bp.route('/api/v1/agent-backends/status',
                                methods=['GET'])
@require_auth
@api_meta(
    summary='List agent backends',
    description=(
        'Returns the registered agent backends (built-in Tofu plus any '
        'external CLIs that are installed and authenticated) with their '
        'capability flags. Auth-required read-only — clients use it to '
        'render a backend picker and to know which UI controls to hide '
        'for each backend.'
    ),
    tags=['capabilities'],
)
def backends_status():
    from lib.agent_backends import list_backends
    try:
        backends = list_backends()
    except Exception as e:
        logger.error('[AgentBackends] list_backends failed: %s', e,
                     exc_info=True)
        return api_internal_error(e, context='list_backends',
                                  source='api_v1.agent_backends.status')
    return api_ok({'backends': backends})


__all__ = ['api_v1_agent_backends_bp']

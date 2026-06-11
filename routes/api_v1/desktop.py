"""routes/api_v1/desktop.py — Desktop-agent status probe.

Single read-only route. Reports whether the desktop agent is currently
connected (last poll within 15 s) plus how many commands are pending in
the queue. Used by the in-app debug panel to render a presence dot.

The actual long-poll RPC channel (``POST /api/desktop/poll``) stays at
its original path under :mod:`routes.desktop` because it's a Bridge-Secret-
authenticated long-poll between server and agent, not a JSON REST verb.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_desktop_bp = Blueprint('api_v1_desktop', __name__)


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Desktop-agent connection status',
    description=(
        'Returns ``{connected, last_poll, pending_commands}`` so the UI '
        'can render a presence indicator. Connection is defined as a '
        'poll within the last 15 s.'
    ),
    tags=['capabilities'],
)
async def desktop_status():
    from lib.desktop import (
        is_desktop_agent_connected,
        last_poll_time,
        pending_commands_count,
    )
    return jsonify({
        'connected': is_desktop_agent_connected(),
        'last_poll': last_poll_time(),
        'pending_commands': pending_commands_count(),
    })


__all__ = ['api_v1_desktop_bp']

"""routes/api_v1/browser.py — Browser-extension status surface.

Three operator-facing read endpoints. The raw extension long-poll routes
(``/api/browser/{poll, commands, result, download}``) stay at their
legacy paths because they're Bridge-Secret-authenticated long-poll RPC
between the server and the Chrome extension, not JSON REST verbs.

Routes:
  GET /api/v1/browser/status   — overall connection state + queue counts
  GET /api/v1/browser/clients  — connected clients list (per-client routing)
  GET /api/v1/browser/test     — synthetic ``list_tabs`` round-trip probe
"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_browser_bp = Blueprint('api_v1_browser', __name__)


@api_v1_browser_bp.route('/api/v1/browser/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Browser-extension connection status',
    description=(
        'Returns a snapshot of the extension bridge: ``connected``, '
        '``lastPoll`` (epoch seconds), ``secondsAgo``, the per-client '
        '``clients`` array, and pending/total command counts.'
    ),
    tags=['capabilities'],
)
def browser_status():
    from lib.browser import (
        _commands, _commands_lock, _last_poll_time,
        get_connected_clients, is_extension_connected,
    )
    connected = is_extension_connected()
    clients = get_connected_clients()
    with _commands_lock:
        pending_count = sum(1 for c in _commands.values() if not c.get('picked_up'))
        total_count = len(_commands)
    return jsonify({
        'connected': connected,
        'lastPoll': _last_poll_time,
        'secondsAgo': round(time.time() - _last_poll_time, 1) if _last_poll_time else None,
        'clients': clients,
        'pendingCommands': pending_count,
        'totalCommands': total_count,
    })


@api_v1_browser_bp.route('/api/v1/browser/clients', methods=['GET'])
@require_auth
@api_meta(
    summary='List connected browser extension clients',
    description=(
        'Returns ``{clients: [{client_id, last_poll, first_seen, name}]}`` '
        'for every extension instance that has polled within the active '
        'window. Used by the Settings UI to surface multi-device routing.'
    ),
    tags=['capabilities'],
)
def browser_clients():
    from lib.browser import get_connected_clients
    return jsonify({'clients': get_connected_clients()})


@api_v1_browser_bp.route('/api/v1/browser/test', methods=['GET'])
@require_auth
@api_meta(
    summary='Browser bridge round-trip probe',
    description=(
        'Issues a synthetic ``list_tabs`` command to the connected '
        'extension (or the specific ``clientId`` query param) and '
        'returns the response. Returns ``503`` if no extension is '
        'connected, ``502`` if the bridge replied with an error.'
    ),
    tags=['capabilities'],
)
def browser_test():
    from lib.browser import (
        _commands, _commands_lock, _last_poll_time,
        get_connected_clients, is_extension_connected,
        send_browser_command,
    )
    client_id = request.args.get('clientId') or None
    status = {
        'connected': is_extension_connected(client_id),
        'lastPoll': round(time.time() - _last_poll_time, 1) if _last_poll_time else None,
        'clients': get_connected_clients(),
    }
    with _commands_lock:
        status['pendingCommands'] = len(_commands)
        status['commandIds'] = list(_commands.keys())[:5]
    if not is_extension_connected(client_id):
        return jsonify({'status': status, 'error': 'Extension not connected'}), 503
    result, error = send_browser_command('list_tabs', timeout=10, client_id=client_id)
    if error:
        return jsonify({'status': status, 'result': result, 'error': error}), 502
    return jsonify({'status': status, 'result': result, 'error': error})


__all__ = ['api_v1_browser_bp']

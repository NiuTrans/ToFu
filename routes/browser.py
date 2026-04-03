"""routes/browser.py — Browser Extension Bridge API."""

import io
import os
import time
import zipfile

from flask import Blueprint, jsonify, request, send_file

from lib.log import get_logger

logger = get_logger(__name__)

browser_bp = Blueprint('browser', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@browser_bp.after_request
def _browser_cors(response):
    """Add CORS headers for /api/browser/* so Chrome Extension can reach us."""
    if request.path.startswith('/api/browser/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
    return response


@browser_bp.route('/api/browser/poll', methods=['POST', 'OPTIONS'])
def browser_poll():
    if request.method == 'OPTIONS':
        return '', 204
    from lib.browser import mark_poll, resolve_batch, wait_for_commands
    data = request.get_json(silent=True) or {}
    client_id = data.get('clientId') or None
    mark_poll(client_id)
    results = data.get('results', [])
    if results:
        logger.info('[Browser] poll received %d result(s) from client=%s: cmd_ids=%s',
                    len(results), (client_id or 'anon')[:12],
                    [r.get('id', '?')[:8] for r in results])
        resolve_batch(results)
    commands = wait_for_commands(timeout=8, client_id=client_id)
    if commands:
        logger.info('[Browser] poll returning %d command(s) to client=%s: %s',
                    len(commands), (client_id or 'anon')[:12],
                    [(c.get('type', '?'), c.get('id', '?')[:8]) for c in commands])
    else:
        logger.debug('[Browser] poll idle (no commands) client=%s', (client_id or 'anon')[:12])
    return jsonify({'commands': commands})


@browser_bp.route('/api/browser/commands', methods=['GET', 'OPTIONS'])
def browser_get_commands():
    """Legacy GET commands endpoint."""
    if request.method == 'OPTIONS':
        return '', 204
    from lib.browser import mark_poll, wait_for_commands
    client_id = request.args.get('clientId') or None
    mark_poll(client_id)
    commands = wait_for_commands(timeout=8, client_id=client_id)
    return jsonify({'commands': commands})


@browser_bp.route('/api/browser/result', methods=['POST', 'OPTIONS'])
def browser_post_result():
    """Legacy POST result endpoint."""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True) or {}
    cmd_id = data.get('id', '')
    if not cmd_id:
        logger.warning('[Browser] result POST missing command id')
        return jsonify({'error': 'No command id'}), 400
    from lib.browser import resolve_command
    has_error = data.get('error')
    if has_error:
        logger.warning('[Browser] result for cmd %s has error: %s', cmd_id[:8], str(has_error)[:200])
    ok = resolve_command(cmd_id, result=data.get('result'), error=has_error)
    if not ok:
        logger.warning('[Browser] result for cmd %s — command not found or expired', cmd_id[:8])
        return jsonify({'error': 'Command not found or expired'}), 404
    logger.info('[Browser] result resolved for cmd %s', cmd_id[:8])
    return jsonify({'ok': True})


@browser_bp.route('/api/browser/status', methods=['GET'])
def browser_status():
    from lib.browser import _commands, _commands_lock, _last_poll_time, get_connected_clients, is_extension_connected
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


@browser_bp.route('/api/browser/clients', methods=['GET'])
def browser_clients():
    """List all connected browser extension clients."""
    from lib.browser import get_connected_clients
    return jsonify({'clients': get_connected_clients()})


@browser_bp.route('/api/browser/test', methods=['GET'])
def browser_test():
    from lib.browser import (
        _commands,
        _commands_lock,
        _last_poll_time,
        get_connected_clients,
        is_extension_connected,
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


@browser_bp.route('/api/browser/download', methods=['GET'])
def browser_download():
    ext_dir = os.path.join(BASE_DIR, 'browser_extension')
    if not os.path.isdir(ext_dir):
        logger.warning('[Browser] download requested but extension directory not found: %s', ext_dir)
        return jsonify({'error': 'Extension directory not found'}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(ext_dir):
            for f in files:
                fp = os.path.join(root, f)
                arcname = os.path.join('browser_extension', os.path.relpath(fp, ext_dir))
                zf.write(fp, arcname)
    buf.seek(0)
    logger.info('[Browser] extension zip downloaded (%d bytes)', buf.getbuffer().nbytes)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='browser_extension.zip')

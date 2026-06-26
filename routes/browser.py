"""routes/browser.py — Browser Extension Bridge API."""

import hmac
import io
import os
import threading
import zipfile

from flask import Blueprint, jsonify, request, send_file

from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import async_parse_body, parse_body

logger = get_logger(__name__)

browser_bp = Blueprint('browser', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── One-time startup audit when bridge auth is configured ──
# Emits a single audit_log entry the first time enforcement runs, so
# operators have a trace that the §10.4-gated change is in effect.
_AUDIT_LOCK = threading.Lock()
_AUDIT_LOGGED = False


def _maybe_audit_enforcement_on() -> None:
    global _AUDIT_LOGGED
    if _AUDIT_LOGGED:
        return
    with _AUDIT_LOCK:
        if _AUDIT_LOGGED:
            return
        _AUDIT_LOGGED = True
        try:
            audit_log('config_change',
                      param='bridge_auth_enforcement',
                      old='permissive (Phase A)',
                      new='enforcing (Phase C)',
                      reason='TOFU_BRIDGE_SECRET configured — bridge endpoints '
                             'now reject requests without a matching X-Bridge-Secret',
                      approved_by='user')
        except Exception as e:
            logger.debug('[Browser] startup audit_log failed: %s', e)


def _check_bridge_auth(kind: str = 'browser') -> bool:
    """Verify the optional X-Bridge-Secret header.

    Behaviour:
      * If TOFU_BRIDGE_SECRET is unset →
        return True (auth disabled, current LAN-only default).
      * If set and the header matches (timing-safe compare) → return True.
      * If set and the header is missing or wrong → emit an audit entry
        and return False. Callers must abort(401) when this returns False.

    Reads the env var at call time rather than module load so Settings UI
    hot-reload works the same way as proxy / model config.
    """
    expected = (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip()
    if not expected:
        return True
    _maybe_audit_enforcement_on()
    provided = request.headers.get('X-Bridge-Secret', '')
    if provided and hmac.compare_digest(provided, expected):
        return True
    try:
        audit_log('bridge_auth_fail',
                  kind=kind,
                  path=request.path,
                  ip=request.remote_addr,
                  has_header=bool(provided),
                  ua=(request.user_agent.string or '')[:120])
    except Exception as _aerr:
        logger.debug('[Browser] audit_log bridge_auth_fail failed: %s', _aerr)
    logger.warning('[Browser] bridge auth rejected from %s on %s (header=%s)',
                   request.remote_addr, request.path, 'present' if provided else 'missing')
    return False


def _bridge_unauthorized():
    """Return a uniform 401 JSON envelope for bridge auth failures."""
    return jsonify({
        'error': 'bridge_auth_required',
        'hint': 'set X-Bridge-Secret header to match TOFU_BRIDGE_SECRET',
    }), 401


@browser_bp.route('/api/browser/poll', methods=['POST', 'OPTIONS'])
async def browser_poll():
    if request.method == 'OPTIONS':
        return '', 204
    if not _check_bridge_auth('browser'):
        return _bridge_unauthorized()
    from lib.browser import mark_poll, resolve_batch, wait_for_commands_async
    data = await async_parse_body()
    client_id = data.get('clientId') or None
    mark_poll(client_id)
    results = data.get('results', [])
    if results:
        logger.info('[Browser] poll received %d result(s) from client=%s: cmd_ids=%s',
                    len(results), (client_id or 'anon')[:12],
                    [r.get('id', '?')[:8] for r in results])
        resolve_batch(results)
    # Async-native wait: releases the worker thread for the whole long-poll
    # window instead of pinning it on a threading.Event (see
    # lib.browser.queue.wait_for_commands_async).
    commands = await wait_for_commands_async(client_id=client_id)
    if commands:
        logger.info('[Browser] poll returning %d command(s) to client=%s: %s',
                    len(commands), (client_id or 'anon')[:12],
                    [(c.get('type', '?'), c.get('id', '?')[:8]) for c in commands])
    else:
        logger.debug('[Browser] poll idle (no commands) client=%s', (client_id or 'anon')[:12])
    return jsonify({'commands': commands})


@browser_bp.route('/api/browser/commands', methods=['GET', 'OPTIONS'])
async def browser_get_commands():
    """Legacy GET commands endpoint."""
    if request.method == 'OPTIONS':
        return '', 204
    if not _check_bridge_auth('browser'):
        return _bridge_unauthorized()
    from lib.browser import mark_poll, wait_for_commands_async
    client_id = request.args.get('clientId') or None
    mark_poll(client_id)
    commands = await wait_for_commands_async(client_id=client_id)
    return jsonify({'commands': commands})


@browser_bp.route('/api/browser/result', methods=['POST', 'OPTIONS'])
def browser_post_result():
    """Legacy POST result endpoint."""
    if request.method == 'OPTIONS':
        return '', 204
    if not _check_bridge_auth('browser'):
        return _bridge_unauthorized()
    data = parse_body()
    cmd_id = data.get('id', '')
    if not cmd_id:
        logger.warning('[Browser] result POST missing command id')
        return api_bad_request('No command id')
    from lib.browser import resolve_command
    has_error = data.get('error')
    if has_error:
        logger.warning('[Browser] result for cmd %s has error: %s', cmd_id[:8], str(has_error)[:200])
    ok = resolve_command(cmd_id, result=data.get('result'), error=has_error)
    if not ok:
        logger.warning('[Browser] result for cmd %s — command not found or expired', cmd_id[:8])
        return api_not_found('Command not found or expired')
    logger.info('[Browser] result resolved for cmd %s', cmd_id[:8])
    return api_ok()
# Operator-facing status / clients / test endpoints moved to
# routes/api_v1/browser.py. The remaining bridge-secret-authenticated
# long-poll routes (poll/commands/result) and the binary download stay
# here since they're not JSON REST verbs.


@browser_bp.route('/api/browser/download', methods=['GET'])
def browser_download():
    ext_dir = os.path.join(BASE_DIR, 'browser_extension')
    if not os.path.isdir(ext_dir):
        logger.warning('[Browser] download requested but extension directory not found: %s', ext_dir)
        return api_not_found('Extension directory not found')
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

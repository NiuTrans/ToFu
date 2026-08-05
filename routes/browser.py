"""routes/browser.py — Browser Extension Bridge API."""

import io
import json
import os
import time
import zipfile
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, send_file

from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import async_parse_body, parse_body
from routes._bridge_caller import (
    bridge_unauthorized as _bridge_unauthorized,
    check_bridge_auth as _check_bridge_auth,
    resolve_bridge_caller as _resolve_bridge_caller,
)

logger = get_logger(__name__)

browser_bp = Blueprint('browser', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Bridge authentication lives in routes/_bridge_caller.py, shared with the
# desktop bridge so the two identity layers are literally the same object
# (B0 §5.3): the resolver returns (ok, user_id, key_id) — a per-user
# agents:bridge token is accepted AND its identity is threaded into the
# queue (mark_poll / wait_for_commands_async), which is what makes the
# fail-closed cross-tenant delivery gate reachable from this HTTP entry.


@browser_bp.route('/api/browser/poll', methods=['POST', 'OPTIONS'])
async def browser_poll():
    if request.method == 'OPTIONS':
        return '', 204
    _auth_ok, _bridge_user, _bridge_key = _resolve_bridge_caller('browser')
    if not _auth_ok:
        # A 401 answered BY THIS GATE (a proxy's 401 never reaches this
        # process) means an installed extension holding a stale/revoked
        # credential — the stranded fleet, which cannot heal itself (no
        # update channel, and a parked 401 client cannot poll). Record who
        # knocked so the panel can tell "installed but locked out" from
        # "never installed" and offer the one-click preseeded re-download.
        try:
            from lib.browser import mark_locked_out
            _rej = await async_parse_body()
            mark_locked_out((_rej or {}).get('clientId') or None,
                            ext_version=str((_rej or {}).get('extVersion') or '')[:32])
        except Exception as e:
            logger.debug('[Browser] locked-out mark failed: %s', e)
        return _bridge_unauthorized()
    from lib.browser import mark_poll, resolve_batch, wait_for_commands_async
    data = await async_parse_body()
    client_id = data.get('clientId') or None
    try:
        chrome_major = int(data.get('chromeMajor') or 0)
    except (ValueError, TypeError) as e:
        logger.debug('[Browser] non-numeric chromeMajor from client=%s: %s',
                     (client_id or 'anon')[:12], e)
        chrome_major = 0
    mark_poll(client_id, chrome_major=chrome_major, user_id=_bridge_user,
              ext_version=str(data.get('extVersion') or '')[:32])
    results = data.get('results', [])
    if results:
        logger.info('[Browser] poll received %d result(s) from client=%s: cmd_ids=%s',
                    len(results), (client_id or 'anon')[:12],
                    [r.get('id', '?')[:8] for r in results])
        resolve_batch(results)
    # Async-native wait: releases the worker thread for the whole long-poll
    # window instead of pinning it on a threading.Event (see
    # lib.browser.queue.wait_for_commands_async).
    commands = await wait_for_commands_async(client_id=client_id, user_id=_bridge_user)
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
    _auth_ok, _bridge_user, _bridge_key = _resolve_bridge_caller('browser')
    if not _auth_ok:
        return _bridge_unauthorized()
    from lib.browser import mark_poll, wait_for_commands_async
    client_id = request.args.get('clientId') or None
    mark_poll(client_id, user_id=_bridge_user)
    commands = await wait_for_commands_async(client_id=client_id, user_id=_bridge_user)
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


def _external_base_url():
    """The address the DOWNLOADING browser can poll us on again later.

    ``request.host_url`` alone is NOT that address under a path-prefixed,
    TLS-terminating cloud-IDE gateway (e.g. ``…/proxy/15000/``): the scheme
    downgrades to http (TLS ends at the edge; ProxyFix deliberately
    unwired) and the prefix is stripped before forwarding, so a preseed
    baked from it points the extension at the gateway's DEFAULT route —
    whose app answers ``POST /api/browser/poll`` with 405 and never
    forwards (owner incident 2026-08-04: extension parked on "HTTP 405",
    zero polls in access.log).

    Priority:
      1. ``?base=`` — the panel's own ``location.origin + BASE_PATH``, the
         address this browser demonstrably reaches us on. Pinned to the
         request's Host so a crafted link can never steer a freshly-minted
         bridge key toward a foreign host.
      2. ``VSCODE_PROXY_URI`` with ``{{port}}`` filled from the socket the
         request arrived on — the platform's canonical external-URL
         template, covering downloads that bypass the panel.
      3. ``request.host_url`` — correct on direct (unproxied) connections.
    """
    base = (request.args.get('base') or '').strip().rstrip('/')
    if base:
        try:
            parsed = urlparse(base)
        except ValueError as e:
            logger.debug('[Browser] unparseable base= rejected: %r (%s)',
                         base[:120], e)
            parsed = None
        if (parsed is not None
                and parsed.scheme in ('http', 'https')
                and parsed.netloc.lower() == (request.host or '').lower()
                and not parsed.query and not parsed.fragment):
            return base
        logger.warning('[Browser] download base= rejected (want host %r): %r',
                       request.host, base[:120])
    tmpl = os.environ.get('VSCODE_PROXY_URI', '')
    if '{{port}}' in tmpl:
        # The internal listen port the proxy maps its /proxy/<port>/ to.
        # Host header first (direct connections carry it); the ASGI
        # scope's server tuple behind a gateway (whose Host is external
        # and portless). urlparse().port raises ValueError on garbage.
        port = ''
        try:
            _p = urlparse('//' + (request.host or '')).port
            port = str(_p) if _p else ''
        except ValueError as e:
            logger.debug('[Browser] host port parse failed: %s', e)
            port = ''
        if not port:
            server = getattr(request, 'scope', {}).get('server') or ()
            port = str(server[1]) if len(server) == 2 and server[1] else ''
        if port:
            return tmpl.replace('{{port}}', port).rstrip('/')
    return request.host_url.rstrip('/')


def _build_bridge_preseed():
    """Mint a fresh bridge credential for THIS download and return the
    preseed payload, or ``None`` when minting is impossible.

    Owner directive 2026-08-03: the downloaded extension must pair with ZERO
    user input — nobody should have to paste a key that only the backend can
    mint. Same shape as the desktop agent's connection-line preseed: the zip
    inherits the caller's download-time auth, so baking the credential in is
    no wider a grant than the download itself.

    A NEW key is minted per download (secrets are stored hashed, so an
    existing key can never be re-materialised for packaging). Fail-open:
    any mint failure degrades to a zip WITHOUT the preseed file — the
    extension stays installable, the popup field remains as the repair path.
    """
    try:
        from routes.api_v1.auth import current_auth
        from lib.api_keys import create_key
        from lib.log import audit_log
        ctx = current_auth()
        uid = (ctx.user_id if ctx and getattr(ctx, 'user_id', '') else '') or ''
        row, token = create_key(
            'browser-ext-preseed-%s' % time.strftime('%Y%m%d'),
            scopes=['agents:bridge'], user_id=uid)
        audit_log('browser_extension_preseed_minted',
                  key_id=row.get('id'), user_id=uid)
        return {
            # The URL the user's browser just used to reach us — by
            # definition the one the extension (same browser) can poll.
            # NOT bare request.host_url: that loses the external scheme +
            # proxy path prefix behind a cloud-IDE gateway (405 incident).
            'serverUrl': _external_base_url(),
            'bridgeSecret': token,
        }
    except Exception as e:
        logger.warning('[Browser] bridge preseed mint failed (serving zip '
                       'without it): %s', e)
        return None


@browser_bp.route('/api/browser/download', methods=['GET'])
def browser_download():
    ext_dir = os.path.join(BASE_DIR, 'browser_extension')
    if not os.path.isdir(ext_dir):
        logger.warning('[Browser] download requested but extension directory not found: %s', ext_dir)
        return api_not_found('Extension directory not found')
    preseed = _build_bridge_preseed()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(ext_dir):
            for f in files:
                fp = os.path.join(root, f)
                arcname = os.path.join('browser_extension', os.path.relpath(fp, ext_dir))
                zf.write(fp, arcname)
        if preseed:
            zf.writestr('browser_extension/bridge_preseed.json',
                        json.dumps(preseed))
    buf.seek(0)
    logger.info('[Browser] extension zip downloaded (%d bytes, preseed=%s)',
                buf.getbuffer().nbytes, bool(preseed))
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='browser_extension.zip')

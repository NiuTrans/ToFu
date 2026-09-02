"""routes/browser.py — Browser Extension Bridge API."""

import io
import json
import logging
import os
import time
import zipfile
from urllib.parse import urlparse

from quart import Blueprint, jsonify, request

from lib.browser.log_safety import text_for_log
from lib.quart_sync import send_file

from lib.log import get_logger, log_event
from lib.api_response import (
    api_bad_request, api_error, api_not_found, api_ok,
    api_payload_too_large, api_service_unavailable,
)
from lib.request_parser import async_parse_body
from routes._bridge_caller import (
    browser_poll_admission_rejection,
    bridge_unauthorized as _bridge_unauthorized,
    resolve_bridge_caller as _resolve_bridge_caller,
)

logger = get_logger(__name__)

browser_bp = Blueprint('browser', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAX_FILE_TRANSFER_CONTROL_BYTES = 16 * 1024

# Bridge authentication lives in routes/_bridge_caller.py, shared with the
# desktop bridge so the two identity layers are literally the same object
# (B0 §5.3): the resolver returns (ok, user_id, key_id) — a per-user
# agents:bridge token is accepted AND its identity is threaded into the
# queue (mark_poll / wait_for_commands_async), which is what makes the
# fail-closed cross-tenant delivery gate reachable from this HTTP entry.


def _file_transfer_caller():
    """Authenticate and bind a transfer request to its polling device."""
    auth_ok, owner_user_id, _bridge_key = _resolve_bridge_caller('browser')
    if not auth_ok:
        return None, _bridge_unauthorized()
    client_id = str(request.headers.get('X-Browser-Client-Id') or '').strip()
    if not client_id:
        return None, api_bad_request('X-Browser-Client-Id is required')
    token = str(request.headers.get('X-Transfer-Token') or '').strip()
    if not token:
        return None, api_bad_request('X-Transfer-Token is required')
    return {
        'owner_user_id': str(owner_user_id),
        'client_id': client_id,
        'token': token,
    }, None


def api_file_transfer_error(exc):
    """Project the transport's stable code/status without exposing secrets."""
    from lib.browser.file_transfer import BrowserFileTransferError

    if isinstance(exc, BrowserFileTransferError):
        return api_error(str(exc), status=exc.status, code=exc.code)
    logger.error('[BrowserFileTransfer] unexpected route failure (%s): %s',
                 type(exc).__name__, text_for_log(exc))
    return api_error(
        'Browser file transfer failed unexpectedly',
        status=500,
        code='browser_file_transfer_internal',
    )


async def _read_transfer_chunk(max_bytes: int) -> bytes | None:
    """Read raw request bytes without an unbounded ``get_data()`` buffer."""
    payload = bytearray()
    async for segment in request.body:
        if len(payload) + len(segment) > max_bytes:
            return None
        payload.extend(segment)
    return bytes(payload)


async def _read_file_transfer_json():
    """Parse one bounded control envelope; the app-wide limit is 520 MiB."""
    if (request.content_length is not None
            and request.content_length > _MAX_FILE_TRANSFER_CONTROL_BYTES):
        return None, api_payload_too_large(
            _MAX_FILE_TRANSFER_CONTROL_BYTES,
            code='browser_file_transfer_control_too_large',
        )
    payload = await _read_transfer_chunk(_MAX_FILE_TRANSFER_CONTROL_BYTES)
    if payload is None:
        return None, api_payload_too_large(
            _MAX_FILE_TRANSFER_CONTROL_BYTES,
            code='browser_file_transfer_control_too_large',
        )
    try:
        body = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, api_bad_request('Valid JSON object required')
    if not isinstance(body, dict):
        return None, api_bad_request('JSON object required')
    return body, None


@browser_bp.route(
    '/api/browser/file-transfers/<transfer_id>/start',
    methods=['POST', 'OPTIONS'],
)
async def browser_file_transfer_start(transfer_id):
    """Accept authenticated upstream response metadata before any bytes."""
    if request.method == 'OPTIONS':
        return '', 204
    caller, rejection = _file_transfer_caller()
    if rejection:
        return rejection
    body, rejection = await _read_file_transfer_json()
    if rejection:
        return rejection
    try:
        from lib.browser.file_transfer import file_transfer_store
        result = file_transfer_store.start(
            transfer_id,
            owner_user_id=caller['owner_user_id'],
            client_id=caller['client_id'],
            token=caller['token'],
            final_url=body.get('finalUrl'),
            response_status=body.get('responseStatus'),
            content_type=body.get('contentType') or '',
            content_disposition=body.get('contentDisposition') or '',
            content_length=body.get('contentLength'),
            suggested_filename=body.get('suggestedFilename') or '',
        )
        return api_ok(result)
    except Exception as exc:
        logger.debug(
            '[BrowserFileTransfer] start rejected: %s',
            type(exc).__name__,
        )
        return api_file_transfer_error(exc)


@browser_bp.route(
    '/api/browser/file-transfers/<transfer_id>/chunks/<int:sequence>',
    methods=['PUT', 'OPTIONS'],
)
async def browser_file_transfer_chunk(transfer_id, sequence):
    """Append one bounded, ordered, idempotent binary chunk."""
    if request.method == 'OPTIONS':
        return '', 204
    caller, rejection = _file_transfer_caller()
    if rejection:
        return rejection
    from lib.browser.file_transfer import MAX_CHUNK_BYTES
    if request.content_length is not None \
            and request.content_length > MAX_CHUNK_BYTES:
        return api_payload_too_large(
            MAX_CHUNK_BYTES,
            code='browser_file_transfer_chunk_too_large',
        )
    payload = await _read_transfer_chunk(MAX_CHUNK_BYTES)
    if payload is None:
        return api_payload_too_large(
            MAX_CHUNK_BYTES,
            code='browser_file_transfer_chunk_too_large',
        )
    try:
        from lib.browser.file_transfer import file_transfer_store
        result = file_transfer_store.append_chunk(
            transfer_id,
            sequence,
            payload,
            owner_user_id=caller['owner_user_id'],
            client_id=caller['client_id'],
            token=caller['token'],
            declared_sha256=request.headers.get('X-Chunk-SHA256', ''),
        )
        return api_ok(result)
    except Exception as exc:
        logger.debug(
            '[BrowserFileTransfer] chunk rejected: %s',
            type(exc).__name__,
        )
        return api_file_transfer_error(exc)


@browser_bp.route(
    '/api/browser/file-transfers/<transfer_id>/complete',
    methods=['POST', 'OPTIONS'],
)
async def browser_file_transfer_complete(transfer_id):
    """Verify totals and atomically publish the server-staging file."""
    if request.method == 'OPTIONS':
        return '', 204
    caller, rejection = _file_transfer_caller()
    if rejection:
        return rejection
    body, rejection = await _read_file_transfer_json()
    if rejection:
        return rejection
    try:
        from lib.browser.file_transfer import file_transfer_store
        result = file_transfer_store.complete(
            transfer_id,
            owner_user_id=caller['owner_user_id'],
            client_id=caller['client_id'],
            token=caller['token'],
            total_bytes=body.get('totalBytes'),
            chunk_count=body.get('chunkCount'),
        )
        return api_ok(result)
    except Exception as exc:
        logger.debug(
            '[BrowserFileTransfer] completion rejected: %s',
            type(exc).__name__,
        )
        return api_file_transfer_error(exc)


@browser_bp.route(
    '/api/browser/file-transfers/<transfer_id>',
    methods=['DELETE', 'OPTIONS'],
)
async def browser_file_transfer_abort(transfer_id):
    """Delete reconstructible partial/completed transfer data."""
    if request.method == 'OPTIONS':
        return '', 204
    caller, rejection = _file_transfer_caller()
    if rejection:
        return rejection
    try:
        from lib.browser.file_transfer import file_transfer_store
        removed = file_transfer_store.abort(
            transfer_id,
            owner_user_id=caller['owner_user_id'],
            client_id=caller['client_id'],
            token=caller['token'],
        )
        return api_ok(aborted=bool(removed))
    except Exception as exc:
        logger.debug(
            '[BrowserFileTransfer] abort rejected: %s',
            type(exc).__name__,
        )
        return api_file_transfer_error(exc)


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
            from lib.browser.queue import mark_locked_out
            from lib.bridge_auth import identify_rejected_bridge_owner
            _rej = await async_parse_body()
            rejected_owner = identify_rejected_bridge_owner(
                request.headers.get('X-Bridge-Secret', ''))
            if rejected_owner:
                mark_locked_out(
                    (_rej or {}).get('clientId') or None,
                    owner_user_id=rejected_owner,
                    ext_version=str(
                        (_rej or {}).get('extVersion') or '')[:32],
                )
        except Exception as e:
            logger.debug('[Browser] locked-out mark failed: %s', e)
        return _bridge_unauthorized()
    from lib.browser.queue import (
        BrowserPollCapacityExceeded,
        MAX_RESULTS_PER_POLL,
        mark_poll,
        resolve_batch,
        wait_for_commands_async,
    )
    data = await async_parse_body()
    if not isinstance(data, dict):
        return api_bad_request('JSON object required')
    raw_client_id = data.get('clientId')
    if not isinstance(raw_client_id, str):
        return api_bad_request('clientId must be a string')
    client_id = raw_client_id.strip()
    if not client_id:
        return api_bad_request('clientId is required')
    if len(client_id) > 128:
        return api_bad_request('clientId must be at most 128 characters')
    results = data.get('results', [])
    if not isinstance(results, list) or not all(
            isinstance(result, dict) for result in results):
        return api_bad_request('results must be an array of objects')
    if len(results) > MAX_RESULTS_PER_POLL:
        return api_error(
            'browser_poll_results_too_large',
            status=413,
            code='browser_poll_results_too_large',
            maxResults=MAX_RESULTS_PER_POLL,
        )
    for result in results:
        result_id = result.get('id')
        if not isinstance(result_id, str) or not result_id:
            return api_bad_request('each result id must be a non-empty string')
        if len(result_id) > 128:
            return api_bad_request(
                'each result id must be at most 128 characters')
    from lib.browser.protocol import (
        ALL_CAPABILITIES,
        BrowserProtocolRejected,
        PROTOCOL_VERSION,
    )
    capabilities = data.get('capabilities')
    raw_protocol_version = data.get('protocolVersion')
    try:
        reported_current_protocol = (
            not isinstance(raw_protocol_version, bool)
            and int(raw_protocol_version) == PROTOCOL_VERSION
        )
    except (TypeError, ValueError, OverflowError):
        reported_current_protocol = False
    if reported_current_protocol:
        if not isinstance(capabilities, list):
            return api_bad_request('capabilities must be an array')
        if len(capabilities) > len(ALL_CAPABILITIES):
            return api_bad_request('capabilities has too many entries')
        if any(
            not isinstance(capability, str) or len(capability) > 64
            for capability in capabilities
        ):
            return api_bad_request(
                'each capability must be a string of at most 64 characters')
        for field_name, max_length in (('extVersion', 32), ('profile', 80)):
            value = data.get(field_name, '')
            if not isinstance(value, str) or len(value) > max_length:
                return api_bad_request(
                    f'{field_name} must be a string of at most '
                    f'{max_length} characters')
    try:
        chrome_major = int(data.get('chromeMajor') or 0)
    except (ValueError, TypeError) as e:
        logger.debug('[Browser] non-numeric chromeMajor from client=%s: %s',
                     client_id[:12], e)
        chrome_major = 0
    try:
        mark_poll(
            client_id,
            owner_user_id=str(_bridge_user or ''),
            chrome_major=chrome_major,
            ext_version=str(data.get('extVersion') or '')[:32],
            protocol_version=raw_protocol_version,
            capabilities=capabilities,
            profile=str(data.get('profile') or '')[:80],
        )
    except BrowserProtocolRejected as exc:
        from lib.browser.queue import mark_incompatible_client
        from lib.browser.poll_admission import browser_poll_admission
        first_rejection = mark_incompatible_client(
            client_id,
            owner_user_id=str(_bridge_user or ''),
            ext_version=str(data.get('extVersion') or '')[:32],
            protocol_version=data.get('protocolVersion'),
            reason=str(exc),
        )
        if first_rejection:
            log_event(
                logger,
                logging.WARNING,
                'browser.protocol_rejected',
                '[Browser] extension upgrade required for client=%s '
                '(ext=%s, reported_protocol=%s, required_protocol=%s)',
                client_id[:12],
                str(data.get('extVersion') or '?')[:32],
                data.get('protocolVersion'),
                PROTOCOL_VERSION,
                client_id=client_id[:12],
                extension_version=str(data.get('extVersion') or '')[:32],
                reported_protocol=data.get('protocolVersion'),
                required_protocol=PROTOCOL_VERSION,
            )
        browser_poll_admission().note_protocol_rejection(
            credential=request.headers.get('X-Bridge-Secret', ''),
            client_protocol_version=data.get('protocolVersion'),
        )
        response, status = api_error(
            str(exc),
            status=426,
            code='browser_protocol_upgrade_required',
            upgradeRequired=True,
            requiredProtocolVersion=PROTOCOL_VERSION,
            clientProtocolVersion=data.get('protocolVersion'),
        )
        response.headers['Retry-After'] = '300'
        return response, status
    except BrowserPollCapacityExceeded as exc:
        from lib.browser.poll_admission import BrowserPollAdmissionDecision
        return browser_poll_admission_rejection(
            BrowserPollAdmissionDecision(False, exc.code, 1))
    except (TypeError, ValueError) as exc:
        logger.info('[Browser] rejected poll client=%s: %s',
                    client_id[:12], exc)
        return api_bad_request(str(exc))
    if results:
        logger.info('[Browser] poll received %d result(s) from client=%s: cmd_ids=%s',
                    len(results), client_id[:12],
                    [str(r.get('id', '?'))[:8] for r in results])
        resolve_batch(
            results,
            client_id=client_id,
            owner_user_id=str(_bridge_user),
        )
    # Async-native wait: releases the worker thread for the whole long-poll
    # window instead of pinning it on a threading.Event (see
    # lib.browser.queue.wait_for_commands_async).
    try:
        commands = await wait_for_commands_async(
            client_id=client_id,
            owner_user_id=str(_bridge_user),
        )
    except BrowserPollCapacityExceeded as exc:
        from lib.browser.poll_admission import BrowserPollAdmissionDecision
        return browser_poll_admission_rejection(
            BrowserPollAdmissionDecision(False, exc.code, 1))
    if commands:
        logger.info('[Browser] poll returning %d command(s) to client=%s: %s',
                    len(commands), client_id[:12],
                    [(c.get('type', '?'), c.get('id', '?')[:8]) for c in commands])
    else:
        logger.debug('[Browser] poll idle (no commands) client=%s', client_id[:12])
    return jsonify({'commands': commands, 'protocolVersion': PROTOCOL_VERSION})
# Operator-facing status / clients / test endpoints moved to
# routes/api_v1/browser.py. The remaining bridge-secret-authenticated
# poll route and binary download stay here because they are device transport,
# not user-facing JSON REST verbs.


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
    existing key can never be re-materialised for packaging). Mint failure is
    explicit: serving an unpaired package would defer a known failure until
    after installation and create a worse recovery experience.
    """
    try:
        from routes.api_v1.auth import current_auth, request_principal
        from lib.api_keys import create_key
        from lib.log import audit_log
        ctx = current_auth()
        principal = request_principal()
        owner_user_id = principal.require_owner(
            context='browser extension credential')
        row, token = create_key(
            'browser-ext-preseed-%s' % time.strftime('%Y%m%d'),
            scopes=['agents:bridge'], owner_user_id=owner_user_id,
            account_user_id=(ctx.account_user_id if ctx else ''),
            tenant_id=principal.tenant_id)
        audit_log('browser_extension_preseed_minted',
                  key_id=row.get('id'), owner_user_id=owner_user_id)
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
    if preseed is None:
        return api_service_unavailable(
            'browser_extension_credential_unavailable',
            message='the paired browser extension is temporarily unavailable; '
                    'retry shortly',
        )
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

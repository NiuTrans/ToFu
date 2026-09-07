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

import os
import shutil
import subprocess
import sys
import time

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request, api_forbidden, api_internal_error, api_not_found, api_ok,
    api_payload,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.ttl_cache import TTLCache

from lib.request_parser import parse_body

from .auth import current_auth, require_auth

logger = get_logger(__name__)

api_v1_browser_bp = Blueprint('api_v1_browser', __name__)


@api_v1_browser_bp.route('/api/v1/browser/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Browser-extension connection status',
    description=(
        'Returns a snapshot of the extension bridge: ``connected``, '
        '``lastPoll`` (epoch seconds), ``secondsAgo``, the per-client '
        '``clients`` array, ``chromeMajor`` (highest Chromium major version '
        'across connected clients, for LNA-prompt guidance), '
        '``servedExtVersion``, owner-scoped ``lockedOutClients`` and '
        '``incompatibleClients`` recovery signals, '
        'pending/total command counts, and ``localBrowser`` — the '
        '``{family, name, extensionsUrl}`` of the Chromium-family browser '
        'this machine can drive, or ``null``. The UI keys the guided-install '
        'button off ``localBrowser``: with no browser to open there is no '
        'button, because a control that cannot achieve what it claims must '
        'not invite the click.'
    ),
    tags=['capabilities'],
)
def browser_status():
    import os

    from lib.browser.queue import (
        _commands, _commands_lock,
        get_connected_clients,
    )
    ctx = current_auth()
    user_id = str(ctx.owner_user_id or '')
    clients = get_connected_clients(owner_user_id=user_id)
    connected = bool(clients)
    from lib.browser.queue import (
        get_incompatible_clients,
        get_locked_out_clients,
    )
    locked_out = get_locked_out_clients(owner_user_id=user_id)
    incompatible = get_incompatible_clients(owner_user_id=user_id)
    # Highest Chromium major across connected clients. Chrome 142+ enforces the
    # "Local Network Access" permission prompt by default; the UI uses this to
    # surface guidance for the browser actually running the bridge.
    chrome_major = max((c.get('chrome_major', 0) or 0 for c in clients), default=0)
    with _commands_lock:
        own_commands = [c for c in _commands.values()
                        if (c.get('owner_user_id') or '') == user_id]
        pending_count = sum(1 for c in own_commands if not c.get('picked_up'))
        total_count = len(own_commands)
    # Absolute on-disk path of the unpacked extension, plus WHICH browser (if
    # any) this machine can actually drive.
    #
    # Two independent facts must BOTH hold before the path is worth sending:
    #
    #   1. The peer is loopback — a remote peer (LAN IP, Docker port-map,
    #      tunnel, cloud IDE) loads the extension into THEIR browser, where a
    #      server-side path does not exist.
    #   2. This machine actually HAS a Chromium-family browser. If it does
    #      not, then nobody is viewing this UI from this machine either, so
    #      the path is useless no matter what the socket says — and the
    #      IP test alone cannot see that, because a same-host reverse proxy
    #      (nginx / ngrok / cloudflared → 127.0.0.1, with ProxyFix unwired)
    #      reports loopback for every public request.
    #
    # The probe is the fact a proxy cannot forge, so it backs up the IP test
    # rather than trusting it alone. Failing (2) falls through to the
    # download-and-unzip instruction, which IS actionable from anywhere.
    from .auth import _remote_is_loopback
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ext_dir = os.path.join(base_dir, 'browser_extension')
    local_browser = _detect_local_browser()
    extension_path = None
    if os.path.isdir(ext_dir):
        if not _remote_is_loopback():
            logger.debug('[Browser] suppressing extensionPath for non-loopback '
                         'peer %s — remote browser cannot load the server-side '
                         'folder', request.remote_addr)
        elif not local_browser:
            logger.debug('[Browser] suppressing extensionPath: no Chromium-'
                         'family browser on this machine, so nobody is viewing '
                         'this UI from it')
        else:
            extension_path = ext_dir
    from lib.browser.fetch import last_browser_fallback
    from lib.browser.protocol import PROTOCOL_VERSION
    from lib.browser.sessions import lease_status
    freshest = max(clients, key=lambda row: row.get('last_poll', 0)) \
        if clients else {}
    user_last_poll = freshest.get('last_poll')
    return api_ok({
        'connected': connected,
        'lastPoll': user_last_poll,
        'secondsAgo': round(time.time() - user_last_poll, 1)
        if user_last_poll else None,
        'clients': clients,
        'pendingCommands': pending_count,
        'totalCommands': total_count,
        'extensionPath': extension_path,
        'chromeMajor': chrome_major,
        # Fleet recovery inputs: the version a fresh download would carry and
        # clients whose polls died at the bridge gate. Stale credentials are
        # installed but locked out, never to be shown as "not installed".
        'servedExtVersion': _served_ext_version(),
        'lockedOutClients': locked_out,
        # Authenticated devices rejected by the strict protocol handshake are
        # not connected authorities, but they are installed and recoverable.
        # Keep this owner-scoped signal distinct from dead credentials so the
        # UI can prescribe an upgrade instead of misreporting "not installed".
        'incompatibleClients': incompatible,
        # Only what the UI renders. The binary's absolute path is server
        # filesystem detail the browser has no use for.
        'localBrowser': ({'family': local_browser['family'],
                          'name': local_browser['name'],
                          'extensionsUrl': local_browser['extensionsUrl']}
                         if local_browser else None),
        'protocolVersion': PROTOCOL_VERSION,
        'clientProtocolVersion': int(freshest.get('protocol_version') or 0),
        'capabilities': list(freshest.get('capabilities') or []),
        'profile': freshest.get('profile', ''),
        'leases': lease_status(owner_user_id=user_id),
        'lastFallback': last_browser_fallback(user_id=user_id),
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
    from lib.browser.queue import get_connected_clients
    ctx = current_auth()
    user_id = str(ctx.owner_user_id or '')
    return api_ok({
        'clients': get_connected_clients(owner_user_id=user_id),
    })


def _request_user_id() -> str:
    ctx = current_auth()
    return str(ctx.owner_user_id or '')


@api_v1_browser_bp.route('/api/v1/browser/access', methods=['GET'])
@require_auth
@api_meta(
    summary='Get browser domain access policy',
    description=('Returns the current user\'s read-denied domains and durable '
                 'write grants. Cookie values and page content are never exposed.'),
    tags=['browser'],
)
def browser_access_get():
    from lib.browser.access import get_access_policy
    return api_ok(get_access_policy(_request_user_id()))


@api_v1_browser_bp.route('/api/v1/browser/access', methods=['PUT'])
@require_auth
@api_meta(
    summary='Update browser domain access policy',
    description=(
        'Replaces ``read_denied_domains`` and/or applies one ``grant`` or '
        '``revoke`` operation. Write grants are tied to this user and a '
        'specific browser client/profile; they never carry across domains.'),
    tags=['browser'],
)
def browser_access_put():
    from lib.browser.access import (
        get_access_policy, grant_write, replace_read_denials,
        replace_write_grants, revoke_write,
    )
    body = parse_body() or {}
    if not isinstance(body, dict):
        return api_bad_request('JSON object required')
    user_id = _request_user_id()
    try:
        denials = body.get('read_denied_domains', body.get('readDeniedDomains'))
        if denials is not None:
            if not isinstance(denials, list):
                return api_bad_request('read_denied_domains must be an array')
            replace_read_denials(user_id, denials)
        grants = body.get('write_grants', body.get('writeGrants'))
        if grants is not None:
            if not isinstance(grants, list):
                return api_bad_request('write_grants must be an array')
            from lib.browser.access import normalize_domain
            from lib.browser.queue import get_connected_clients
            connected_ids = {
                str(row.get('client_id') or '')
                for row in get_connected_clients(owner_user_id=user_id)
            }
            current = get_access_policy(user_id)
            existing = {
                (normalize_domain(row.get('domain')),
                 str(row.get('client_id') or row.get('clientId') or ''),
                 str(row.get('profile') or ''))
                for row in current.get('write_grants', [])
                if isinstance(row, dict)
            }
            for row in grants:
                if not isinstance(row, dict):
                    return api_bad_request(
                        'each write grant must be an object')
                identity = (
                    normalize_domain(row.get('domain')),
                    str(row.get('client_id') or row.get('clientId') or ''),
                    str(row.get('profile') or ''),
                )
                if identity not in existing and identity[1] not in connected_ids:
                    return api_bad_request(
                        'new write grants require one of your connected browsers')
            ctx = current_auth()
            replace_write_grants(
                user_id, grants,
                granted_by=str(getattr(ctx, 'owner_user_id', '')
                               or getattr(ctx, 'name', '') or ''))
        grant = body.get('grant')
        if isinstance(grant, dict):
            from lib.browser.queue import get_connected_clients
            grant_client = str(
                grant.get('client_id') or grant.get('clientId') or '')
            owned_clients = {
                str(row.get('client_id') or '')
                for row in get_connected_clients(owner_user_id=user_id)
            }
            if grant_client not in owned_clients:
                return api_bad_request(
                    'grant client_id must name one of your connected browsers')
            ctx = current_auth()
            grant_write(
                user_id, grant.get('domain', ''),
                client_id=grant_client,
                profile=grant.get('profile') or '',
                granted_by=str(getattr(ctx, 'owner_user_id', '')
                               or getattr(ctx, 'name', '') or ''))
        revoke = body.get('revoke')
        if isinstance(revoke, dict):
            revoke_write(
                user_id, revoke.get('domain', ''),
                client_id=revoke.get('client_id') or revoke.get('clientId') or '',
                profile=revoke.get('profile') or '')
        return api_ok(get_access_policy(user_id))
    except ValueError as exc:
        return api_bad_request(str(exc))


@api_v1_browser_bp.route('/api/v1/browser/adapters', methods=['GET'])
@require_auth
@api_meta(
    summary='List browser site adapters and health',
    description=('Returns commands, read/write classification, schemas, and '
                 'missing extension capabilities for each adapter.'),
    tags=['browser'],
)
def browser_adapters():
    from lib.browser.adapters import adapters_payload
    from lib.browser.queue import get_connected_clients
    clients = get_connected_clients(owner_user_id=_request_user_id())
    requested = request.args.get('clientId') or ''
    if requested and requested not in {
            str(row.get('client_id') or '') for row in clients}:
        return api_bad_request('clientId is not connected for this user')
    selected = requested or (str(max(
        clients, key=lambda row: row.get('last_poll', 0)).get('client_id') or '')
        if clients else '')
    return api_ok(adapters_payload(client_id=selected))


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
    from lib.browser.queue import (
        _commands, _commands_lock,
        get_connected_clients,
        send_browser_command,
    )
    user_id = _request_user_id()
    clients = get_connected_clients(owner_user_id=user_id)
    requested = request.args.get('clientId') or ''
    owned = {str(row.get('client_id') or '') for row in clients}
    if requested and requested not in owned:
        return api_bad_request('clientId is not connected for this user')
    client_id = requested or (str(max(
        clients, key=lambda row: row.get('last_poll', 0)).get('client_id') or '')
        if clients else '')
    connected = bool(client_id)
    status = {
        'connected': connected,
        'lastPoll': round(time.time() - max(
            (row.get('last_poll', 0) for row in clients), default=0), 1)
        if clients else None,
        'clients': clients,
    }
    with _commands_lock:
        own_commands = {
            command_id: command for command_id, command in _commands.items()
            if (command.get('owner_user_id') or '') == user_id
        }
        status['pendingCommands'] = len(own_commands)
        status['commandIds'] = list(own_commands)[:5]
    if not connected:
        return api_payload({'status': status,
                            'error': 'Extension not connected'}, 503)
    result, error = send_browser_command(
        'list_tabs',
        timeout=10,
        client_id=client_id,
        owner_user_id=user_id,
    )
    if error:
        return api_payload({'status': status, 'result': result,
                            'error': error}, 502)
    return api_ok({'status': status, 'result': result,
                   'error': error})


# ── Guided extension install: open the extensions page (loopback only) ──
#
# The merged Local Control modal walks a same-machine user through loading
# the unpacked extension. Chrome's sandbox deliberately gives a web page no
# way to flip Developer mode or click "Load unpacked" — but the SERVER, when
# the user is browsing from the very machine it runs on, can at least open
# the browser at the right page. That is ALL this route does, and the name
# says so: pretending to finish an install we cannot finish would be the
# same lie as a bare token with no address.


# Chromium-family browsers that run this extension UNCHANGED.
#
# Edge is here because it is Chromium under the hood: same `chrome.*`
# namespace, same MV3 service-worker background, same "Load unpacked" flow.
#
# Firefox is deliberately ABSENT, and not as an oversight to correct later:
# it has no persistent unpacked-install path at all (Mozilla's own docs — an
# `about:debugging` add-on lasts "until you remove it or restart Firefox",
# and end users can only install add-ons Mozilla has signed). Listing it
# would manufacture exactly the promise-we-cannot-keep this module was fixed
# to stop making. Firefox support is a signing + distribution pipeline, not a
# browser-launch table entry.
#
# Each family carries its OWN extensions URL: chrome://extensions is not
# Edge's extensions page, and handing a browser another vendor's internal URL
# lands the user nowhere useful.
_BROWSER_FAMILIES = (
    # (family, display name, extensions URL,
    #  posix names, macOS app paths, windows path parts)
    ('chrome', 'Chrome', 'chrome://extensions',
     ('google-chrome', 'google-chrome-stable', 'chrome'),
     ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',),
     (('Google', 'Chrome', 'Application', 'chrome.exe'),)),
    ('edge', 'Edge', 'edge://extensions',
     ('microsoft-edge', 'microsoft-edge-stable', 'msedge'),
     ('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',),
     (('Microsoft', 'Edge', 'Application', 'msedge.exe'),)),
    ('chromium', 'Chromium', 'chrome://extensions',
     ('chromium', 'chromium-browser'),
     ('/Applications/Chromium.app/Contents/MacOS/Chromium',),
     (('Chromium', 'Application', 'chrome.exe'),)),
)


def _probe_local_browser() -> dict | None:
    """Walk the family table and return the first browser present, or None.

    The RAW probe — uncached, hits the filesystem every call. Callers should
    use ``_detect_local_browser()`` instead; this exists separately so the
    cache has something to memoise and so tests can exercise the platform
    branches without a cache masking the result.

    Never falls back to the DEFAULT browser (xdg-open / os.startfile): on a
    machine whose default is Firefox or Safari that opens a page which cannot
    load this extension.
    """
    for (family, name, url, posix_names,
         mac_paths, win_parts) in _BROWSER_FAMILIES:
        binary = None
        if sys.platform == 'darwin':
            for cand in mac_paths:
                if os.path.isfile(cand):
                    binary = cand
                    break
        elif sys.platform == 'win32':
            for env in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
                base = os.environ.get(env)
                if not base:
                    continue
                for parts in win_parts:
                    cand = os.path.join(base, *parts)
                    if os.path.isfile(cand):
                        binary = cand
                        break
                if binary:
                    break
        if binary is None:
            for n in posix_names:
                hit = shutil.which(n)
                if hit:
                    binary = hit
                    break
        if binary:
            logger.debug('[Browser] probe: %s (%s) at %s', name, family, binary)
            return {'binary': binary, 'family': family, 'name': name,
                    'extensionsUrl': url}
    logger.debug('[Browser] probe found no Chromium-family browser here')
    return None


# Whether this machine has a browser is a fact that changes at most a couple
# of times in a machine's life, but the probe hangs off GET /status, which the
# Local Control modal polls every 3s. The MISS path is the expensive one:
# with nothing installed, every candidate name misses and each miss walks the
# whole PATH — measured here at ~408 stat() calls / ~6ms per probe on local
# disk, and this project deploys onto FUSE mounts where stat costs markedly
# more.
#
# The TTL is the load-bearing part, not the caching. "Tofu can't find my
# browser" is the ORIGINAL complaint this module was written to fix; an
# unbounded cache would hand that same report back to any user who installs a
# browser mid-session, except this time the probe would be right and the
# cache lying. 60s keeps a fresh install visible well inside the user's own
# retry patience while collapsing ~20 polls per minute into one filesystem
# walk.
#
# TTLCache (not a hand-rolled dict) because it already solves the two things
# that would otherwise be reinvented badly here: get_or_compute serialises
# concurrent missers per key so N open tabs cause ONE walk rather than a
# stampede, and every instance registers for the cgroup memory-pressure
# relief sweep (lib.ttl_cache.clear_all_caches).
_BROWSER_PROBE_CACHE = TTLCache(ttl=60, max_size=1, name='browser_probe')
_BROWSER_PROBE_KEY = 'local'

# The extension version THIS server would serve in a fresh download zip —
# read from the on-disk manifest, TTL-cached (the status endpoint is polled
# every 3s while the Local Control modal is open; the version changes only
# when the deployed source changes). The panel diffs each client's reported
# ext_version against this to tell "installed but outdated" from "current".
_SERVED_EXT_CACHE = TTLCache(ttl=60, max_size=1, name='served_ext_version')


def _served_ext_version() -> str:
    def _read() -> str:
        try:
            import json
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
            with open(os.path.join(base_dir, 'browser_extension',
                                   'manifest.json'), encoding='utf-8') as f:
                return str(json.load(f).get('version') or '')
        except Exception as e:
            logger.debug('[Browser] served ext version unreadable: %s', e)
            return ''
    return _SERVED_EXT_CACHE.get_or_compute('v', _read)


def _detect_local_browser() -> dict | None:
    """Return this machine's drivable Chromium-family browser, or ``None``.

    Cached for ``_BROWSER_PROBE_CACHE.ttl`` seconds; see that constant for
    why the expiry is mandatory rather than a tuning knob.

    This is the single source of truth for two separate UI decisions —
    whether to offer the open-the-page button at all, and whether the
    server-side unpacked-extension path is worth showing. It is a fact about
    the machine, which is what makes it strictly stronger than the IP-based
    loopback test it backs up: a same-host reverse proxy makes every public
    request *look* loopback, but it cannot conjure a browser onto a headless
    server.
    """
    return _BROWSER_PROBE_CACHE.get_or_compute(
        _BROWSER_PROBE_KEY, _probe_local_browser)


@api_v1_browser_bp.route('/api/v1/browser/open-extensions', methods=['POST'])
@require_auth
@api_meta(
    summary='Open the local Chromium-family browser at its extensions page',
    description=(
        'Side effect: launches the server machine\'s Chromium-family browser '
        '(Chrome / Edge / Chromium) at ITS OWN extensions page — one step of '
        'the guided extension install in the Local Control modal. Gated on '
        'the request peer being loopback, because the window opens on the '
        'SERVER machine, which only helps when the user is browsing from that '
        'same machine. Returns 404 when no such browser exists here; the UI '
        'consumes ``localBrowser`` from /status and does not render the '
        'button at all in that case, so this is a backstop rather than the '
        'primary signal. The remaining steps (Developer mode → Load unpacked '
        '→ pick the folder) are browser-sandboxed and cannot be automated; '
        'the UI says so rather than implying one click finishes the install.'
    ),
    tags=['capabilities'],
)
def browser_open_extensions():
    """Open THIS machine's Chromium-family browser at its extensions page."""
    from .auth import _remote_is_loopback
    if not _remote_is_loopback():
        logger.warning('[Browser] open-extensions refused: peer %s is not '
                       'loopback — the page would open on the server, not '
                       "on the user's machine", request.remote_addr)
        return api_forbidden(
            'The extensions page can only be opened when you are browsing '
            'from the same machine the server runs on.')
    browser = _detect_local_browser()
    if not browser:
        logger.info('[Browser] open-extensions: no Chromium-family browser '
                    'found on this machine')
        return api_not_found(
            'No Chromium-family browser was found on this machine — open the '
            'extensions page manually instead.')
    binary = browser['binary']
    # The URL travels WITH the browser the probe picked: edge://extensions is
    # Edge's page and chrome://extensions is not. A second hardcoded copy of
    # "which page is the extensions page" is how that silently regresses.
    url = browser['extensionsUrl']
    try:
        kwargs = {}
        if sys.platform != 'win32':
            kwargs['start_new_session'] = True
        subprocess.Popen([binary, url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **kwargs)
    except Exception as e:
        logger.error('[Browser] failed to launch %s: %s', binary, e,
                     exc_info=True)
        return api_internal_error(e, context='routes.api_v1.browser',
                                  source='browser_open_extensions')
    logger.info('[Browser] opened %s via %s (loopback user)', url, binary)
    audit_log('browser_extensions_page_opened', browser=binary,
              family=browser['family'], peer=request.remote_addr)
    return api_ok({'opened': url, 'browser': binary,
                   'browserName': browser['name']})


__all__ = ['api_v1_browser_bp']

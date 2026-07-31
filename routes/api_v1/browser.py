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

from flask import Blueprint, jsonify, request

from lib.api_response import (
    api_forbidden, api_internal_error, api_not_found, api_ok,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.ttl_cache import TTLCache

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
        '``clients`` array, ``chromeMajor`` (highest Chromium major version '
        'across connected clients, for LNA-prompt guidance), '
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

    from lib.browser import (
        _commands, _commands_lock, _last_poll_time,
        get_connected_clients, is_extension_connected,
    )
    connected = is_extension_connected()
    clients = get_connected_clients()
    # Highest Chromium major across connected clients. Chrome 142+ enforces the
    # "Local Network Access" permission prompt by default; the UI uses this to
    # surface guidance for the browser actually running the bridge.
    chrome_major = max((c.get('chrome_major', 0) or 0 for c in clients), default=0)
    with _commands_lock:
        pending_count = sum(1 for c in _commands.values() if not c.get('picked_up'))
        total_count = len(_commands)
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
    return jsonify({
        'connected': connected,
        'lastPoll': _last_poll_time,
        'secondsAgo': round(time.time() - _last_poll_time, 1) if _last_poll_time else None,
        'clients': clients,
        'pendingCommands': pending_count,
        'totalCommands': total_count,
        'extensionPath': extension_path,
        'chromeMajor': chrome_major,
        # Only what the UI renders. The binary's absolute path is server
        # filesystem detail the browser has no use for.
        'localBrowser': ({'family': local_browser['family'],
                          'name': local_browser['name'],
                          'extensionsUrl': local_browser['extensionsUrl']}
                         if local_browser else None),
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

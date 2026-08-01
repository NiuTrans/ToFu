"""routes/api_v1/desktop.py — Desktop-agent status probe.

Single read-only route. Reports whether the desktop agent is currently
connected (last poll within 15 s) plus how many commands are pending in
the queue. Used by the in-app debug panel to render a presence dot.

The actual long-poll RPC channel (``POST /api/desktop/poll``) stays at
its original path under :mod:`routes.desktop` because it's a Bridge-Secret-
authenticated long-poll between server and agent, not a JSON REST verb.
"""

from __future__ import annotations

import sys
import time

from flask import Blueprint, jsonify

from lib.log import audit_log, get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_desktop_bp = Blueprint('api_v1_desktop', __name__)


def _setup_state(connected: bool) -> str:
    """Which ONE install instruction the UI should show.

    The setup surface must present a single next action, never a menu of
    every possible path — so the CHOICE is made here, where the facts
    actually live, rather than guessed in JS.

    * ``connected``  — an agent polled within the window; nothing to install.
    * ``tray``       — this server process is the packaged desktop app
      (``sys.frozen``, set by PyInstaller and re-exec'd by
      desktop/launcher.py with TOFU_RUN_SERVER=1). The agent runs
      IN-PROCESS via the tray's "Enable Computer Control" item, so the
      instruction is one click and no token is involved. This supersedes
      the old ``python -m lib.desktop_agent`` flow (launcher.py docstring:
      "Replaces the old 'install a second program and run python -m
      lib.desktop_agent' flow").
    * ``remote``     — anything else: the user's machine is not this
      machine, so they need the desktop app plus a bridge token.

    ``sys.frozen`` is the load-bearing signal, NOT the peer address:
    :func:`routes.api_v1.auth._remote_is_loopback` documents that a
    same-host reverse proxy makes every public request present as
    loopback, so it can never distinguish "the user is on this box" from
    "the user is behind nginx". A frozen process, by contrast, IS the
    tray app by construction. Loopback is consulted only to keep a
    source-run local dev server (frozen=False, peer=loopback) out of the
    ``remote`` bucket — it would otherwise be told to install a second
    copy of an app it is already running.
    """
    if connected:
        return 'connected'
    if getattr(sys, 'frozen', False):
        return 'tray'
    from .auth import _remote_is_loopback
    if _remote_is_loopback():
        return 'local_source'
    return 'remote'


# ── Platform/release knowledge: extracted to lib/desktop_dist/platforms ──
# (2026-07, pt_a859c11e75d142d1). The route previously OWNED these helpers;
# the background mirror (lib/desktop_dist/mirror.py) would have been a
# second copy of the same rules. Re-exported here so existing callers and
# guard suites that import them from the route see no drift.
from lib.desktop_dist.platforms import (  # noqa: F401
    _PLATFORM_ASSETS_CACHE,
    _RELEASE_ASSET_CACHE,
    _assets_from_release_payload,
    _desktop_download_url,
    _detect_arch,
    _detect_os,
    _latest_release_assets,
    _match_platform_assets,
    _platform_assets,
    _update_repo,
)
from lib.desktop_dist import mirror as _dist_mirror
from lib.desktop_dist import store as _dist_store


def _request_platform_downloads(arch_override: str = '') -> list[dict]:
    """Per-platform direct links for the CURRENT request's visitor.

    ── Zero network in the request path ──
    This used to resolve against ``api.github.com`` SYNCHRONOUSLY (TTL-cached,
    up to a 6 s timeout) inside an async route: every cache expiry stalled the
    event loop, which is the measured reason the Local Control modal's desktop
    row "always takes much longer". The answer now comes from the LOCAL
    artifact store (lib/desktop_dist): the background mirror keeps the
    published installers on this server's disk, so the client's download
    itself no longer depends on its route to the public GitHub network either.

    When the store cannot serve this platform yet (first boot, refresh in
    flight), the row is omitted and the mirror is kicked — the releases-page
    escape hatch stays, and the modal's 3 s poll pops the direct link in once
    the file lands. URLs are ABSOLUTE, built from the request's own host — an
    address the user demonstrably reaches (see _agent_server_url).

    ``arch_override`` is the architecture the CLIENT resolved for itself via
    ``navigator.userAgentData.getHighEntropyValues(['architecture'])`` — the
    only practical source on macOS, where the UA always says Intel.
    """
    from urllib.parse import quote

    from flask import request
    try:
        ua = request.user_agent.string or ''
    except Exception:
        ua = ''
    hint = (arch_override or '').strip() \
        or request.headers.get('Sec-CH-UA-Arch', '')
    os_key = _detect_os(ua)
    if not os_key:
        return []
    rows = _dist_store.find_for_platform(os_key, _detect_arch(ua, hint))
    # Kick the mirror whether or not the store served: an empty store needs
    # filling, a stale one needs refreshing. Non-blocking and single-flight.
    _dist_mirror.ensure_fresh()
    # Opt-in autobuild: a Linux visitor with no locally-BUILT artifact can
    # kick a native build (this server's own platform is the only one it can
    # truly build). Off by default — a build is minutes of CPU, so it happens
    # only where the operator asked for it, never implicitly for everyone.
    if (os_key == 'linux'
            and not any(e.get('source') == 'built' for e in rows)):
        import os as _os
        if _os.environ.get('TOFU_DESKTOP_DIST_AUTOBUILD') == '1':
            from lib.desktop_dist import builder as _dist_builder
            if not _dist_builder.is_running():
                _dist_builder.start(reason='autobuild')
    base = (request.host_url or '').rstrip('/')
    out = []
    for e in rows:
        name = e.get('filename') or ''
        if not name:
            continue
        out.append({
            'os': e.get('os'),
            'arch': e.get('arch'),
            'label': e.get('label'),
            'filename': name,
            'url': base + '/api/v1/desktop/download/' + quote(name),
            'hosted': 'server',
            'size': e.get('size') or 0,
            'source': e.get('source') or 'mirrored',
        })
    return out


def _agent_server_url() -> str:
    """The base URL a remote agent should point itself at.

    Taken from the REQUEST the browser just made, because that is by
    construction an address the user can actually reach this server on —
    a configured BIND_HOST would frequently be ``0.0.0.0`` (meaningless to
    type) and an internal hostname may not resolve from the user's machine.
    """
    from flask import request
    return (request.host_url or '').rstrip('/')


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Desktop-agent connection status',
    description=(
        'Returns ``{connected, last_poll, pending_commands, setup_state, '
        'download_url, downloads, server_url}`` so the UI can render a '
        'presence indicator AND the single appropriate install instruction. '
        'Connection is defined as a poll within the last 15 s. '
        '``setup_state`` is one of ``connected`` / ``tray`` / '
        '``local_source`` / ``remote``; the two URL fields let the remote '
        'case render a real download link and a complete, copy-paste-ready '
        'connect line instead of a bare secret. ``downloads`` is the list of '
        'installers THIS visitor can run, each ``{os, arch, label, filename, '
        'url, hosted, size, source}`` served SAME-ORIGIN from this server '
        '(``/api/v1/desktop/download/<filename>``, ``hosted == "server"``) '
        'out of the local artifact store — the request path performs no '
        'network, and the client download no longer depends on the public '
        'GitHub network. '
        'It carries BOTH macOS DMGs when the architecture is unknown (an '
        'Apple Silicon Mac reports "Intel Mac OS X" in its UA, so guessing '
        'would hand half of Mac users a download that cannot open); pass '
        '``?arch=arm64|x86_64`` — which the client reads from '
        '``navigator.userAgentData.getHighEntropyValues`` — to narrow it to '
        'one. Empty when the platform is unrecognised; use ``download_url``.'
    ),
    tags=['capabilities'],
)
async def desktop_status():
    from flask import request
    from lib.desktop import (
        is_desktop_agent_connected,
        last_poll_time,
        list_agents,
        pending_commands_count,
    )
    from .auth import current_auth
    _auth = current_auth()
    _uid = (_auth.user_id
            if _auth and getattr(_auth, 'user_id', '') else None)
    connected = is_desktop_agent_connected()
    _last = last_poll_time()
    # The client resolves its own architecture via
    # navigator.userAgentData.getHighEntropyValues(['architecture']) and passes
    # it here; macOS cannot be narrowed any other way (an Apple Silicon Mac
    # reports "Intel Mac OS X" in its UA).
    _arch = (request.args.get('arch') or '').strip()[:16]
    return jsonify({
        'connected': connected,
        'last_poll': _last,
        'secondsAgo': (round(time.time() - _last, 1) if _last else None),
        'pending_commands': pending_commands_count(),
        'agents': list_agents(user_id=_uid),
        'setup_state': _setup_state(connected),
        'download_url': _desktop_download_url(),
        'downloads': _request_platform_downloads(_arch),
        'server_url': _agent_server_url(),
    })


@api_v1_desktop_bp.route('/api/v1/desktop/build', methods=['GET', 'POST'])
@require_auth
@api_meta(
    summary='Inspect (GET) or kick (POST) a native on-server desktop build',
    description=(
        'POST starts a single-flight background build of the desktop app '
        'from the COMMITTED tree (git archive HEAD → PyInstaller → boot '
        'smoke → tar), recorded in the artifact store with '
        '``source == "built"``. Only the server\'s own platform can be built '
        'natively (PyInstaller cannot cross-compile); Windows/macOS are '
        'served by the mirror. GET returns the persisted build state.'
    ),
    tags=['capabilities'],
)
async def desktop_build():
    from flask import request
    from lib.desktop_dist import builder as _dist_builder
    if request.method == 'POST':
        st = _dist_builder.start(reason='api')
        audit_log('desktop_build_kicked', state=st.get('state'),
                  version=st.get('version'))
        return jsonify(st), 202
    return jsonify(_dist_builder.state())


@api_v1_desktop_bp.route('/api/v1/desktop/download/<path:filename>',
                         methods=['GET'])
@require_auth
@api_meta(
    summary='Download a server-hosted desktop installer',
    description=(
        'Serves an installer from the local artifact store '
        '(lib/desktop_dist) as an attachment, with Range support '
        '(``conditional=True``) so a 100+ MB download is resumable. '
        '``filename`` must exactly match a manifest entry — no path material '
        'is accepted, so traversal is structurally impossible.'
    ),
    tags=['capabilities'],
)
def desktop_download(filename):
    """SYNC on purpose: pure file serving via the sync-safe send_file shim
    (same carve-out as serve_motion_file) — a 135 MB stream must not sit on
    the event loop."""
    from quart import send_file
    path = _dist_store.resolve_file(filename)
    if path is None:
        return jsonify({'error': 'not_found',
                        'message': 'no such artifact'}), 404
    return send_file(path, as_attachment=True,
                     attachment_filename=filename, conditional=True)


@api_v1_desktop_bp.route('/api/v1/desktop/streams/<cmd_id>', methods=['GET'])
@require_auth
async def desktop_stream(cmd_id):
    """Reassembled live output of one streamed command (RWA P2/P4b-2b).

    Debug / inspector surface — the chat UI consumes the same frames via
    the ``tool_progress`` SSE channel instead. ``cmd_id`` is an unguessable
    uuid minted per command; entries expire with the command TTL.
    """
    from lib.desktop import get_command_stream
    stream = get_command_stream(cmd_id)
    if stream is None:
        return jsonify({'error': 'not_found',
                        'message': 'unknown or expired command stream'}), 404
    return jsonify(stream)


# ── RWA P4b:Devices 页(拍板 5A)—— agents + bridge tokens 一屏 ──

_BRIDGE_SCOPE = 'agents:bridge'


@api_v1_desktop_bp.route('/api/v1/desktop/devices', methods=['GET'])
@require_auth
async def desktop_devices():
    """Devices page payload: the caller's agents + their bridge tokens.

    Tokens are listed METADATA-ONLY (id/name/created/scopes) — the secret
    is only ever returned once, by POST /api/v1/desktop/token.
    """
    from lib.api_keys import list_keys
    from lib.desktop import list_agents
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    tokens = [
        {'id': k.get('id'), 'name': k.get('name'),
         'created_at': k.get('created_at'),
         'scopes': sorted(k.get('scopes') or [])}
        for k in list_keys()
        if _BRIDGE_SCOPE in (k.get('scopes') or [])
        and (k.get('user_id') or '') == uid
    ]
    return jsonify({
        'agents': list_agents(user_id=uid),
        'tokens': tokens,
    })


@api_v1_desktop_bp.route('/api/v1/desktop/token', methods=['POST'])
@require_auth
async def desktop_token_mint():
    """Mint a per-user bridge token (scope agents:bridge).

    The raw secret is returned EXACTLY ONCE in this response; afterwards
    only metadata is listable. Bound to the caller's user_id so poll auth
    scopes every command to them (RWA P4a).
    """
    from lib.api_keys import create_key
    from lib.request_parser import async_parse_body, optional_str
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    body = await async_parse_body()
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'desktop-bridge'
    row, token = create_key(name, scopes=[_BRIDGE_SCOPE], user_id=uid)
    audit_log('desktop_bridge_token_minted', key_id=row.get('id'),
              name=name, user_id=uid)
    return jsonify({'id': row.get('id'), 'name': name, 'token': token,
                    'scopes': [_BRIDGE_SCOPE]}), 201


@api_v1_desktop_bp.route('/api/v1/desktop/token/<key_id>', methods=['DELETE'])
@require_auth
async def desktop_token_revoke(key_id):
    """Revoke one of the caller's OWN bridge tokens.

    Deliberately NOT the admin-scoped /api/v1/keys DELETE: a tenant may
    revoke only their own agents:bridge keys, nothing wider.
    """
    from lib.api_keys import get_key_by_id, revoke_key
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    row = get_key_by_id(key_id)
    if (not row or _BRIDGE_SCOPE not in (row.get('scopes') or [])
            or (row.get('user_id') or '') != uid):
        return jsonify({'error': 'not_found',
                        'message': 'bridge token not found'}), 404
    revoke_key(key_id)
    audit_log('desktop_bridge_token_revoked', key_id=key_id, user_id=uid)
    return jsonify({'revoked': key_id})


__all__ = ['api_v1_desktop_bp']

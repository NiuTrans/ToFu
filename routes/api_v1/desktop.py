"""routes/api_v1/desktop.py — Desktop-agent status probe + pairing surface.

The status/build/download/streams/devices routes are REST verbs the Local
Control panel consumes. The pairing routes (pair-code mint + pair
exchange) implement the one-time pairing-code UX
(docs/DESKTOP_AGENT_DIST_DESIGN.md §11): the panel mints a 6-digit code
(authenticated), the agent exchanges it for an agents:bridge token (the
code IS the credential — no bearer).

The actual long-poll RPC channel (``POST /api/desktop/poll``) stays at
its original path under :mod:`routes.desktop` because it's a Bridge-Secret-
authenticated long-poll between server and agent, not a JSON REST verb.
"""

from __future__ import annotations

import sys
import time

from flask import Blueprint

from lib.api_response import (
    api_conflict, api_created, api_error, api_not_found, api_ok, api_payload,
)
from lib.env_compat import getenv_compat
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
      instruction is one click and no token is involved.
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

    ── The tunnel blind spot (measured 2026-08-02, owner live) ──
    An ssh -L port forward makes a REMOTE machine's browser present as
    loopback too, and the server has NO signal to tell it apart from a
    true local dev server — so ``local_source`` is structurally wrong
    for tunnel users: its primary instruction ("install the full desktop
    app") installed a second Tofu on the office machine whose bundled
    server grabbed a fallback port and whose agent polled IT, never this
    one. The honest fix is NOT re-classification (impossible without a
    distinguishing signal — a guess would misroute true-local users) but
    the surface escape hatch: the local_source branch renders a
    collapsed "从另一台电脑访问本服务器？" details section with the
    agent download + mint flow (local-control.js). Anyone tempted to
    "detect the tunnel" here: there is nothing to detect.
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


def _entry_preseed_url(entry: dict) -> str:
    """The preseed URL worth advertising to the panel ('' when unusable).

    A loopback/unspecified preseed works only when the installer lands
    on the SERVER's own machine; offered to a remote controlled machine
    it attaches the agent to a void AND suppresses the first-run connect
    dialog (the measured first agent artifact baked
    ``http://127.0.0.1:15000``). The panel only ever sees a preseed that
    can promise a real auto-connect — anything else falls through to the
    minted-connect-line flow.
    """
    url = str(((entry or {}).get('preseed') or {}).get('url') or '')
    url = url.strip()
    if not url or _dist_store.is_loopback_url(url):
        return ''
    return url


def _request_platform_downloads(arch_override: str = '',
                                kind: str = 'full') -> list[dict]:
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
    address the user demonstrably reaches (see _agent_server_url). Under a
    path-prefixed cloud-IDE proxy (…/proxy/<port>/) the host alone is NOT
    enough — the proxy strips the prefix before forwarding, so the backend
    structurally cannot see it, and the click dies on the gateway's default
    route without ever reaching Tofu. The client therefore re-bases the
    canonical ``/api/...`` tail onto its live ``BASE_PATH`` before rendering
    (local-control.js ``_lcResolveDlUrl`` — the same seam as pdf_viewer.js
    ``_resolvePaperPdfUrl``).

    ``arch_override`` is the architecture the CLIENT resolved for itself via
    ``navigator.userAgentData.getHighEntropyValues(['architecture'])`` — the
    only practical source on macOS, where the UA always says Intel.
    """
    from urllib.parse import quote

    from flask import request
    try:
        ua = request.user_agent.string or ''
    except Exception as e:
        logger.debug('[Desktop] user-agent parse failed: %s', e)
        ua = ''
    hint = (arch_override or '').strip() \
        or request.headers.get('Sec-CH-UA-Arch', '')
    os_key = _detect_os(ua)
    if not os_key:
        return []
    rows = _dist_store.find_for_platform(os_key, _detect_arch(ua, hint),
                                         kind=kind)
    # Kick the mirror whether or not the store served: an empty store needs
    # filling, a stale one needs refreshing. Non-blocking and single-flight.
    _dist_mirror.ensure_fresh()
    import os as _os
    # Opt-in autobuild: a Linux visitor with no locally-BUILT artifact can
    # kick a native build (this server's own platform is the only one it can
    # truly build). Off by default — a build is minutes of CPU, so it happens
    # only where the operator asked for it, never implicitly for everyone.
    if (kind == 'full' and os_key == 'linux'
            and not any(e.get('source') == 'built' for e in rows)):
        if _os.environ.get('TOFU_DESKTOP_DIST_AUTOBUILD') == '1':
            from lib.desktop_dist import builder as _dist_builder
            if not _dist_builder.is_running():
                _dist_builder.start(reason='autobuild')
    # Same opt-in for Windows: no built installer → kick the Wine-toolchain
    # build (payload cached per (git_sha, deps), then the NSIS wrapper).
    # macOS never gets one — the documented permanent boundary. The agent
    # kind kicks the agent target of the same builder: a visitor hitting
    # the AGENT surface (kind='agent') with no built agent artifact gets
    # one built (stale-while-build ⇒ the full installer stays the offer).
    if (os_key == 'windows'
            and not any(e.get('source') == 'built' for e in rows)):
        if _os.environ.get('TOFU_DESKTOP_DIST_AUTOBUILD') == '1':
            from lib.desktop_dist import winbuilder as _win_builder
            if not _win_builder.is_running():
                _win_builder.start_installer(reason='autobuild',
                                             target=kind)
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
            'kind': e.get('kind') or 'full',
            'preseed_url': _entry_preseed_url(e),
        })
    return out


def _with_drift(agents):
    """Flag agents whose build differs from this server's (owner amendment ②).

    The command protocol evolves WITH the server — a release-line agent
    against a HEAD server can silently mis-dispatch. ``outdated`` is True
    only when BOTH versions are known and differ: a legacy agent without
    the frame field is 'unknown', not 'outdated' (never cry wolf on the
    devices page).
    """
    try:
        from lib.version import __version__ as sv
        sv = (sv or '').strip()
    except Exception as e:
        logger.debug('[Desktop] server version read failed: %s', e)
        sv = ''
    out = []
    for a in agents or []:
        a = dict(a)
        av = str(a.get('version') or '').strip()
        a['outdated'] = bool(sv and av and av != sv)
        out.append(a)
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


def _host_reachability(host: str) -> str:
    """Whether an AGENT can use the address this request arrived on.

    The connect line is minted from ``request.host_url`` — an address the
    BROWSER demonstrably reaches. Under an SSO-fronted gateway (cloud-IDE
    preview proxies, corporate IdP) the browser sails through on cookies
    while the agent — which carries only a bridge token — is bounced at
    the edge and never reaches Tofu. Measured 2026-08-03 (owner live):
    the codelab preview proxy answered every /api/* with
    ``401 {"error":"Unauthorized"}`` while access.log showed ZERO agent
    polls — the owner had pasted a proxy-URL connect line and the agent
    polled a wall, silently. The panel warns when the address it is about
    to hand out is of that kind. 'public' is a heuristic (a public host
    CAN be fine when nothing intercepts it), so the panel warns without
    blocking the mint.
    """
    import ipaddress
    h = (host or '').split(':')[0].strip().strip('[]').lower()
    if h in ('', 'localhost', 'localhost.localdomain'):
        return 'loopback'
    try:
        ip = ipaddress.ip_address(h)
    except ValueError as e:
        logger.debug('[Desktop] host %r is not an IP literal — treating '
                     'as public: %s', h, e)
        return 'public'
    if ip.is_loopback:
        return 'loopback'
    if ip.is_private:
        return 'private'
    return 'public'


def _caller_bridge_token_count(uid: str) -> int:
    """How many agents:bridge tokens the caller has minted (metadata only).

    Feeds the panel's waiting-diagnosis: tokens issued but zero agents
    arrived ⇒ the line was minted, so the failure is downstream of the
    copy — almost always the address half (a proxy URL the agent cannot
    use), which is exactly what server_url_reachability flags.
    """
    try:
        from lib.api_keys import list_keys
        return sum(1 for k in list_keys()
                   if _BRIDGE_SCOPE in (k.get('scopes') or [])
                   and (k.get('user_id') or '') == (uid or ''))
    except Exception as e:
        logger.debug('bridge token count unavailable: %s', e)
        return 0


_BRIDGE_SCOPE = 'agents:bridge'


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Desktop-agent connection status',
    description=(
        'Returns ``{connected, last_poll, pending_commands, setup_state, '
        'download_url, downloads, agent_downloads, server_url, '
        'server_url_reachability, bridge_tokens_issued, '
        'bridge_token_required, agents}`` so the UI can render a presence '
        'indicator AND the single appropriate install instruction. '
        'Connection is defined as a poll within the last 15 s. '
        '``setup_state`` is one of ``connected`` / ``tray`` / '
        '``local_source`` / ``remote``.'
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
    _arch = (request.args.get('arch') or '').strip()[:16]
    return api_ok({
        'connected': connected,
        'last_poll': _last,
        'secondsAgo': (round(time.time() - _last, 1) if _last else None),
        'pending_commands': pending_commands_count(),
        'agents': _with_drift(list_agents(user_id=_uid)),
        'setup_state': _setup_state(connected),
        'download_url': _desktop_download_url(),
        'downloads': _request_platform_downloads(_arch),
        'agent_downloads': _request_platform_downloads(_arch,
                                                       kind='agent'),
        'server_url': _agent_server_url(),
        'server_url_reachability': _host_reachability(request.host),
        'bridge_tokens_issued': _caller_bridge_token_count(_uid),
        'bridge_token_required': bool(
            (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip()),
    })


@api_v1_desktop_bp.route('/api/v1/desktop/build', methods=['GET', 'POST'])
@require_auth
@api_meta(
    summary='Inspect (GET) or kick (POST) an on-server desktop build',
    description=(
        'POST starts a single-flight background build of the desktop app '
        'from the COMMITTED tree (git archive HEAD → PyInstaller → boot '
        'smoke), recorded in the artifact store with ``source == "built"``. '
        'The default (or ``{"os": "linux"}``) builds the server\'s own '
        'platform natively. ``{"os": "windows"}`` drives the userspace Wine '
        'toolchain (lib/desktop_dist/winbuilder.py — payload cached per '
        '(git_sha, deps), then the NSIS wrapper; optional '
        '``{"server_url": ...}`` pre-seeds the remote attachment into the '
        'installer). macOS cannot be built on Linux (documented permanent '
        'boundary — the mirror serves it). GET returns both builders\' '
        'persisted states.'
    ),
    tags=['capabilities'],
)
async def desktop_build():
    from flask import request
    from lib.desktop_dist import builder as _dist_builder
    if request.method == 'POST':
        body = {}
        try:
            body = await request.get_json(silent=True) or {}
        except Exception as e:
            logger.debug('desktop_build: non-JSON body ignored: %s', e)
        os_key = str(body.get('os') or 'linux').strip().lower()
        if os_key == 'windows':
            from lib.desktop_dist import winbuilder as _win_builder
            url = str(body.get('server_url') or '').strip()
            kind = str(body.get('kind') or 'full').strip().lower()
            if kind not in ('full', 'agent'):
                kind = 'full'
            st = _win_builder.start_installer(reason='api', server_url=url,
                                              target=kind)
            audit_log('desktop_build_kicked', os='windows', kind=kind,
                      state=st.get('state'), version=st.get('version'))
            return api_payload(st, 202)
        st = _dist_builder.start(reason='api')
        audit_log('desktop_build_kicked', state=st.get('state'),
                  version=st.get('version'))
        return api_payload(st, 202)
    from lib.desktop_dist import winbuilder as _win_builder
    return api_ok({'linux': _dist_builder.state(),
                   'windows': _win_builder.state()})


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
    path = _dist_store.resolve_file(filename)
    if path is None:
        return api_not_found('not_found',
                             message='no such artifact')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, as_attachment=True,
                                 attachment_filename=filename)


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
        return api_not_found(
            'not_found', message='unknown or expired command stream')
    return api_ok(stream)


@api_v1_desktop_bp.route('/api/v1/desktop/devices', methods=['GET'])
@require_auth
@api_meta(
    summary='Devices page: the caller\'s agents + their bridge tokens',
    description=(
        'Tokens are listed METADATA-ONLY (id/name/created/scopes) — the '
        'secret is only ever returned once, by POST /api/v1/desktop/token.'
    ),
    tags=['capabilities'],
)
async def desktop_devices():
    """Devices page payload: the caller's agents + their bridge tokens."""
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
    return api_ok({
        'agents': _with_drift(list_agents(user_id=uid)),
        'tokens': tokens,
    })


@api_v1_desktop_bp.route('/api/v1/desktop/pair-code', methods=['POST'])
@require_auth
@api_meta(
    summary='Mint a one-time pairing code',
    description=(
        'Mints a 6-digit one-time code (valid 5 minutes, one-shot, '
        '3-attempt lockout) for pairing a controlled machine. Bound to '
        'the calling user; the agent consumes it through POST '
        '/api/desktop/pair to receive an agents:bridge token.'
    ),
    tags=['capabilities'],
)
async def desktop_pair_code_mint():
    """Mint a one-time pairing code.

    The code is the ONLY credential the agent needs to attach itself
    to this user's account — no bearer, no bridge secret. The panel
    renders the code big with a copy button and a 5-minute countdown.
    """
    from lib.desktop.pairing import (_CODE_TTL_S, mint_code,
                                     pending_codes)
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    if not uid:
        return api_not_found('not_found',
                             message='authenticated user required')
    code, expires_at = mint_code(uid)
    audit_log('desktop_pair_code_minted', user_id=uid)
    return api_created({
        'code': code,
        'expires_at': expires_at,
        'ttl': _CODE_TTL_S,
        'pending': pending_codes(uid),
    })


@api_v1_desktop_bp.route('/api/desktop/pair', methods=['POST'])
@api_meta(
    summary='Exchange a pairing code for a bridge token',
    description=(
        'The AGENT calls this (NO bearer — the code IS the credential) '
        'to consume a one-time code and receive an agents:bridge token. '
        'One-shot: a code that is missing, expired, over-attempted, or '
        'already used fails. Does NOT require authentication: this is '
        'the onboarding of a fresh agent that has no token yet.'
    ),
    tags=['capabilities'],
)
async def desktop_pair():
    """Exchange a pairing code for an agents:bridge token.

    Not authenticated by design: the agent pairing itself in has no
    token. The 6-digit one-time code is the sole credential; it is
    consumed exactly once. On success the agent gets a bridge token
    bound to the code's minting user, saves it as its remote
    attachment, and starts polling.
    """
    from flask import request
    from lib.api_keys import create_key
    from lib.desktop.pairing import (consume_code,
                                     ip_fail_budget_exceeded,
                                     record_pair_failure,
                                     record_pair_success)
    from lib.request_parser import async_parse_body, optional_str
    # Per-IP global failure budget (owner 2026-08-04): an attacker who
    # keeps guessing NEW codes gets a fresh per-code budget each time, so
    # per-code lockout alone leaves 1e6 space brute-forceable. A blocked
    # IP gets 429 BEFORE its code is even looked up — the rate bound is
    # the real boundary, not the per-code retry count.
    client_ip = request.remote_addr or '<unknown>'
    if ip_fail_budget_exceeded(client_ip):
        audit_log('desktop_pair_rate_limited', ip=client_ip)
        return api_error('pair_rate_limited', status=429,
                         message='Too many failed pairing attempts from '
                                 'this address. Wait a few minutes and '
                                 'try again with a fresh code.')
    body = await async_parse_body()
    code = optional_str(body, 'code', default='',
                        max_len=16).strip()
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'paired-agent'
    platform = optional_str(body, 'platform', default='',
                            max_len=40).strip() or 'unknown'
    user_id = consume_code(code)
    if user_id is None:
        record_pair_failure(client_ip)
        audit_log('desktop_pair_failed', code=code[:2] + '****',
                  reason='invalid_code')
        return api_conflict('invalid_code',
                            message='This pairing code is invalid, expired, '
                                    'or already used. Generate a new one '
                                    'in the panel.')
    record_pair_success(client_ip)
    row, token = create_key(name, scopes=[_BRIDGE_SCOPE], user_id=user_id)
    audit_log('desktop_pair_succeeded', key_id=row.get('id'),
              user_id=user_id, platform=platform)
    return api_created({
        'id': row.get('id'),
        'name': name,
        'token': token,
        'scopes': [_BRIDGE_SCOPE],
        'user_id': user_id,
    })


@api_v1_desktop_bp.route('/api/v1/desktop/token', methods=['POST'])
@require_auth
@api_meta(
    summary='Mint a per-user bridge token (scope agents:bridge)',
    description=(
        'The raw secret is returned EXACTLY ONCE in this response; '
        'afterwards only metadata is listable. Bound to the caller\'s '
        'user_id so poll auth scopes every command to them (RWA P4a).'
    ),
    tags=['capabilities'],
)
async def desktop_token_mint():
    """Mint a per-user bridge token (scope agents:bridge)."""
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
    return api_created({'id': row.get('id'), 'name': name,
                        'token': token, 'scopes': [_BRIDGE_SCOPE]})


@api_v1_desktop_bp.route('/api/v1/desktop/token/<key_id>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Revoke one of the caller\'s OWN bridge tokens',
    description=(
        'Deliberately NOT the admin-scoped /api/v1/keys DELETE: a tenant '
        'may revoke only their own agents:bridge keys, nothing wider.'
    ),
    tags=['capabilities'],
)
async def desktop_token_revoke(key_id):
    """Revoke one of the caller's OWN bridge tokens."""
    from lib.api_keys import get_key_by_id, revoke_key
    from .auth import current_auth
    auth = current_auth()
    uid = (auth.user_id if auth and getattr(auth, 'user_id', '') else '')
    row = get_key_by_id(key_id)
    if (not row or _BRIDGE_SCOPE not in (row.get('scopes') or [])
            or (row.get('user_id') or '') != uid):
        return api_not_found('not_found',
                             message='bridge token not found')
    revoke_key(key_id)
    audit_log('desktop_bridge_token_revoked', key_id=key_id, user_id=uid)
    return api_ok({'revoked': key_id})


__all__ = ['api_v1_desktop_bp']

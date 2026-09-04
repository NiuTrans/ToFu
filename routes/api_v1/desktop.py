"""routes/api_v1/desktop.py — desktop-agent status and distribution surface.

The status/build/download/streams/devices routes are REST verbs the Local
Control panel consumes. Current controlled-end setup is one personalized
installer: route candidates and its agents:bridge credential stay inside the
EXE. Pair-code/token routes remain wire compatibility for already-shipped
clients only; current UI never asks a user to mint, copy, or paste auth data.

The actual long-poll RPC channel (``POST /api/desktop/poll``) stays at
its original path under :mod:`routes.desktop` because it's a Bridge-Secret-
authenticated long-poll between server and agent, not a JSON REST verb.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from urllib.parse import urlsplit

from quart import Blueprint

from lib.api_response import (
    api_conflict, api_created, api_error, api_not_found, api_ok, api_payload,
)
from lib.log import audit_log, get_logger
from lib.log_policy import stream_backup_count, stream_max_bytes
from lib.log_redaction import redact_text, sanitize_value
from lib.log_retention import append_bytes_locked, copytruncate_if_oversize
from lib.openapi import api_meta
from lib.request_parser import BadRequest, async_parse_body, optional_str

from .auth import require_auth

logger = get_logger(__name__)

api_v1_desktop_bp = Blueprint('api_v1_desktop', __name__)


_DESKTOP_BUILD_OS_CHOICES = ('linux', 'windows')
_DESKTOP_BUILD_KIND_CHOICES = ('full', 'agent')
_MAX_BUILD_SERVER_URL_CHARS = 4096


def _desktop_build_parameters(body: dict) -> tuple[str, str, str]:
    """Validate one expensive build request before it starts background work.

    Defaults are useful for an empty POST, but applying them to misspelled or
    malformed fields is unsafe: ``windwos`` previously launched a Linux build
    and an unknown ``kind`` launched the much larger full application.  The
    preseed URL is persisted in artifact metadata, so credential carriers are
    rejected here rather than relying on the installed client to discard them.
    """
    os_key = optional_str(
        body, 'os', default='linux', max_len=16).lower() or 'linux'
    if os_key not in _DESKTOP_BUILD_OS_CHOICES:
        raise BadRequest(
            'os must be one of: ' + ', '.join(_DESKTOP_BUILD_OS_CHOICES),
            field='os',
        )
    if os_key == 'linux':
        return os_key, 'full', ''

    kind = optional_str(
        body, 'kind', default='full', max_len=16).lower() or 'full'
    if kind not in _DESKTOP_BUILD_KIND_CHOICES:
        raise BadRequest(
            'kind must be one of: ' + ', '.join(_DESKTOP_BUILD_KIND_CHOICES),
            field='kind',
        )

    server_url = optional_str(
        body,
        'server_url',
        default='',
        max_len=_MAX_BUILD_SERVER_URL_CHARS,
    )
    if server_url:
        try:
            parsed = urlsplit(server_url)
            has_hostname = bool(parsed.hostname)
        except ValueError as error:
            raise BadRequest(
                'server_url must be an absolute HTTP(S) URL',
                field='server_url',
            ) from error
        if parsed.scheme.lower() not in ('http', 'https') or not has_hostname:
            raise BadRequest(
                'server_url must be an absolute HTTP(S) URL',
                field='server_url',
            )
        if (parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise BadRequest(
                'server_url must not contain credentials, query, or fragment',
                field='server_url',
            )
    return os_key, kind, server_url


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
      machine, so it receives a personalized controlled-end installer.

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
    the surface escape hatch: the local_source branch renders the controlled-
    end role with its personalized installer (local-control.js). Anyone
    tempted to "detect the tunnel" here: there is nothing to detect.
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
# (2026-07, ). The route previously OWNED these helpers;
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

    from quart import request
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
    from quart import request
    return (request.host_url or '').rstrip('/')


def _host_reachability(host: str) -> str:
    """Whether an AGENT can use the address this request arrived on.

    The installer attachment includes an address derived from the request —
    one the BROWSER demonstrably reaches. Under an SSO-fronted gateway
    (cloud-IDE preview proxies, corporate IdP) the browser sails through on cookies
    while the agent — which carries only a bridge token — is bounced at
    the edge and never reaches Tofu. Measured 2026-08-03 (owner live):
    the codelab preview proxy answered every /api/* with
    ``401 {"error":"Unauthorized"}`` while access.log showed ZERO agent
    polls — a proxy-derived URL made the agent poll a wall silently. The panel
    warns when the address it is about to embed is of that kind. 'public' is
    a heuristic (a public host CAN be fine when nothing intercepts it), so the
    panel warns without blocking the download.
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


def _caller_bridge_token_count(
    owner_user_id: int, tenant_id: str | None,
) -> int:
    """How many agents:bridge tokens the caller has minted (metadata only).

    Feeds the panel's waiting-diagnosis: tokens issued but zero agents
    arrived ⇒ the line was minted, so the failure is downstream of the
    copy — almost always the address half (a proxy URL the agent cannot
    use), which is exactly what server_url_reachability flags.
    """
    try:
        from lib.api_keys import list_keys
        return sum(
            1 for key in list_keys(
                owner_user_id=owner_user_id, tenant_id=tenant_id)
            if _BRIDGE_SCOPE in (key.get('scopes') or [])
        )
    except Exception as e:
        logger.debug('bridge token count unavailable: %s', e)
        return 0


_BRIDGE_SCOPE = 'agents:bridge'


# ── One-file zero-config agent installer ─────────────────────────────
# The pairing-code and ZIP UX are RETIRED. The panel now downloads one
# directly runnable personalized .exe. Its fixed NSIS trailer carries
# {route candidates, a fresh agents:bridge token}; the installer writes
# that internal record into the install dir and first launch imports it.
# The measured failure this kills:
# an agent handed a browser-reachable proxy URL (missing scheme /
# /proxy/<port> prefix, then SSO-401 for a cookieless client) polls a
# wall forever — access.log showed ZERO agent requests while the panel
# waited for a pairing code that could never be redeemed.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _server_bind_class() -> str:
    """'all' | 'loopback' | 'specific' — how off-machine-reachable we are.

    Reads the bind the RUNNING server actually took (``_TOFU_RUNTIME_HOST``,
    recorded by server.py at boot). A loopback bind means NO desktop agent
    on another machine can ever reach this server directly — the panel
    surfaces that as an operator warning instead of letting the attach
    flow fail silently (owner incident 2026-08-05: a platform-injected
    BIND_HOST=127.0.0.1 quietly overrode the 0.0.0.0 default).
    """
    host = (os.environ.get('_TOFU_RUNTIME_HOST')
            or os.environ.get('BIND_HOST') or '0.0.0.0').strip().lower()
    if host in _LOOPBACK_HOSTS:
        return 'loopback'
    if host in ('', '0.0.0.0', '::'):
        return 'all'
    return 'specific'


def _direct_lan_candidate() -> str:
    """``http://<lan-ip>:<port>`` when the bind makes it reachable, else ''.

    Same honesty guard as the LAN discovery responder (lib/desktop/
    pairing.py): advertising an address the server cannot be reached at
    sends every discovering agent to a dead route.
    """
    if _server_bind_class() == 'loopback':
        return ''
    from lib.desktop.pairing import lan_ip
    ip = lan_ip()
    if not ip:
        return ''
    try:
        port = int(os.environ.get('_TOFU_RUNTIME_PORT') or '15000')
    except (TypeError, ValueError) as e:
        logger.debug('[Desktop] bad _TOFU_RUNTIME_PORT: %s', e)
        port = 15000
    return 'http://%s:%d' % (ip, port)


def _agent_installer_ready(entry: dict | None) -> bool:
    """Whether the artifact itself proves it can import an embedded attach.

    The old git-ancestry proxy only proved that agent-side JSON import code
    existed; it could not prove the *installer* knew how to extract internal
    data. The fixed trailer is direct artifact evidence and also works for
    mirrored release installers whose build commit is unavailable locally.
    """
    if not entry or not entry.get('filename'):
        return False
    path = _dist_store.resolve_file(str(entry['filename']))
    if path is None:
        return False
    from lib.desktop_dist.agent_installer import has_attachment_slot
    return has_attachment_slot(path)


def _agent_store_entry() -> dict | None:
    """The newest servable WINDOWS agent artifact (the only agent target)."""
    rows = _dist_store.find_for_platform('windows', 'x86_64', kind='agent')
    return rows[0] if rows else None


def _visitor_os() -> str:
    """The request visitor's OS key: 'windows' | 'macos' | 'linux' | ''.

    The panel picks its primary controlled-end offer by this value: the
    personalized one-file installer is a WINDOWS .exe by design (the NSIS
    trailer rewrite in lib/desktop_dist/agent_installer.py), so a
    macOS/Linux visitor must be steered to the mirrored per-platform agent
    asset in ``agent_downloads`` — never handed a package it cannot run.
    The rule lives server-side (one ``_detect_os`` owns it); the frontend
    renders, it does not re-derive.
    """
    from quart import request
    try:
        ua = request.user_agent.string or ''
    except Exception as e:
        logger.debug('[Desktop] user-agent parse failed: %s', e)
        ua = ''
    return _detect_os(ua)


@api_v1_desktop_bp.route('/api/v1/desktop/status', methods=['GET'])
@require_auth
@api_meta(
    summary='Desktop-agent connection status',
    description=(
        'Returns ``{connected, last_poll, pending_commands, setup_state, '
        'visitor_os, download_url, downloads, agent_downloads, server_url, '
        'server_url_reachability, bridge_tokens_issued, '
        'agents}`` so the UI can render a presence '
        'indicator AND the single appropriate install instruction. '
        'Connection is defined as a poll within the last 15 s. '
        '``setup_state`` is one of ``connected`` / ``tray`` / '
        '``local_source`` / ``remote``.'
    ),
    tags=['capabilities'],
)
async def desktop_status():
    from quart import request
    from lib.desktop import (
        is_desktop_agent_connected,
        last_poll_time,
        list_agents,
        pending_commands_count,
    )
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop status')
    connected = is_desktop_agent_connected()
    _last = last_poll_time()
    _arch = (request.args.get('arch') or '').strip()[:16]
    return api_ok({
        'connected': connected,
        'last_poll': _last,
        'visitor_os': _visitor_os(),
        'secondsAgo': (round(time.time() - _last, 1) if _last else None),
        'pending_commands': pending_commands_count(),
        'agents': _with_drift(list_agents(user_id=str(owner_user_id))),
        'setup_state': _setup_state(connected),
        'download_url': _desktop_download_url(),
        'downloads': _request_platform_downloads(_arch),
        'agent_downloads': _request_platform_downloads(_arch,
                                                       kind='agent'),
        'server_url': _agent_server_url(),
        'server_url_reachability': _host_reachability(request.host),
        'bridge_tokens_issued': _caller_bridge_token_count(
            owner_user_id, principal.tenant_id),
        # One-file installer surface: the bind class lets the
        # panel WARN when a loopback bind makes remote agents unreachable
        # by construction; readiness flips the agent download from a stale
        # bare exe to the personalized, directly runnable installer.
        'server_bind': _server_bind_class(),
        'agent_installer_ready': _agent_installer_ready(_agent_store_entry()),
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
        'installer). Build selectors are validated rather than defaulted on '
        'typos; preseed URLs must be credential-free absolute HTTP(S) URLs. '
        'macOS cannot be built on Linux (documented permanent boundary — the '
        'mirror serves it). GET returns both builders\' persisted states.'
    ),
    tags=['capabilities'],
)
async def desktop_build():
    from quart import request
    from lib.desktop_dist import builder as _dist_builder
    if request.method == 'POST':
        body = await async_parse_body(strict=True)
        os_key, kind, url = _desktop_build_parameters(body)
        if os_key == 'windows':
            from lib.desktop_dist import winbuilder as _win_builder
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
    """SYNC on purpose: file serving crosses the explicit Quart boundary in
    the executor (same carve-out as serve_motion_file), so a 135 MB stream
    never sits on the event loop."""
    path = _dist_store.resolve_file(filename)
    if path is None:
        return api_not_found('not_found',
                             message='no such artifact')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, as_attachment=True,
                                 attachment_filename=filename)


# ``agent-bundle`` remains a wire-compatible alias for already-cached pages,
# but it returns the SAME .exe response. No route distributes a ZIP anymore.
def _build_attach_bundle(
    *,
    owner_user_id: int,
    account_user_id: str,
    tenant_id: str | None,
):
    """The ONE attach-payload construction — Windows trailer and page push.

    Both zero-config channels deliver the same record: an ordered
    route-candidate list plus a fresh per-user ``agents:bridge`` token
    minted at download time. The Windows channel rewrites the .exe's NSIS
    trailer with it; the macOS/Linux channel hands it to the signed-in
    Local Control page, which pushes it to the unattached agent's loopback
    broker (``POST /api/v1/desktop/agent-attach-bundle`` below).

    Returns ``(payload, '')`` or ``(None, 'credential_unavailable')``. A
    device without a credential can never poll, so a package guaranteed to
    fail must not leave this process.
    """
    token = ''
    try:
        from lib.api_keys import create_key
        row, token = create_key(
            'agent-attach-%s' % time.strftime('%Y%m%d-%H%M%S'),
            scopes=[_BRIDGE_SCOPE], owner_user_id=owner_user_id,
            account_user_id=account_user_id, tenant_id=tenant_id)
        audit_log('desktop_agent_bundle_minted', key_id=row.get('id'),
                  owner_user_id=owner_user_id)
    except Exception as e:
        logger.warning('[Desktop] attach-bundle credential mint failed: %s', e)
        return None, 'credential_unavailable'

    candidates = []
    direct = _direct_lan_candidate()
    if direct:
        candidates.append(direct)
    fallbacks = []
    try:
        from routes.browser import _external_base_url
        live = (_external_base_url() or '').rstrip('/')
        if live and live != direct:
            fallbacks.append(live)
    except Exception as e:
        logger.warning('[Desktop] live-base resolution failed: %s', e)

    return {
        'v': 1,
        'kind': 'tofu-agent-attach',
        'minted_at': time.time(),
        'token': token,
        # Probe order the agent walks: direct LAN first (no SSO between),
        # then its own ladder (loopback → LAN broadcast → ssh self-tunnel),
        # the browser-reachable base LAST (SSO-edge risk, measured
        # 2026-08-03).
        'candidates': candidates,
        'fallback_candidates': fallbacks,
    }, ''


@api_v1_desktop_bp.route('/api/v1/desktop/agent-attach-bundle',
                         methods=['POST'])
@require_auth
@api_meta(
    summary='Mint a fresh attach bundle for a browser-pushed agent attach',
    description=(
        'The macOS/Linux zero-config counterpart of the personalized '
        'Windows installer: the signed-in Local Control page relays the '
        'returned bundle (route candidates + a fresh per-user '
        'agents:bridge token) to the unattached agent\'s loopback broker '
        '(``POST /v1/attach``), which validates and persists it. '
        '``?base=`` pins the browser-reachable fallback to the page\'s '
        'live origin+prefix (same host-pinning rule as the installer). '
        '503 when a gated bridge cannot mint the credential.'
    ),
    tags=['capabilities'],
)
def desktop_agent_attach_bundle():
    """SYNC: two dict lookups and a key mint — no I/O worth a thread."""
    from .auth import current_auth, request_principal
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop attach bundle')
    payload, mint_error = _build_attach_bundle(
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    if mint_error:
        return api_error(
            'agent_credential_unavailable', status=503,
            message='the attach bundle is temporarily unavailable — '
                    'retry shortly')
    audit_log('desktop_agent_attach_bundle_served',
              owner_user_id=owner_user_id)
    return api_ok(payload)


@api_v1_desktop_bp.route('/api/v1/desktop/agent-bundle', methods=['GET'])
@api_v1_desktop_bp.route('/api/v1/desktop/agent-installer', methods=['GET'])
@require_auth
@api_meta(
    summary='Download one personalized zero-config agent installer',
    description=(
        'Returns one directly runnable ``.exe``. Its internal NSIS trailer '
        'contains an ordered route-candidate list and, when needed, a fresh '
        'per-user agents:bridge token minted at download time. The installer '
        'writes that data internally; there is no ZIP, sidecar file, unzip '
        'step, pairing code, or user-entered credential. ``?base=`` (the '
        'panel\'s live origin + '
        'prefix, host-pinned) becomes the LAST-RESORT candidate. Behind '
        'an SSO edge direct polling is rejected, so direct-LAN and the '
        'agent-side tunnel rungs come first; the installed agent can also '
        'use that candidate through the signed-in page\'s browser-assisted '
        'transport without receiving its cookies. 409 when '
        'the stored installer predates the attach flow (a rebuild is '
        'kicked automatically).'
    ),
    tags=['capabilities'],
)
def desktop_agent_installer():
    """SYNC on purpose: streams a ~50 MB executable without buffering it."""
    from .auth import current_auth, request_principal

    entry = _agent_store_entry()
    if entry is None:
        return api_not_found(
            'not_found',
            message='no agent installer in the store yet — a build is '
                    'likely in flight; watch /api/v1/desktop/build')
    if not _agent_installer_ready(entry):
        # A stale exe has no internal attachment slot. Serving it as
        # zero-config would be a lie; kick a rebuild and fail explicitly.
        try:
            from lib.desktop_dist import winbuilder as _win_builder
            if not _win_builder.is_running():
                _win_builder.start_installer(reason='installer-stale',
                                             target='agent')
        except Exception as e:
            logger.warning('[Desktop] installer-stale rebuild kick failed: %s', e)
        return api_error('agent_installer_stale', status=409,
                         message='the agent installer predates the '
                                 'one-file attach flow — a rebuild was '
                                 'just kicked; retry in a few minutes')
    path = _dist_store.resolve_file(entry['filename'])
    if path is None:
        return api_not_found('not_found',
                             message='agent artifact missing on disk')

    # Download-time credential stays inside the installer. The user never
    # sees, copies, pastes, or reasons about it.
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop installer')
    attach, mint_error = _build_attach_bundle(
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    if mint_error:
        # Serving an EXE guaranteed to fail would hide the fault until after
        # installation. Keep it internal and ask for a plain retry while the
        # credential authority recovers.
        return api_error(
            'agent_credential_unavailable', status=503,
            message='the controlled-end installer is temporarily '
                    'unavailable — retry shortly')
    from quart import Response
    from lib.desktop_dist.agent_installer import iter_personalized

    # Validate the small record before response headers are committed; a
    # pathological proxy URL must produce a normal JSON error, not a download
    # that truncates after 50 MB when the generator finally rejects it.
    from lib.desktop_dist.agent_installer import encode_attachment
    try:
        encode_attachment(attach)
    except ValueError as e:
        logger.warning('[Desktop] installer attachment refused: %s', e)
        return api_error('agent_attachment_too_large', status=400,
                         message='the externally visible server URL is too '
                                 'long to embed in the installer')

    response = Response(
        iter_personalized(path, attach),
        mimetype='application/vnd.microsoft.portable-executable')
    response.content_length = os.path.getsize(path)
    response.headers.set('Content-Disposition', 'attachment',
                         filename=entry['filename'])
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    logger.info('[Desktop] personalized agent installer downloaded (%s, '
                'candidates=%s, token=%s)', entry['filename'],
                attach['candidates'] + attach['fallback_candidates'],
                'minted' if attach['token'] else 'none')
    return response


# ── Client-diagnostics inbox (owner ask 2026-08-06) ──────────────────
# A controlled machine that cannot reach this server cannot push its own
# logs anywhere — so the agent's tray / role-window carries a「复制诊断
# 信息」button (desktop/agent_launcher._diag_report), and the user pastes
# the bundle HERE. The server appends it to a JSONL file the operator (or
# the assistant) reads straight from disk; the GET lets the panel confirm
# the paste landed. The agent redacts first and the server recursively redacts
# again as mandatory defense in depth; text is capped at _DIAG_MAX_CHARS.
# The file uses the shared
# stream policy rather than growing once per authenticated paste forever.
_DIAG_LOG = os.path.join(_REPO_ROOT, 'logs', 'desktop_client_diag.log')
_DIAG_MAX_CHARS = 200_000
_DIAG_LIST_LIMIT = 20
_DIAG_WRITE_LOCK = threading.Lock()


def _append_client_diag_entry(entry: dict) -> None:
    """Append one redacted JSONL record under the shared stream ceiling."""
    import json as _json

    safe_entry = sanitize_value(
        dict(entry), field_name='desktop_client_diagnostic', max_items=12,
        max_string_chars=_DIAG_MAX_CHARS)
    safe_entry = safe_entry if isinstance(safe_entry, dict) else {}
    safe_entry['text'] = redact_text(
        safe_entry.get('text') or '', max_chars=_DIAG_MAX_CHARS)
    line = (_json.dumps(safe_entry, ensure_ascii=False) + '\n').encode('utf-8')
    os.makedirs(os.path.dirname(_DIAG_LOG), exist_ok=True)
    with _DIAG_WRITE_LOCK:
        ceiling = stream_max_bytes('desktop_client_diag')
        copytruncate_if_oversize(
            _DIAG_LOG,
            max_bytes=ceiling,
            trigger_bytes=max(1, ceiling - len(line)),
            backup_count=stream_backup_count('desktop_client_diag'))
        append_bytes_locked(_DIAG_LOG, line)


@api_v1_desktop_bp.route('/api/v1/desktop/client-diag', methods=['POST'])
@require_auth
@api_meta(
    summary='Store a pasted agent-diagnostics bundle',
    description=(
        'The counterpart of the agent\'s copy-diagnostics button: the user '
        'pastes the clipboard bundle into the Local Control panel and it '
        'lands in logs/desktop_client_diag.log as one JSON line '
        '(ts/user_id/text). 400 on an empty paste; the text is capped at '
        '200k chars.'
    ),
    tags=['capabilities'],
)
async def desktop_client_diag_submit():
    from lib.request_parser import async_parse_body
    from .auth import request_principal
    owner_user_id = request_principal().require_owner(
        context='desktop diagnostics')
    body = await async_parse_body()
    # Manual validation (not optional_str): the refusal must ride the
    # api_error envelope, not the global BadRequest handler.
    text = body.get('text')
    if not isinstance(text, str):
        return api_error('bad_diag', status=400,
                         message='text must be a string')
    text = text.strip()
    if len(text) > _DIAG_MAX_CHARS:
        return api_error('diag_too_large', status=400,
                         message='diagnostics too large (max %d chars)'
                                 % _DIAG_MAX_CHARS)
    if not text:
        return api_error('empty_diag', status=400,
                         message='nothing to store — paste the copied '
                                 'diagnostics first')
    entry = {'ts': time.time(), 'owner_user_id': owner_user_id, 'text': text}
    try:
        _append_client_diag_entry(entry)
    except OSError as e:
        logger.error('[Desktop] client-diag store failed: %s', e,
                     exc_info=True)
        return api_error('diag_store_failed', status=500,
                         message='could not store the diagnostics — '
                                 'see logs/error.log')
    logger.info('[Desktop] client diagnostics received (%d chars, user=%s)',
                len(text), owner_user_id)
    return api_ok({'received': len(text)})


@api_v1_desktop_bp.route('/api/v1/desktop/client-diag', methods=['GET'])
@require_auth
@api_meta(
    summary='List recent pasted agent-diagnostics bundles',
    description=(
        'Newest-first metadata + preview of the last 20 submissions '
        '(the full text stays on disk in logs/desktop_client_diag.log). '
        'Lets the panel confirm a paste landed.'
    ),
    tags=['capabilities'],
)
async def desktop_client_diag_list():
    import json as _json
    from .auth import current_auth

    entries = []
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(_DIAG_LOG, flags)
        with os.fdopen(descriptor, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            if size > 2_000_000:
                f.seek(size - 1_000_000)
                f.readline()  # drop the partial line the seek landed in
                lines = f.readlines()
            else:
                lines = f.readlines()
        auth = current_auth()
        requesting_owner_user_id = str(auth.owner_user_id or '')
        include_all_users = bool(auth and auth.has_scope('admin'))
        for raw_line in reversed(lines):
            try:
                e = _json.loads(raw_line.decode('utf-8', errors='replace'))
            except ValueError as e2:
                logger.debug('[Desktop] diag line undecodable: %s', e2)
                continue
            if not isinstance(e, dict):
                continue
            entry_owner_user_id = str(e.get('owner_user_id') or '')
            if (not include_all_users
                    and entry_owner_user_id != requesting_owner_user_id):
                continue
            entries.append({'ts': e.get('ts'),
                            'owner_user_id': e.get('owner_user_id'),
                            'chars': len(e.get('text') or ''),
                            'preview': (e.get('text') or '')[:200]})
            if len(entries) >= _DIAG_LIST_LIMIT:
                break
    except FileNotFoundError:
        logger.debug('[Desktop] client-diag file absent — nothing to list')
    except OSError as e:
        logger.warning('[Desktop] client-diag list failed: %s', e)
    return api_ok({'entries': entries})


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
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop devices')
    tokens = [
        {'id': k.get('id'), 'name': k.get('name'),
         'created_at': k.get('created_at'),
         'scopes': sorted(k.get('scopes') or [])}
        for k in list_keys(
            owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
        if _BRIDGE_SCOPE in (k.get('scopes') or [])
    ]
    return api_ok({
        'agents': _with_drift(list_agents(user_id=str(owner_user_id))),
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
    from lib.desktop.pairing import (
        _CODE_TTL_S, PairingIdentity, mint_code, pending_codes,
    )
    from .auth import current_auth, request_principal
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop pairing')
    identity = PairingIdentity(
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    code, expires_at = mint_code(identity)
    audit_log('desktop_pair_code_minted', owner_user_id=owner_user_id)
    return api_created({
        'code': code,
        'expires_at': expires_at,
        'ttl': _CODE_TTL_S,
        'pending': pending_codes(owner_user_id),
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
    from quart import request
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
    identity = consume_code(code)
    if identity is None:
        record_pair_failure(client_ip)
        audit_log('desktop_pair_failed', code=code[:2] + '****',
                  reason='invalid_code')
        return api_conflict('invalid_code',
                            message='This pairing code is invalid, expired, '
                                    'or already used. Generate a new one '
                                    'in the panel.')
    record_pair_success(client_ip)
    row, token = create_key(
        name,
        scopes=[_BRIDGE_SCOPE],
        owner_user_id=identity.owner_user_id,
        account_user_id=identity.account_user_id,
        tenant_id=identity.tenant_id,
    )
    audit_log('desktop_pair_succeeded', key_id=row.get('id'),
              owner_user_id=identity.owner_user_id, platform=platform)
    return api_created({
        'id': row.get('id'),
        'name': name,
        'token': token,
        'scopes': [_BRIDGE_SCOPE],
        'owner_id': identity.owner_user_id,
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
    from .auth import current_auth, request_principal
    auth = current_auth()
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop token mint')
    body = await async_parse_body()
    name = optional_str(body, 'name', default='', max_len=80).strip() \
        or 'desktop-bridge'
    row, token = create_key(
        name,
        scopes=[_BRIDGE_SCOPE],
        owner_user_id=owner_user_id,
        account_user_id=(auth.account_user_id if auth else ''),
        tenant_id=principal.tenant_id,
    )
    audit_log('desktop_bridge_token_minted', key_id=row.get('id'),
              name=name, owner_user_id=owner_user_id)
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
    from .auth import request_principal
    principal = request_principal()
    owner_user_id = principal.require_owner(context='desktop token revoke')
    row = get_key_by_id(
        key_id, owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
    if not row or _BRIDGE_SCOPE not in (row.get('scopes') or []):
        return api_not_found('not_found',
                             message='bridge token not found')
    revoke_key(
        key_id, owner_user_id=owner_user_id, tenant_id=principal.tenant_id)
    audit_log('desktop_bridge_token_revoked', key_id=key_id,
              owner_user_id=owner_user_id)
    return api_ok({'revoked': key_id})


__all__ = ['api_v1_desktop_bp']

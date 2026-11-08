"""HTTP authentication, request identity and route scope guards.

auth_before_request resolves one request-local credential candidate through
current storage, applies auth_mode and the synthetic-admin trust boundary, and
stamps AuthContext/PrincipalContext. Static assets and credential-only device
bridges retain their distinct admission paths. Cookie redirects, typed HTTP
rejections, rate headers and scope decorators remain owned here.

lib.api_keys.transport owns token precedence and selection metadata. WebSocket
admission uses that same selector with its own connection lifecycle; mobile
links reuse presented_credential without parsing or authenticating again.
Identity and credential authority are defined in docs/IDENTITY.md.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from typing import Optional

from quart import current_app, redirect, request
from quart import Response, g

from lib.api_keys import (
    AuthContext, local_admin_context, validate_token,
)
from lib.api_keys.transport import (
    CredentialCandidate, SESSION_COOKIE, select_credential,
)
from lib.api_response import api_forbidden, api_typed_error, api_unauthorized
from lib.auth_mode import requires_credential as _mode_requires_credential
from lib.auth_mode import is_multi_user
from lib.identity import PrincipalContext, principal_from_auth_context
from lib.log import audit_log, get_logger, set_principal
from lib.rate_limit.rate_limit_api import RateDecision, apply_headers, check_request
from lib.usage_tracker import record as record_usage

logger = get_logger(__name__)
_auth_log = logging.getLogger('server.auth')


# Cookie that pins a session to a Bearer token. HttpOnly so the value
# never leaks to JS; SameSite=Lax so same-origin XHR keeps working but
# a third-party form submit can't authenticate.
SESSION_COOKIE_MAX_AGE = 86400 * 30


# Path prefixes that participate in the API rate-limit / 401 contract
# beyond the implicit "everything that's not in the public list". We
# keep this list explicit because ``/metrics`` is at the top level and
# easy to overlook, and so the bearer middleware's request-counter
# treats all three surfaces uniformly.
_API_PREFIXES = ('/api/', '/v1/', '/metrics')


# Routes that don't require auth. Anchored exact-match OR prefix-match.
# Keep this list short \u2014 every entry is a potential information leak.
#
# Note: ``/`` is NOT public. A fresh browser visit without a cookie
# lands on the friendly hint page (rendered below) telling the user
# to append ``?token=\u2026``. Once they do, the cookie is installed and
# every subsequent same-origin call works seamlessly.
_PUBLIC_EXACT = frozenset({
    '/favicon.ico',
    '/favicon.svg',
    '/robots.txt',
    '/api/health',
    '/api/ready',
    '/health/live',
    '/health/ready',
    '/health/startup',
    '/api/openapi.json',
    '/api/openapi.yaml',
    '/api/docs',
    '/api/redoc',
    '/api/v4/meta',
    '/api/v4/openapi.json',
    '/api/v1/capabilities',
    '/api/v1/keys/whoami',
    '/api/v1/auth/mode',  # GET only; PUT goes through @require_scope('admin')
    '/api/v1/billing/pricing',  # public price card; mutation paths require admin
    '/api/v1/billing/webhooks/stripe',  # auth via signed payload
    '/api/v1/billing/webhooks/alipay',  # auth via RSA2 signature
    '/api/v1/users/signup',    # public registration (gated by relay.json)
    '/api/v1/users/login',     # public login
    '/api/v1/users/logout',    # public; idempotent on missing session
    '/api/v1/users/me',        # public probe; ownerId is null when unauthed
    '/api/desktop/pair',          # pairing exchange: the 6-digit code IS the credential (RWA P4a — code + audit, no bearer)
    '/dashboard',         # customer dashboard HTML; data fetches go through the gate
    '/dashboard/',
    '/login',             # signup/login HTML page
    '/login/',
    '/signup',
    '/signup/',
})
_PUBLIC_PREFIXES = (
    '/static/',
    '/.well-known/',
)


def _is_v4_public(path: str) -> bool:
    """Mirror the v1 public allowlist onto the dual-mounted v4 projection.

    Derived (never hand-listed) so a v1 path and its v4 twin can never drift
    apart. Gated by the same ``API_V4_ENABLED`` latch that exposes the routes,
    so a hidden v4 surface is never accidentally public.
    """
    if not (path == '/api/v4' or path.startswith('/api/v4/')):
        return False
    try:
        from quart import current_app
        from lib.api_v4 import v4_routes_enabled
        if not v4_routes_enabled(current_app._get_current_object()):
            return False
    except RuntimeError:
        return False
    return _is_public('/api/v1' + path[len('/api/v4'):])


# Device onboarding: the caller is another machine and the one-time code in
# its payload is the credential. A local kernel listens on loopback only, so no
# device can dial it; there the exchange keeps the default deny.
_DEVICE_PAIRING_EXCHANGE = '/api/desktop/pair'


def _is_public(path: str) -> bool:
    if (path == _DEVICE_PAIRING_EXCHANGE
            and current_app.extensions.get('tofu_local_kernel') is not None):
        return False
    if path in _PUBLIC_EXACT:
        return True
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    return _is_v4_public(path)


# Open mode hands every request a synthetic full-admin context. That is
# safe ONLY for a loopback-bound personal install. If the server is bound
# to a routable interface (0.0.0.0, Docker port-map, a tunnel) while in
# open mode, a remote client would otherwise reach the admin API with no
# credential. We therefore restrict the synthetic grant to loopback peers
# unless the operator explicitly opts in.
_OPEN_MODE_ALLOW_REMOTE = (
    os.environ.get('TOFU_OPEN_MODE_ALLOW_REMOTE', '').strip().lower()
    in ('1', 'true', 'yes', 'on'))


def address_is_loopback(addr) -> bool:
    """Return whether an HTTP/ASGI peer address is local loopback.

    Accepts the string shape used by ``request.remote_addr`` and the
    ``(host, port)`` tuple carried by an ASGI HTTP/WebSocket scope.  Keeping
    this pure lets the WebSocket gate enforce the exact same trust boundary
    as the HTTP middleware (Quart does not run ``before_request`` for WS).
    """
    import ipaddress
    if isinstance(addr, (tuple, list)):
        addr = addr[0] if addr else ''
    addr = str(addr or '').strip()
    if not addr:
        return False
    if addr == '<local>':
        return True
    addr = addr.split('%', 1)[0]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError as e:
        _auth_log.debug('Auth: unparseable peer address %r: %s', addr, e)
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, 'ipv4_mapped', None)
    return bool(mapped and mapped.is_loopback)


def open_mode_peer_allowed(addr) -> bool:
    """Whether open mode may synthesize an admin for this socket peer."""
    return _OPEN_MODE_ALLOW_REMOTE or address_is_loopback(addr)


def local_kernel_authority_refused(host) -> bool:
    """Whether a local kernel must refuse this Host (never outside one).

    Every app and web page on the device reaches a local kernel's loopback
    port; answering only its own authority defeats DNS rebinding.
    ``local_kernel.py`` publishes that authority before serving.
    """
    local_kernel = current_app.extensions.get('tofu_local_kernel')
    if local_kernel is None:
        return False
    return str(host or '').strip().lower() != local_kernel['host']


async def local_kernel_host_fence():
    """Answer a foreign Host with 403 before any other request gate.

    Application assembly registers this right after request correlation, ahead
    of the API-version, boot and storage gates, so a rebound page gets the same
    403 while the kernel boots as after.
    """
    if not local_kernel_authority_refused(request.host):
        return None
    _auth_log.warning('Auth: local kernel refused Host %.120s on %s',
                      request.host, request.path)
    return api_typed_error(
        'permission', status=403,
        detail='This local kernel answers only its own loopback address.',
        source='api_v1.auth.local_kernel_host')


_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def browser_request_is_cross_site(headers, host) -> bool:
    """Whether a browser reports this request as started by another site.

    Every page the local user visits can reach a loopback server, so open
    mode must not lend its synthetic admin to such a request. Fetch Metadata
    decides when present; browsers without it are judged by ``Origin``.
    Headless clients send neither header.
    """
    site = str(headers.get('Sec-Fetch-Site') or '').strip().lower()
    if site:
        return site not in ('same-origin', 'none')
    origin = str(headers.get('Origin') or '').strip()
    if not origin:
        return False
    from urllib.parse import urlparse
    return urlparse(origin).netloc.lower() != str(host or '').strip().lower()


def _remote_is_loopback() -> bool:
    """True when the request peer is the local host (127.0.0.0/8, ::1).

    Uses ``request.remote_addr`` — the direct socket peer, NOT any
    ``X-Forwarded-For`` header (which a remote client can spoof).

    ⚠️ NOT a trust signal by itself. A reverse proxy on the SAME host
    (nginx / ngrok / cloudflared → 127.0.0.1, the standard tunnel shape)
    makes EVERY public request present as loopback, and ProxyFix is not
    installed (), so the server cannot tell them
    apart. Bridge endpoints therefore require a CREDENTIAL and never
    consult this — see :func:`_is_bridge_path` and
    ``docs/modules/integrations_api.md`` §3.2b / §3.4.
    """
    return address_is_loopback(request.remote_addr)


# ── Bridge endpoints: credential-only, never address-based ────────────
#
# The browser extension and desktop agent poll these. A bridge command can
# read the whole cookie jar, attach the DevTools debugger, write files and
# run shell commands — strictly more dangerous than reaching the plain UI.
# They are therefore exempt from the open-mode synthetic-admin grant: a real
# credential is required no matter what the peer address looks like and no
# matter how TOFU_OPEN_MODE_ALLOW_REMOTE is set.
_BRIDGE_PATHS = frozenset({
    '/api/browser/poll',
    '/api/browser/commands',
    '/api/browser/result',
    '/api/desktop/poll',
})
_BRIDGE_PATH_PREFIXES = (
    '/api/browser/file-transfers/',
)

def _is_bridge_path(path: str) -> bool:
    return path in _BRIDGE_PATHS or any(
        path.startswith(prefix) for prefix in _BRIDGE_PATH_PREFIXES)


async def _bridge_auth_context():
    """Resolve the device caller once, with no address or shared-secret trust."""
    provided = (request.headers.get('X-Bridge-Secret') or '').strip()
    if not provided:
        return None
    allow_process_agent = request.path == '/api/desktop/poll'
    from lib.bridge_auth import resolve_bridge_credential
    # Current-authority validation and conditional audit touch use synchronous
    # storage ports, so keep both off the serving event loop.
    return await asyncio.to_thread(
        resolve_bridge_credential,
        provided,
        allow_process_agent=allow_process_agent,
    )


async def _validate_token(token: str) -> Optional[AuthContext]:
    """Validate and stamp a token without blocking Quart's event loop."""
    return await asyncio.to_thread(validate_token, token)


def _is_api_path(path: str) -> bool:
    """Path participates in the headless contract (rate limits + 401 envelope).

    Everything under ``/api/`` (including the legacy single-user routes)
    counts. ``/v1/*`` (compat) and ``/metrics`` (admin) likewise. Static
    files, the index, and the well-known prefix are explicitly NOT
    api paths and short-circuit out at the public-allow-list step.
    """
    if _is_public(path):
        return False
    return any(path.startswith(p) for p in _API_PREFIXES)


# \u2500\u2500 Token extraction \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def presented_credential() -> CredentialCandidate:
    """Select once per HTTP request; raw-token adapters reuse the same candidate.

    This request-local value is not authentication. Each middleware invocation
    validates it against current storage; no result survives into another request.
    """
    candidate = getattr(g, 'credential_candidate', None)
    if candidate is None:
        candidate = select_credential(request.headers, request.cookies, request.args)
        g.credential_candidate = candidate
    return candidate


# \u2500\u2500 Middleware \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _rate_limit_rejection(decision: RateDecision):
    resp, status = api_typed_error(
        'ratelimit', status=429,
        detail=f'Rate limit exceeded ({decision.reason})',
        source='api_v1.auth.rate_limit',
        extensions={
            'retry_after_s': round(decision.retry_after_s, 2),
        })
    apply_headers(resp, decision)
    return resp, status


def _stamp_principal() -> PrincipalContext:
    """Bind the resolved ``g.auth_ctx`` to the principal ContextVar.

    Lets services consume one structured identity and ``lib.log.audit_log``
    attach its stable subject/owner. Invalid or missing ownership raises so
    the auth boundary can deny the request before a repository is reached.
    """
    ctx = getattr(g, 'auth_ctx', None)
    principal = principal_from_auth_context(
        ctx, allow_personal_owner=not is_multi_user())
    g.principal_context = principal
    set_principal(principal.subject_id, principal.owner_user_id or '')
    return principal


def _principal_binding_rejection(error: BaseException):
    logger.warning('[Auth] principal binding refused: %s', error)
    return api_typed_error(
        'permission', status=403,
        detail='Authenticated principal has no valid owner identity.',
        source='api_v1.auth.principal')


async def auth_before_request():
    """Resolve ``g.auth_ctx`` for every request.

    Sets ``g.auth_ctx`` and ``g.rate_decision``. Returns a Response only
    when the request is rejected (401 / 429 / 401-redirect-with-cookie).
    """
    path = request.path
    g.auth_ctx = None
    g.credential_candidate = None
    g.principal_context = None
    g.rate_decision = None
    g.browser_poll_admission_lease = None

    # Static assets short-circuit before any token work — they're hit
    # tens of times per page load and should never touch the cache.
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return None

    # ── Bridge endpoints: credential-only, address-blind ────────────
    # Placed BEFORE the open-mode short-circuit on purpose: otherwise the
    # synthetic local-admin grant would wave a bridge poll through on peer
    # address alone, and under a same-host reverse proxy that is the whole
    # public internet (docs/modules/integrations_api.md §3.2b).
    # TOFU_OPEN_MODE_ALLOW_REMOTE cannot downgrade this (§3.4b).
    if _is_bridge_path(path):
        # CORS preflight carries NO credentials by spec (the browser strips
        # them), so gating OPTIONS would make every cross-origin bridge call
        # impossible. The preflight reveals nothing and mutates nothing; the
        # actual POST/GET that follows is still fully gated below.
        if request.method == 'OPTIONS':
            return None
        if path == '/api/browser/poll':
            from lib.browser.poll_admission import browser_poll_admission
            poll_controller = browser_poll_admission()
            _, poll_lease = poll_controller.enter(
                credential=request.headers.get('X-Bridge-Secret', ''),
                peer=request.remote_addr or '',
                reported_protocol_version=request.headers.get(
                    'X-Browser-Protocol-Version', ''),
            )
            g.browser_poll_admission_lease = poll_lease
        bridge_context = await _bridge_auth_context()
        if bridge_context is not None:
            g.auth_ctx = bridge_context
            g.bridge_auth_context = bridge_context
            try:
                _stamp_principal()
            except (PermissionError, ValueError) as exc:
                return _principal_binding_rejection(exc)
            if path == '/api/browser/poll':
                poll_controller.admit_owner(
                    poll_lease,
                    owner_user_id=bridge_context.owner_user_id,
                )
            return None
        try:
            audit_log('bridge_auth_fail', kind='gate', path=path,
                      ip=request.remote_addr,
                      has_header=bool(request.headers.get('X-Bridge-Secret')),
                      ua=(request.user_agent.string or '')[:120])
        except Exception as _aerr:
            logger.debug('[Auth] bridge audit_log failed: %s', _aerr)
        _auth_log.warning('Auth: bridge credential required on %s (peer=%s)',
                          path, request.remote_addr)
        return api_unauthorized(
            'bridge_auth_required',
            hint='pair this device to obtain an agents:bridge credential')

    # ── Open mode short-circuit ─────────────────────────────────────
    # No credential required. Tokens are still honoured if presented
    # (so the same Bearer header / cookie keeps working when an
    # operator later switches to private mode), but missing/invalid
    # ones do NOT 401 — every request gets a synthetic full-privilege
    # context. Rate limiting and idempotency keying treat the
    # synthetic context as "no real principal" (see
    # ``lib.rate_limit.rate_limit_api`` / ``lib.idempotency``).
    credential_required = _mode_requires_credential()
    candidate = presented_credential()
    token = candidate.token
    ctx = await _validate_token(token) if token else None
    if not credential_required:
        ctx_open = ctx
        # Synthetic full-admin grant is loopback-only by default. A
        # remote peer in open mode does NOT get the free admin context;
        # it must present a valid credential (resolved above) or it
        # falls through to the private-mode rejection path below. This
        # closes the "bind 0.0.0.0 + open mode = unauthenticated admin"
        # foot-gun. Operators who front the server with their own auth
        # can opt back in via TOFU_OPEN_MODE_ALLOW_REMOTE=1.
        if ctx_open is None:
            if (request.method not in _SAFE_METHODS
                    and browser_request_is_cross_site(
                        request.headers, request.host)):
                # CSRF: another site's page may not write as the local admin.
                _auth_log.warning(
                    'Auth: open-mode synthetic admin refused for cross-site '
                    '%s %s (origin=%.120s)', request.method, path,
                    request.headers.get('Origin') or '')
            elif open_mode_peer_allowed(request.remote_addr):
                ctx_open = local_admin_context()
            else:
                # Remote, unauthenticated, open mode → behave like
                # private mode for this request (fall through).
                _auth_log.warning(
                    'Auth: open-mode synthetic admin refused for non-loopback '
                    'peer %s on %s (set TOFU_OPEN_MODE_ALLOW_REMOTE=1 to allow)',
                    request.remote_addr, path)
        if ctx_open is not None:
            g.auth_ctx = ctx_open
            try:
                _stamp_principal()
            except (PermissionError, ValueError) as exc:
                return _principal_binding_rejection(exc)
            if ctx_open.via_open_mode and _is_api_path(path):
                # Open-mode admission has no resource counter or worker hop.
                g.rate_decision = check_request(ctx_open)
            return None
        # else: fall through to the credential-required gate below.

    # The private gate must use the derived check (exact + prefix + v4
    # mirror), not only _PUBLIC_EXACT: after the v1 mount was retired,
    # traffic arrives at /api/v4/<public> and an exact-v1-path lookup would
    # 401 every previously public endpoint.
    is_public = _is_public(path)

    # 1. Try API keys + cookie + query-string first (single auth model).
    #    We resolve even on public paths so /api/v1/keys/whoami can tell
    #    the caller who they are.
    used_query_token = False
    if token:
        if ctx is not None:
            audit_log('api_request_auth', key_id=ctx.key_id,
                      name=ctx.name, path=path)
            if (request.args.get('token') or '').strip() == token:
                used_query_token = True
        else:
            # Wrong / expired token: 401 immediately, regardless of
            # public-list status. Don't fall through to other auth
            # mechanisms because the user has clearly tried to
            # authenticate and we should tell them it failed.
            # Log a token prefix (first 16 chars — enough to grep, not
            # enough to be a usable secret) + the transport it arrived
            # on, so a token-vs-keystore mismatch (e.g. a stale
            # .first_run_token) is diagnosable from logs/app.log alone.
            _auth_log.warning('Auth: rejected token prefix=%.16s source=%s '
                              '(path=%s remote=%s)', token,
                              candidate.source, path,
                              request.remote_addr)
            return api_typed_error(
                'permission', status=401,
                detail='Invalid or expired API key. If you copied it from '
                       'data/config/.first_run_token, that token may have '
                       'been rotated — restart the server to mint a fresh one.',
                source='api_v1.auth.token')

    g.auth_ctx = ctx
    if ctx is not None:
        try:
            _stamp_principal()
        except (PermissionError, ValueError) as exc:
            return _principal_binding_rejection(exc)

    # 2. Browser landed on / with ?token=<key>: install cookie + redirect.
    if used_query_token and request.method == 'GET':
        from urllib.parse import urlencode, parse_qs, urlparse, urlunparse
        parsed = urlparse(request.url)
        params = parse_qs(parsed.query)
        params.pop('token', None)
        clean_query = urlencode(params, doseq=True)
        clean_url = urlunparse(parsed._replace(query=clean_query))
        resp = redirect(clean_url)
        resp.set_cookie(SESSION_COOKIE, token,
                        max_age=SESSION_COOKIE_MAX_AGE,
                        httponly=True, samesite='Lax',
                        secure=request.is_secure)
        _auth_log.info('Auth: cookie installed for %s (key=%s)',
                       request.remote_addr, ctx.key_id if ctx else '?')
        return resp

    # 4. Public path \u2014 may proceed regardless of auth resolution.
    #    Public + key present: still rate-check so X-RateLimit-* headers
    #    appear (clients use them for back-pressure even on capabilities/
    #    whoami), but never enforce 429 \u2014 public means public.
    if is_public:
        if ctx and ctx.key_id:
            decision: RateDecision = check_request(ctx)
            g.rate_decision = decision
        return None

    # 5. Reject when no credential resolved on a private path.
    if ctx is None:
        if path.startswith(('/api/', '/v1/', '/metrics')):
            return api_typed_error(
                'permission', status=401,
                detail='Authentication required. Send Authorization: '
                       'Bearer tofu_live_…',
                source='api_v1.auth.required')
        return Response(
            '<!doctype html><meta charset="utf-8">'
            '<title>Sign in required \u2014 Tofu</title>'
            '<style>body{font:14px/1.5 system-ui,sans-serif;margin:6em auto;'
            'max-width:36em;padding:0 1.5em;color:#222}h2{margin:0 0 .5em}'
            'code{background:#f3f3f3;padding:.1em .4em;border-radius:3px}</style>'
            '<h2>\U0001f512 Sign in required</h2>'
            '<p>This Tofu instance is private. Open this URL with '
            '<code>?token=YOUR_TOKEN</code> appended, or send '
            '<code>Authorization: Bearer YOUR_TOKEN</code>.</p>'
            '<p>The token is printed on first server boot and saved to '
            '<code>data/config/.first_run_token</code>.</p>'
            '<p>If you copied a token from that file but still get '
            '<em>Invalid or expired API key</em>, the key was rotated — '
            'restart the server to mint a fresh one.</p>',
            status=401, content_type='text/html; charset=utf-8',
        )

    # 6. Rate-limit pre-flight (only for keys with a configured budget).
    decision: RateDecision = check_request(ctx)
    g.rate_decision = decision
    if not decision.allowed:
        return _rate_limit_rejection(decision)

    # 7. Per-key request counter (tokens recorded post-hoc by routes).
    if ctx.key_id:
        try:
            # The amortized usage counter occasionally flushes its JSON file.
            await asyncio.to_thread(
                record_usage, ctx.key_id, request_count=1)
        except Exception as e:
            logger.debug('[Auth] usage record failed: %s', e)
    return None


async def attach_rate_headers(response):
    """Attach API rate headers and release any browser-poll admission lease."""
    try:
        decision = getattr(g, 'rate_decision', None)
        if decision is not None:
            apply_headers(response, decision)
    except Exception as e:
        logger.debug('[Auth] rate-header hook failed: %s', e)
    finally:
        _release_browser_poll_admission()
    return response


def _release_browser_poll_admission() -> None:
    """Release the request lease; after-request and teardown may both call."""
    try:
        lease = getattr(g, 'browser_poll_admission_lease', None)
        if lease is None:
            return
        from lib.browser.poll_admission import browser_poll_admission
        browser_poll_admission().release(lease)
    except Exception as exc:
        logger.warning('[Auth] browser poll admission release failed: %s', exc)


async def release_browser_poll_admission_on_teardown(_error=None):
    """Cancellation/error backstop for requests that never build a response."""
    _release_browser_poll_admission()


# \u2500\u2500 Decorators \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def current_auth() -> Optional[AuthContext]:
    """Return the ``AuthContext`` for the current request (or None)."""
    try:
        return getattr(g, 'auth_ctx', None)
    except RuntimeError as e:
        # Working outside of application/request context.
        logger.debug('[Auth] current_auth called outside request ctx: %s', e)
        return None


def request_principal() -> PrincipalContext:
    """Return the request identity or deny context-less/background callers."""
    try:
        principal = getattr(g, 'principal_context', None)
    except RuntimeError:
        principal = None
    if isinstance(principal, PrincipalContext):
        return principal
    # Test adapters and a few blueprint-local contexts set ``g.auth_ctx``
    # directly. Normalize them through the same boundary without restoring a
    # context-less personal fallback.
    return principal_from_auth_context(
        current_auth(), allow_personal_owner=not is_multi_user())


def human_ui_provenance() -> dict:
    """Provenance of an owner acting in the Tofu UI, or PermissionError.

    Decisions only a human may make (lifecycle approvals and the owner's
    delegation to their Global Router, lib/lifecycle_approval.py) accept a
    request only when the principal is a user, the credential is the UI
    session cookie or the loopback open-mode UI (never an API key, bearer
    header or query token: those are headless clients), and the request
    carries same-origin browser evidence: Fetch Metadata where the browser
    sends it (trustworthy origins), otherwise an ``Origin`` naming this host.
    Headless clients send neither unless they forge them deliberately; the
    user agent and peer stay on the record. Open mode has no authentication,
    so there this is a claim (lib.lifecycle_approval.provenance_assurance).
    """
    provenance = _ui_provenance()
    if provenance['kind'] != 'human-ui':
        audit_log('human_ui_provenance_refused', path=request.path,
                  credential=provenance['credential'], browser=provenance['browser'] or '-',
                  ua=provenance['userAgent'][:200], remote=provenance['remoteAddr'])
        raise PermissionError(
            'human_approval_required: open the Tofu UI in your browser and decide there; '
            'API keys, scripts and agents cannot make this decision')
    return provenance


def request_provenance() -> dict:
    """Who made this request: ``human-ui`` when it qualifies, else ``authenticated-request``."""
    return _ui_provenance()


def _ui_provenance() -> dict:
    principal = request_principal()
    context = current_auth()
    source = presented_credential().source
    if context is not None and context.via_open_mode:
        credential = 'open-mode'
    elif context is not None and source == 'cookie':
        credential = 'session'
    else:
        credential = ''
    site = str(request.headers.get('Sec-Fetch-Site') or '').strip().lower()
    origin = str(request.headers.get('Origin') or '').strip()
    if site:
        browser = 'fetch-metadata' if site == 'same-origin' else ''
    else:
        from urllib.parse import urlparse
        browser = ('origin' if origin and urlparse(origin).netloc.lower()
                   == str(request.host or '').strip().lower() else '')
    human = principal.kind == 'user' and bool(credential) and bool(browser)
    return {'kind': 'human-ui' if human else 'authenticated-request',
            'ownerUserId': principal.require_owner(context='request provenance'),
            'subject': principal.subject_id, 'credential': credential if human else source,
            'browser': browser, 'userAgent': (request.headers.get('User-Agent') or '')[:300],
            'remoteAddr': request.remote_addr or ''}


def request_user_id() -> int:
    """Return the authenticated owner for the current request.

    Personal mode maps an unbound local administrator to the declared personal
    owner. Multi-user mode fails closed when authentication did not bind an
    owner, so storage code never silently crosses tenant boundaries.
    """
    return request_principal().require_owner(context='request')


def require_auth(fn):
    """Decorator: 401 if no AuthContext is attached.

    Most routes prefer ``@require_scope('…')`` which implies auth.

    Dual-mode: an ``async def`` handler is wrapped by an async wrapper so
    it stays a coroutine function (Quart awaits it natively); a sync
    handler keeps a sync wrapper (Quart runs it in its thread-pool).
    """
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = current_auth()
            if ctx is None or not ctx.is_authenticated:
                return api_unauthorized('Authentication required')
            return await fn(*args, **kwargs)
        return wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = current_auth()
        if ctx is None or not ctx.is_authenticated:
            return api_unauthorized('Authentication required')
        return fn(*args, **kwargs)
    return wrapper


def require_scope(*scopes: str):
    """Decorator: require ALL given scopes (or admin) on the current key.

    Cookie-authenticated UI calls have admin scope (matches the
    historical privilege level of the local browser surface). Headless
    callers must have every listed scope on their key.
    """
    if not scopes:
        raise ValueError('require_scope needs at least one scope')

    def _denied(ctx):
        """Return the rejection response, or None when access is granted."""
        if ctx is None or not ctx.is_authenticated:
            return api_unauthorized('Authentication required')
        for sc in scopes:
            if not ctx.has_scope(sc):
                audit_log('api_forbidden', key_id=ctx.key_id,
                          name=ctx.name, missing_scope=sc,
                          path=request.path)
                return api_forbidden(
                    f'Missing required scope: {sc}',
                    missing_scope=sc,
                    required_scopes=list(scopes),
                    granted_scopes=sorted(ctx.scopes),
                )
        return None

    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                denied = _denied(current_auth())
                if denied is not None:
                    return denied
                return await fn(*args, **kwargs)
            wrapper._required_scopes = list(scopes)
            return wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            denied = _denied(current_auth())
            if denied is not None:
                return denied
            return fn(*args, **kwargs)
        wrapper._required_scopes = list(scopes)
        return wrapper
    return decorator


def model_relay_guard(*, is_byo: bool = False):
    """Backstop for BYO-only deployments (``model_relay_enabled=false``).

    Returns a 403-style rejection Response when the current request would
    consume the OPERATOR's model slot pool on a relay that has opted out
    of being a model intermediary; returns ``None`` (proceed) otherwise.

    The PRIMARY control is the scope strip at key-mint time (a BYO-only
    tenant key never carries ``chat``). This is the defense-in-depth
    backstop that also refuses a stale pre-flag key still holding ``chat``.

    Allowed through even when model relay is off:
      * ``is_byo=True`` — every candidate in the request-scoped v2 route
        group belongs to an owner-scoped ProviderAccess. No operator/public
        credential can be selected during failover.
      * admin / operator keys — the operator running their own instance
        is not a tenant; they keep full access to their pool.

    Cheap: one cached config read; no DB hit.
    """
    if is_byo:
        return None
    from lib.relay_config import model_relay_enabled
    if model_relay_enabled():
        return None
    ctx = current_auth()
    if ctx is not None and ctx.has_scope('admin'):
        return None
    audit_log('model_relay_denied',
              key_id=(ctx.key_id if ctx else ''),
              name=(ctx.name if ctx else ''),
              path=request.path)
    return api_forbidden(
        'This relay does not provide model access (BYO-only mode). '
        'Configure an owner-scoped ProviderAccess via /api/v1/providers '
        'and select a structured model through the v2 routing authority.',
        error_kind='model_relay_disabled')


def guard_model_relay_or_dispose(route_group):
    """Backstop all completion surfaces and dispose a denied v2 route group.

    Collapses the per-route boilerplate ::

        denied = model_relay_guard(is_byo=route_is_owner_scoped)
        if denied is not None:
            return denied

    A mixed owner/public failover set is not BYO-only: allowing the primary
    owner candidate while retaining an operator candidate would make the
    policy disappear on retry. Denial disposes every slot immediately.
    """
    candidates = list(getattr(route_group, 'candidates', ()) or ())
    owner_scoped = bool(candidates) and all(
        str(getattr(candidate, 'provider', {}).get('scope') or '') == 'owner'
        for candidate in candidates
    )
    denied = model_relay_guard(is_byo=owner_scoped)
    if denied is None:
        return None
    if route_group is not None:
        try:
            from lib.model_routing import dispose_routed_slot_group
            dispose_routed_slot_group(route_group)
        except Exception as e:
            logger.warning(
                '[ModelRelay] route-group dispose on reject failed: %s', e)
    return denied


# Legacy aliases used by ``server.py`` and tests during the transition
# from the old name. Both refer to the same callable.
bearer_auth_before_request = auth_before_request


__all__ = [
    'auth_before_request',
    'bearer_auth_before_request',
    'attach_rate_headers',
    'release_browser_poll_admission_on_teardown',
    'require_auth',
    'require_scope',
    'model_relay_guard',
    'guard_model_relay_or_dispose',
    'current_auth',
    'presented_credential',
    'request_principal',
    'request_user_id',
    'SESSION_COOKIE',
    'address_is_loopback',
    'local_kernel_authority_refused',
    'local_kernel_host_fence',
    'browser_request_is_cross_site',
    'open_mode_peer_allowed',
]

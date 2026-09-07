"""routes/api_v1/auth.py — Single global auth gate for the whole app.

Replaces the previous dual scheme (``server.py:tunnel_auth`` + this
middleware). One ``before_request`` hook resolves an :class:`AuthContext`
once per request, with behavior gated by :mod:`lib.auth_mode`.

Modes (see ``lib/auth_mode.py``)
--------------------------------
  * ``open``       — no credential required; every request gets a
                      synthetic local-admin context. Tokens are still
                      honoured if presented so a single multi-device
                      operator can move to ``private`` without any
                      client-side change.
  * ``private``    — Bearer/cookie required. Hint page on the index,
                      401 on every other non-public path.
  * ``multi-user`` — Same gate as ``private``; reserved for future
                      RBAC differentiation (currently identical).

Token transports (priority, all modes):

  1. ``Authorization: Bearer <token>`` — programmatic / SDK clients.
  2. ``x-api-key: <token>``            — Anthropic SDK convention.
  3. ``tofu_session`` cookie           — set on first browser visit.
  4. ``?token=<token>`` query string   — first-link flow; sets cookie
                                          + redirects to clean URL.
For all four valid paths the resolved context lives at ``g.auth_ctx``.
Routes consult it via :func:`require_auth` / :func:`require_scope`.

Public path policy
------------------
A short allow-list of routes can be reached without a token:

  * ``/``, ``/index.html``, ``/static/*``, ``/favicon.*``, ``/robots.txt``
  * ``/.well-known/*``
  * ``/api/health``                      (liveness probe)
  * ``/api/openapi.json|yaml``,
    ``/api/docs``, ``/api/redoc``        (self-describing surface)
  * ``/api/v4/meta``, ``/api/v4/openapi.json``
                                             (v4 compatibility bootstrap)
  * ``/api/v1/capabilities``             (used by clients to auto-config)
  * ``/api/v1/keys/whoami``              (login probe \u2014 returns
                                          ``{authenticated:false}`` when
                                          unauthenticated)

Everything else \u2014 the old single-user ``/api/*`` surface as well as
``/api/v1/*``, ``/v1/*``, ``/metrics`` \u2014 requires a valid credential.

Single-user comfort (private mode only)
---------------------------------------
On first boot in ``private`` mode ``lib.api_keys.bootstrap_personal_key`` mints a
``tofu_admin_\u2026`` token, prints it to stderr, and persists it (0600) at
``data/config/.first_run_token``. The launcher prints a one-shot URL
``http://host:port/?token=<token>`` so opening the browser once
installs the cookie; subsequent visits authenticate from the cookie
alone.

Rate limiting
-------------
Pre-flight bucket check + standard ``X-RateLimit-*`` headers run for
every authenticated API request. Public paths never enforce 429.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from typing import Optional

from quart import redirect, request
from quart import Response, g

from lib.api_keys import (
    AuthContext, local_admin_context, validate_token,
)
from lib.api_response import api_forbidden, api_typed_error, api_unauthorized
from lib.auth_mode import requires_credential as _mode_requires_credential
from lib.auth_mode import is_multi_user
from lib.identity import PrincipalContext, principal_from_auth_context
from lib.log import audit_log, get_logger, set_principal
from lib.rate_limit_api import RateDecision, apply_headers, check_request
from lib.usage_tracker import record as record_usage

logger = get_logger(__name__)
_auth_log = logging.getLogger('server.auth')


# Cookie that pins a session to a Bearer token. HttpOnly so the value
# never leaks to JS; SameSite=Lax so same-origin XHR keeps working but
# a third-party form submit can't authenticate.
SESSION_COOKIE = 'tofu_session'
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


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


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
    # Credential validation is a synchronous Sidecar write (it stamps
    # last_used_at). Under writer pressure it may wait for the storage
    # deadline, so it must run on the serving loop's bounded sync executor.
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


def _extract_bearer_or_cookie() -> str:
    """Return the candidate token from any supported transport.

    Priority order: explicit Authorization header > x-api-key header >
    session cookie > query string. The query-string path is purely a
    convenience for first-time browser links; the redirect handler
    consumes it before any route sees it.
    """
    auth = request.headers.get('Authorization', '') or ''
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == 'bearer':
            tok = parts[1].strip()
            if tok:
                return tok
    x_api_key = (request.headers.get('x-api-key') or '').strip()
    if x_api_key.startswith(('tofu_live_', 'tofu_admin_')):
        return x_api_key
    cookie_tok = (request.cookies.get(SESSION_COOKIE) or '').strip()
    if cookie_tok.startswith(('tofu_live_', 'tofu_admin_')):
        return cookie_tok
    qs_tok = (request.args.get('token') or '').strip()
    if qs_tok.startswith(('tofu_live_', 'tofu_admin_')):
        return qs_tok
    return ''


def _token_source(token: str) -> str:
    """Return which transport carried ``token`` (for diagnostic logging).

    Mirrors the priority order of :func:`_extract_bearer_or_cookie`.
    """
    auth = request.headers.get('Authorization', '') or ''
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer' and parts[1].strip() == token:
        return 'header'
    if (request.headers.get('x-api-key') or '').strip() == token:
        return 'x-api-key'
    if (request.cookies.get(SESSION_COOKIE) or '').strip() == token:
        return 'cookie'
    if (request.args.get('token') or '').strip() == token:
        return 'query'
    return 'unknown'


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
            from routes._bridge_caller import (
                browser_poll_admission_rejection,
            )
            poll_controller = browser_poll_admission()
            poll_decision, poll_lease = poll_controller.enter(
                credential=request.headers.get('X-Bridge-Secret', ''),
                peer=request.remote_addr or '',
                reported_protocol_version=request.headers.get(
                    'X-Browser-Protocol-Version', ''),
            )
            g.browser_poll_admission_lease = poll_lease
            if not poll_decision.allowed:
                return browser_poll_admission_rejection(poll_decision)
        bridge_context = await _bridge_auth_context()
        if bridge_context is not None:
            g.auth_ctx = bridge_context
            g.bridge_auth_context = bridge_context
            try:
                _stamp_principal()
            except (PermissionError, ValueError) as exc:
                return _principal_binding_rejection(exc)
            if path == '/api/browser/poll':
                owner_decision = poll_controller.admit_owner(
                    poll_lease,
                    owner_user_id=bridge_context.owner_user_id,
                )
                if not owner_decision.allowed:
                    return browser_poll_admission_rejection(owner_decision)
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
    # ``lib.rate_limit_api`` / ``lib.idempotency``).
    if not _mode_requires_credential():
        token = _extract_bearer_or_cookie()
        ctx_open: Optional[AuthContext] = None
        if token:
            ctx_open = await _validate_token(token)
        # Synthetic full-admin grant is loopback-only by default. A
        # remote peer in open mode does NOT get the free admin context;
        # it must present a valid credential (resolved above) or it
        # falls through to the private-mode rejection path below. This
        # closes the "bind 0.0.0.0 + open mode = unauthenticated admin"
        # foot-gun. Operators who front the server with their own auth
        # can opt back in via TOFU_OPEN_MODE_ALLOW_REMOTE=1.
        if ctx_open is None:
            if open_mode_peer_allowed(request.remote_addr):
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
                # The open-mode limiter can use the shared DB backend. Keep
                # that optional storage path off the serving loop as well.
                decision = await asyncio.to_thread(check_request, ctx_open)
                g.rate_decision = decision
                if not decision.allowed:
                    return _rate_limit_rejection(decision)
            return None
        # else: fall through to the credential-required gate below.

    is_public = path in _PUBLIC_EXACT

    # 1. Try API keys + cookie + query-string first (single auth model).
    #    We resolve even on public paths so /api/v1/keys/whoami can tell
    #    the caller who they are.
    token = _extract_bearer_or_cookie()
    ctx: Optional[AuthContext] = None
    used_query_token = False
    if token:
        ctx = await _validate_token(token)
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
                              _token_source(token), path,
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
    'request_principal',
    'request_user_id',
    'SESSION_COOKIE',
    'address_is_loopback',
    'open_mode_peer_allowed',
]

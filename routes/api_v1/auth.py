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
  5. ``X-Tunnel-Token`` / cookie       — back-compat shim for legacy
                                          deployments that still set
                                          ``TUNNEL_TOKEN`` (deprecated).

For all five valid paths the resolved context lives at ``g.auth_ctx``.
Routes consult it via :func:`require_auth` / :func:`require_scope`.

Public path policy
------------------
A short allow-list of routes can be reached without a token:

  * ``/``, ``/index.html``, ``/static/*``, ``/favicon.*``, ``/robots.txt``
  * ``/.well-known/*``
  * ``/api/health``                      (liveness probe)
  * ``/api/openapi.json|yaml``,
    ``/api/docs``, ``/api/redoc``        (self-describing surface)
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
every authenticated API request. Cookie-authenticated requests bypass
the bucket (the local UI is the user's own credential). Public paths
never enforce 429.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
from typing import Optional

from flask import jsonify, redirect, request
from quart import Response, g

from lib.api_keys import (
    AuthContext, local_admin_context, touch_key, validate_token,
)
from lib.api_response import api_forbidden, api_unauthorized
from lib.auth_mode import requires_credential as _mode_requires_credential
from lib.log import audit_log, get_logger
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
    '/api/openapi.json',
    '/api/openapi.yaml',
    '/api/docs',
    '/api/redoc',
    '/api/v1/capabilities',
    '/api/v1/keys/whoami',
    '/api/v1/auth/mode',  # GET only; PUT goes through @require_scope('admin')
    '/api/v1/billing/pricing',  # public price card; mutation paths require admin
    '/api/v1/billing/webhooks/stripe',  # auth via signed payload
    '/api/v1/billing/webhooks/alipay',  # auth via RSA2 signature
    '/api/v1/users/signup',    # public registration (gated by relay.json)
    '/api/v1/users/login',     # public login
    '/api/v1/users/logout',    # public; idempotent on missing session
    '/api/v1/users/me',        # public probe; returns {user: null} unauthed
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


def _legacy_tunnel_token_passes() -> bool:
    """Back-compat: honour ``TUNNEL_TOKEN`` if a deployment still sets it.

    Deprecated. New deployments use API keys exclusively. Existing ones
    keep working without immediate migration. The acceptance paths
    mirror the old ``server.py:tunnel_auth`` exactly.
    """
    tt = os.environ.get('TUNNEL_TOKEN', '')
    if not tt:
        return False
    cookie_val = request.cookies.get('_tunnel_auth') or ''
    expected = hashlib.sha256(tt.encode()).hexdigest()[:32]
    if cookie_val == expected:
        return True
    if request.headers.get('X-Tunnel-Token', '') == tt:
        return True
    if request.args.get('token', '') == tt:
        return True
    return False


# \u2500\u2500 Middleware \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


async def auth_before_request():
    """Resolve ``g.auth_ctx`` for every request.

    Sets ``g.auth_ctx`` and ``g.rate_decision``. Returns a Response only
    when the request is rejected (401 / 429 / 401-redirect-with-cookie).
    """
    path = request.path
    g.auth_ctx = None
    g.rate_decision = None

    # Static assets short-circuit before any token work \u2014 they're hit
    # tens of times per page load and should never touch the cache.
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return None
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
            ctx_open = validate_token(token)
            if ctx_open is not None:
                touch_key(ctx_open.key_id)
        if ctx_open is None:
            ctx_open = local_admin_context()
        g.auth_ctx = ctx_open
        return None

    is_public = path in _PUBLIC_EXACT

    # 1. Try API keys + cookie + query-string first (single auth model).
    #    We resolve even on public paths so /api/v1/keys/whoami can tell
    #    the caller who they are.
    token = _extract_bearer_or_cookie()
    ctx: Optional[AuthContext] = None
    used_query_token = False
    if token:
        ctx = validate_token(token)
        if ctx is not None:
            touch_key(ctx.key_id)
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
            return jsonify({
                'ok': False,
                'error': {'kind': 'unauthorized',
                          'detail': 'Invalid or expired API key. If you '
                                    'copied it from '
                                    'data/config/.first_run_token, that '
                                    'token may have been rotated — restart '
                                    'the server to mint a fresh one.'},
            }), 401

    # 2. Back-compat: legacy TUNNEL_TOKEN flow.
    if ctx is None and _legacy_tunnel_token_passes():
        ctx = AuthContext(
            key_id='', name='tunnel', scopes=frozenset({'admin'}),
            rate_limit_rpm=0, rate_limit_tpd=0, via_tunnel_token=True,
        )

    g.auth_ctx = ctx

    # 3. Browser landed on / with ?token=<key>: install cookie + redirect.
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
            return jsonify({
                'ok': False,
                'error': {'kind': 'unauthorized',
                          'detail': 'Authentication required. Send '
                                    'Authorization: Bearer tofu_live_\u2026'},
            }), 401
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
        resp = jsonify({
            'ok': False,
            'error': {'kind': 'rate_limited',
                      'detail': f'Rate limit exceeded ({decision.reason})',
                      'retry_after_s': round(decision.retry_after_s, 2)},
        })
        apply_headers(resp, decision)
        return resp, 429

    # 7. Per-key request counter (tokens recorded post-hoc by routes).
    if ctx.key_id:
        try:
            record_usage(ctx.key_id, request_count=1)
        except Exception as e:
            logger.debug('[Auth] usage record failed: %s', e)
    return None


async def attach_rate_headers(response):
    """After-request hook: copy bucket state onto the outgoing response."""
    try:
        decision = getattr(g, 'rate_decision', None)
        if decision is not None:
            apply_headers(response, decision)
    except Exception as e:
        logger.debug('[Auth] rate-header hook failed: %s', e)
    return response


# \u2500\u2500 Decorators \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def current_auth() -> Optional[AuthContext]:
    """Return the ``AuthContext`` for the current request (or None)."""
    try:
        return getattr(g, 'auth_ctx', None)
    except RuntimeError as e:
        # Working outside of application/request context.
        logger.debug('[Auth] current_auth called outside request ctx: %s', e)
        return None


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


# Legacy aliases used by ``server.py`` and tests during the transition
# from the old name. Both refer to the same callable.
bearer_auth_before_request = auth_before_request


__all__ = [
    'auth_before_request',
    'bearer_auth_before_request',
    'attach_rate_headers',
    'require_auth',
    'require_scope',
    'current_auth',
    'SESSION_COOKIE',
]

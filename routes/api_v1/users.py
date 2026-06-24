"""routes/api_v1/users.py — Tenant user signup / login / admin CRUD.

Routes
------

Public (no auth required):

  POST /api/v1/users/signup     — register a new tenant user
  POST /api/v1/users/login      — exchange email+password for a session
  POST /api/v1/users/logout     — clear the session cookie
  GET  /api/v1/users/me         — current user (works on cookie or token)

Admin-only:

  GET  /api/v1/users            — list users
  GET  /api/v1/users/{id}       — fetch one
  PATCH /api/v1/users/{id}      — update role / status
  POST /api/v1/users/{id}/keys  — mint a key on behalf of a user
                                   (used by the admin console)

Session model
-------------
Login mints a fresh API key with the ``user_id`` field bound to the
authenticated user, and drops it into the ``tofu_session`` cookie via
the standard auth gate. There is NO separate session-cookie store —
the cookie value IS the bearer token, so `/api/v1/keys/whoami`,
rate-limit accounting, billing, and audit logs all work uniformly
whether the caller hit /login or supplied an Authorization header.

Each login produces a new key; ``/logout`` revokes that specific key.
Multiple devices stay independent: signing out on the laptop doesn't
log the phone out. The browser tab's cookie carries one token at a
time; minting a new one (re-login) silently rotates.

Signup gate
-----------
``signup_enabled`` is read from ``data/config/relay.json`` (defaults
to false). A relay operator can leave signup closed and onboard
users via redemption codes (admin mints code → customer redeems on
``/dashboard``) or via direct ``POST /api/v1/users``-by-admin.
"""

from __future__ import annotations

from flask import Blueprint, request

from lib.api_keys import create_key, revoke_key
from lib.api_response import (
    api_bad_request, api_created, api_forbidden, api_not_found, api_ok,
    api_unauthorized,
)
from lib.billing.users import (
    USER_ROLES, authenticate, create_user, get_user, list_users,
    set_user_status, update_user_role,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    optional_list, optional_str, parse_body, require_str,
)

from .auth import (
    SESSION_COOKIE, SESSION_COOKIE_MAX_AGE, current_auth, require_scope,
)

logger = get_logger(__name__)

api_v1_users_bp = Blueprint('api_v1_users', __name__)


# ── Operator-tunable settings (data/config/relay.json) ───────────────

def _relay_settings() -> dict:
    """Read the relay-policy file via the shared :mod:`lib.relay_config`.

    Single source of truth so signup gating, billing gating, and the
    capabilities surface never drift. Returns the merged settings dict
    (defaults ← file ← env).
    """
    from lib.relay_config import get_settings
    return get_settings()


def _user_payload(u) -> dict:
    return {
        'id': u.id,
        'email': u.email,
        'display_name': u.display_name,
        'role': u.role,
        'status': u.status,
        'created_at': u.created_at,
        'last_login_at': u.last_login_at,
        'email_verified': u.email_verified,
    }


# Scopes that grant access to the OPERATOR's model slot pool. Withheld
# from tenant keys in a BYO-only deployment (model_relay_enabled=false)
# so users must attach their own endpoint via agents:run.
_MODEL_RELAY_SCOPES = frozenset({'chat'})


def _session_scopes() -> list[str]:
    """Build the default scope set for a freshly minted tenant key.

    In a BYO-only deployment (``model_relay_enabled=false``) the
    operator's model slot pool is off-limits: ``chat`` is dropped and
    ``agents:run`` is added so the user can still run agents against
    their OWN registered provider.
    """
    from lib.relay_config import model_relay_enabled
    scopes = ['chat', 'tasks', 'conversations', 'files',
               'agents:paper', 'agents:translate', 'agents:memory',
               'agents:browser', 'agents:search', 'agents:image',
               'webhooks', 'capabilities', 'usage']
    if not model_relay_enabled():
        scopes = [s for s in scopes if s not in _MODEL_RELAY_SCOPES]
        # BYO-only users need to register + run against their own model.
        for extra in ('providers', 'agents:run'):
            if extra not in scopes:
                scopes.append(extra)
    return scopes


def _mint_session_key(user) -> tuple[dict, str]:
    """Mint a token bound to ``user`` and return ``(public_row, plaintext)``.

    The plaintext is the cookie value; the row is shown back to the
    caller so they can copy it as a Bearer token if they want
    out-of-browser access from the same login.
    """
    scopes = _session_scopes()
    if user.role == 'admin':
        return create_key(
            name=f'session:{user.email}', scopes=[], admin=True,
            user_id=user.id,
            metadata={'origin': 'login', 'email': user.email})
    return create_key(
        name=f'session:{user.email}', scopes=scopes,
        user_id=user.id,
        metadata={'origin': 'login', 'email': user.email})


def _response_obj(resp):
    """Unwrap the Response from api_ok/api_created's ``(response, status)``
    tuple so we can mutate cookies on it. Accepts a bare Response too."""
    if isinstance(resp, tuple):
        return resp[0]
    return resp


def _set_session_cookie(resp, token: str) -> None:
    _response_obj(resp).set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True, samesite='Lax',
        secure=request.is_secure)


# ── Public routes ────────────────────────────────────────────────────

@api_v1_users_bp.route('/api/v1/users/signup', methods=['POST'])
@api_meta(summary='Create a new tenant user',
          description='Public registration. Disabled by default; the '
                       'operator must set ``signup_enabled: true`` in '
                       '``data/config/relay.json`` first. Returns 403 '
                       'with ``error_kind=signup_disabled`` otherwise.',
          tags=['users'], public=True,
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'type': 'object',
                          'required': ['email', 'password'],
                          'properties': {
                              'email': {'type': 'string'},
                              'password': {'type': 'string', 'minLength': 8},
                              'display_name': {'type': 'string'}}}}}})
def signup_route():
    settings = _relay_settings()
    if not settings.get('signup_enabled'):
        return api_forbidden(
            'Signup is disabled on this relay. Contact the operator '
            'for a redemption code or an invitation.',
            error_kind='signup_disabled')
    body = parse_body()
    email = require_str(body, 'email', max_len=255).strip().lower()
    password = require_str(body, 'password', max_len=255)
    if len(password) < 8:
        return api_bad_request('password must be at least 8 chars',
                                field='password')
    display_name = optional_str(body, 'display_name', default='', max_len=80)
    role = settings.get('signup_default_role', 'user')
    if role not in USER_ROLES:
        role = 'user'
    try:
        user = create_user(email, password=password,
                          display_name=display_name, role=role)
    except ValueError as e:
        return api_bad_request(str(e), field='email')
    # Optional welcome credit so a fresh user can issue test calls.
    welcome = int(settings.get('signup_welcome_credit_micro') or 0)
    if welcome > 0:
        try:
            from lib.billing import deposit
            deposit(user.id, welcome, kind='bonus',
                    ref_type='signup', ref_id=user.id,
                    note='signup welcome credit')
        except Exception as e:
            logger.warning('[Users] welcome credit failed: %s', e)
    # Auto-login on signup.
    row, token = _mint_session_key(user)
    audit_log('user_signup', user_id=user.id, email=email)
    resp = api_created(user=_user_payload(user), token=token,
                        key=row, welcome_credit_micro=welcome)
    _set_session_cookie(resp, token)
    return resp


@api_v1_users_bp.route('/api/v1/users/login', methods=['POST'])
@api_meta(summary='Login with email + password',
          description='Returns a freshly minted token. The same token '
                       'is also installed as the ``tofu_session`` cookie '
                       'so subsequent same-origin requests authenticate '
                       'automatically.',
          tags=['users'], public=True)
def login_route():
    body = parse_body()
    email = require_str(body, 'email', max_len=255).strip().lower()
    password = require_str(body, 'password', max_len=255)
    user = authenticate(email, password)
    if user is None:
        return api_unauthorized('Invalid email or password',
                                error_kind='invalid_credentials')
    row, token = _mint_session_key(user)
    resp = api_ok(user=_user_payload(user), token=token, key=row)
    _set_session_cookie(resp, token)
    return resp


@api_v1_users_bp.route('/api/v1/users/logout', methods=['POST'])
@api_meta(summary='Logout (revoke the session token)',
          description='Revokes the API key backing the current cookie '
                       '/ Bearer header and clears the cookie. Other '
                       'devices logged into the same account stay logged '
                       'in (each device has its own session key).',
          tags=['users'], public=True)
def logout_route():
    ctx = current_auth()
    if ctx is not None and ctx.key_id and not ctx.via_open_mode \
            and not ctx.via_tunnel_token:
        revoke_key(ctx.key_id)
        audit_log('user_logout', user_id=ctx.user_id,
                  key_id=ctx.key_id)
    resp = api_ok(ok=True)
    _response_obj(resp).set_cookie(SESSION_COOKIE, '', expires=0, max_age=0,
                                   httponly=True, samesite='Lax',
                                   secure=request.is_secure)
    return resp


@api_v1_users_bp.route('/api/v1/users/me', methods=['GET'])
@api_meta(summary='Current tenant user',
          description='Returns the user the current session is bound '
                       'to. ``{user: null}`` when unauthenticated or '
                       'when the session\'s key has no ``user_id`` '
                       '(legacy / personal install).',
          tags=['users'], public=True)
def me_route():
    ctx = current_auth()
    if ctx is None or not ctx.is_authenticated:
        return api_ok(authenticated=False, user=None)
    if not getattr(ctx, 'user_id', ''):
        # Bearer token without a tenant binding (personal install,
        # bootstrap admin key, open mode). Still authenticated, but
        # not a "user" in the multi-tenant sense.
        return api_ok(authenticated=True, user=None,
                      principal={'name': ctx.name, 'key_id': ctx.key_id,
                                  'scopes': sorted(ctx.scopes)})
    user = get_user(ctx.user_id)
    if user is None:
        # The user row vanished between the key's creation and now.
        return api_ok(authenticated=True, user=None,
                      principal={'name': ctx.name, 'key_id': ctx.key_id})
    return api_ok(authenticated=True, user=_user_payload(user),
                  principal={'name': ctx.name, 'key_id': ctx.key_id,
                              'scopes': sorted(ctx.scopes)})


# ── Admin routes ─────────────────────────────────────────────────────

@api_v1_users_bp.route('/api/v1/users', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Admin: list tenant users',
          description='Paginated. Filter by ``?status=`` (active / '
                       'suspended / deleted).',
          tags=['users'], scope='admin')
def list_users_route():
    limit = max(1, min(int(request.args.get('limit') or 100), 1000))
    offset = max(0, int(request.args.get('offset') or 0))
    status = (request.args.get('status') or '').strip()
    try:
        rows = list_users(limit=limit, offset=offset, status=status)
    except ValueError as e:
        return api_bad_request(str(e), field='status')
    return api_ok(users=[_user_payload(u) for u in rows],
                  limit=limit, offset=offset)


@api_v1_users_bp.route('/api/v1/users', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Admin: create a tenant user',
          description='Bypasses ``signup_enabled``. Used by the admin '
                       'console to onboard customers directly.',
          tags=['users'], scope='admin')
def admin_create_user_route():
    body = parse_body()
    email = require_str(body, 'email', max_len=255).strip().lower()
    password = optional_str(body, 'password', default='', max_len=255)
    display_name = optional_str(body, 'display_name', default='', max_len=80)
    role = optional_str(body, 'role', default='user', max_len=20)
    if role not in USER_ROLES:
        return api_bad_request(f'Bad role: {role!r}', field='role')
    try:
        user = create_user(email, password=password,
                          display_name=display_name, role=role)
    except ValueError as e:
        return api_bad_request(str(e), field='email')
    return api_created(user=_user_payload(user))


@api_v1_users_bp.route('/api/v1/users/<user_id>', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Admin: fetch one user',
          tags=['users'], scope='admin')
def get_user_route(user_id):
    user = get_user(user_id)
    if user is None:
        return api_not_found('user not found')
    return api_ok(_user_payload(user))


@api_v1_users_bp.route('/api/v1/users/<user_id>', methods=['PATCH'])
@require_scope('admin')
@api_meta(summary='Admin: update user role / status',
          tags=['users'], scope='admin')
def patch_user_route(user_id):
    body = parse_body()
    user = get_user(user_id)
    if user is None:
        return api_not_found('user not found')
    if 'role' in body:
        try:
            user = update_user_role(user_id, body['role'])
        except ValueError as e:
            return api_bad_request(str(e), field='role')
    if 'status' in body:
        try:
            user = set_user_status(user_id, body['status'])
        except ValueError as e:
            return api_bad_request(str(e), field='status')
    return api_ok(_user_payload(user))


@api_v1_users_bp.route('/api/v1/users/<user_id>/keys', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Admin: mint a key on behalf of a user',
          description='Returns the plaintext token ONCE. The user can '
                       'use it as Bearer auth identical to one minted '
                       'via login.',
          tags=['users'], scope='admin')
def mint_user_key_route(user_id):
    user = get_user(user_id)
    if user is None:
        return api_not_found('user not found')
    body = parse_body()
    name = require_str(body, 'name', max_len=80)
    rpm = int(body.get('rate_limit_rpm') or 60)
    tpd = int(body.get('rate_limit_tpd') or 0)
    from lib.relay_config import model_relay_enabled
    scopes = optional_list(body, 'scopes', default=[]) or [
        'chat', 'tasks', 'conversations', 'usage', 'capabilities']
    # BYO-only deployment: never hand out the operator slot-pool scope,
    # even if the admin explicitly listed it.
    if not model_relay_enabled():
        scopes = [s for s in scopes if s not in _MODEL_RELAY_SCOPES]
    row, token = create_key(
        name=name, scopes=scopes,
        rate_limit_rpm=rpm, rate_limit_tpd=tpd,
        user_id=user_id,
        metadata={'origin': 'admin_mint', 'email': user.email})
    return api_created(key=row, token=token)


__all__ = ['api_v1_users_bp']

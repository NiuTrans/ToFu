"""lib/api_keys.py — Bearer-token API key auth.

Single source of truth for who is authorised to call any non-public
route. Personal browsing, headless SDK clients, OpenAI/Anthropic-compat
callers, and the local UI all authenticate the same way: a token
minted here, validated by ``validate_token``, and carried in any of
``Authorization: Bearer …`` / ``x-api-key`` / the ``tofu_session``
cookie.

On first boot the server calls :func:`bootstrap_personal_key` which
mints one ``tofu_admin_…`` key when the store is empty, prints its
plaintext to stderr exactly once, and persists only its hash. That
key backs both the local UI (cookie) and personal SDK use
(env / config file).

``TUNNEL_TOKEN`` is preserved as a deprecated back-compat shim — when
set it acts as a synthetic admin credential so existing deployments
don't break, but new code should not reach for it.

Storage
-------
``data/config/api_keys.json`` via ``lib.json_store`` (atomic, locked):

    {
      "version": 1,
      "keys": [
        {
          "id":            "k_a3f2c1",
          "name":          "build-bot",
          "prefix":        "tofu_live_a3f2c1",   # public, shown in UI
          "secret_hash":   "<sha256-of-full-token>",
          "scopes":        ["chat","tasks","files"],
          "rate_limit_rpm":  60,
          "rate_limit_tpd":  1000000,
          "created_at":    1701000000.0,
          "last_used_at":  1701002345.0,
          "expires_at":    null,
          "disabled":      false,
          "metadata":      {"created_by":"alice","note":"CI runner"}
        }
      ]
    }

Tokens are shaped ``tofu_live_<32hex>`` or ``tofu_admin_<32hex>``. Only
``prefix`` is stored in the clear; the full token is shown ONCE at
creation, then only its SHA-256 hash is persisted.

Scopes
------
Caller supplies a token in ``Authorization: Bearer tofu_…``. The
``require_scope(scope)`` decorator (see ``routes/api_v1/_auth.py``) gates
each route. The full scope vocabulary is the closed enum in
``ALL_SCOPES`` below; ``admin`` implicitly grants every other scope.

Public API
----------
  list_keys()                         → list[dict] (no secrets)
  get_key_by_id(key_id)               → dict | None
  validate_token(token)               → AuthContext | None
  create_key(name, scopes, ...)       → (dict, plaintext_token)
  revoke_key(key_id)                  → bool
  update_key(key_id, **fields)        → bool
  touch_key(key_id)                   → records last_used_at
  ALL_SCOPES                          → frozenset of scope strings
  AuthContext                         → result of validate_token()
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# ── Storage ────────────────────────────────────────────────────────

_STORE_PATH = config_path('api_keys.json')
_STORE_VERSION = 1

# Closed scope vocabulary. Adding a new scope must always go through
# this list — a route asking for an unknown scope is a programming bug
# and ``require_scope`` raises at registration time.
ALL_SCOPES = frozenset({
    'chat',           # /api/v1/chat/completions, /v1/chat/completions
    'tasks',          # /api/v1/tasks/* (start, poll, abort)
    'conversations',  # /api/v1/conversations/*
    'files',          # /api/v1/files/* (uploads, attachments)
    'providers',      # /api/v1/providers/* (BYO model endpoint CRUD)
    'agents:paper',
    'agents:translate',
    'agents:swarm',
    'agents:scheduler',
    'agents:memory',
    'agents:browser',
    'agents:search',
    'agents:trading',
    'agents:image',
    'agents:mcp',
    'agents:run',     # /api/v1/agent/run (single-call agent runtime)
    'webhooks',       # /api/v1/webhooks/*
    'capabilities',   # /api/v1/capabilities (read-only, public-by-default)
    'usage',          # /api/v1/usage (per-key analytics)
    'admin',          # full surface, includes /api/v1/keys/*
})

# 'admin' implicitly grants every scope below it.
_ADMIN_SCOPE = 'admin'

# ── In-memory cache (loaded once, refreshed on mutation) ──────────
_cache_lock = threading.RLock()
_cache: list[dict] = []
_cache_loaded = False


@dataclass
class AuthContext:
    """Resolved auth state for the current request.

    ``key_id`` and ``scopes`` are populated when a valid Bearer token
    matches a row in ``api_keys.json``. ``via_tunnel_token`` is True
    when the request authenticated via ``TUNNEL_TOKEN`` (cookie/header)
    — those requests have implicit full ``admin`` scope (matches the UI's
    historical privilege level).

    ``via_open_mode`` is True for requests that came through the auth
    gate while ``lib.auth_mode`` reports ``mode=open`` — there is no
    real credential, but downstream ``require_scope`` decorators must
    keep working uniformly, so the synthetic context is tagged with
    full admin scope (see :func:`local_admin_context`).
    """
    key_id: str = ''
    name: str = ''
    scopes: frozenset = field(default_factory=frozenset)
    rate_limit_rpm: int = 0
    rate_limit_tpd: int = 0
    via_tunnel_token: bool = False
    via_open_mode: bool = False
    user_id: str = ''  # owning user (multi-user mode); '' for legacy/personal keys

    def has_scope(self, scope: str) -> bool:
        if self.via_tunnel_token or self.via_open_mode:
            return True
        if _ADMIN_SCOPE in self.scopes:
            return True
        return scope in self.scopes

    @property
    def is_authenticated(self) -> bool:
        return bool(self.key_id) or self.via_tunnel_token or self.via_open_mode


def local_admin_context() -> 'AuthContext':
    """Synthetic full-privilege context for ``mode=open`` requests.

    The key_id is the literal ``'local'`` so usage tracking, idempotency
    keys, and audit logs share one stable principal across an open-mode
    deployment without needing a real row in ``api_keys.json``.
    """
    return AuthContext(
        key_id='local', name='local',
        scopes=frozenset({_ADMIN_SCOPE}),
        rate_limit_rpm=0, rate_limit_tpd=0,
        via_tunnel_token=False, via_open_mode=True,
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _public_view(row: dict) -> dict:
    """Return a row dict with the secret hash redacted."""
    out = dict(row)
    out.pop('secret_hash', None)
    return out


def _ensure_loaded() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    with _cache_lock:
        if _cache_loaded:
            return
        store = read_json(_STORE_PATH, default=None)
        keys: list[dict] = []
        if isinstance(store, dict) and isinstance(store.get('keys'), list):
            keys = [k for k in store['keys'] if isinstance(k, dict)]
        _cache.clear()
        _cache.extend(keys)
        _cache_loaded = True
        logger.info('[ApiKeys] loaded %d key(s) from %s', len(_cache), _STORE_PATH)


def _persist() -> None:
    """Write the cache back to disk atomically."""
    payload = {'version': _STORE_VERSION, 'keys': list(_cache)}
    def _mutator(_):
        return payload
    update_json_atomic(_STORE_PATH, _mutator, default=payload)


def list_keys() -> list[dict]:
    """Return all keys (without secret hashes), newest first."""
    _ensure_loaded()
    with _cache_lock:
        rows = sorted(_cache, key=lambda r: r.get('created_at', 0), reverse=True)
        return [_public_view(r) for r in rows]


def get_key_by_id(key_id: str) -> Optional[dict]:
    _ensure_loaded()
    with _cache_lock:
        for row in _cache:
            if row.get('id') == key_id:
                return _public_view(row)
    return None


def _normalise_scopes(scopes: list) -> frozenset:
    cleaned = set()
    for s in scopes or ():
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s:
            continue
        if s == _ADMIN_SCOPE or s in ALL_SCOPES:
            cleaned.add(s)
        else:
            logger.warning('[ApiKeys] dropping unknown scope %r', s)
    return frozenset(cleaned)


def create_key(name: str, *, scopes: list, rate_limit_rpm: int = 60,
               rate_limit_tpd: int = 0,
               expires_at: Optional[float] = None,
               metadata: Optional[dict] = None,
               admin: bool = False,
               user_id: str = '') -> tuple[dict, str]:
    """Mint a new API key. Returns ``(public_row, plaintext_token)``.

    The plaintext token is shown ONCE — the caller (route handler) is
    responsible for delivering it to the user. Only its SHA-256 hash
    is persisted.

    ``admin=True`` produces a token shaped ``tofu_admin_…`` and forces
    the ``admin`` scope. Otherwise the token is ``tofu_live_…``.
    """
    _ensure_loaded()
    if not name or not isinstance(name, str):
        raise ValueError('name required')
    name = name.strip()[:80]
    scope_set = _normalise_scopes(scopes)
    if admin:
        scope_set = scope_set | {_ADMIN_SCOPE}
    if not scope_set:
        raise ValueError('at least one scope required')

    token_kind = 'admin' if _ADMIN_SCOPE in scope_set else 'live'
    secret = secrets.token_hex(16)
    plaintext = f'tofu_{token_kind}_{secret}'
    prefix_len = len('tofu_xxxx_') + 6  # enough to identify in UI
    prefix = plaintext[:prefix_len]
    key_id = 'k_' + secrets.token_hex(4)
    row = {
        'id': key_id,
        'name': name,
        'prefix': prefix,
        'secret_hash': _hash_token(plaintext),
        'scopes': sorted(scope_set),
        'rate_limit_rpm': max(0, int(rate_limit_rpm or 0)),
        'rate_limit_tpd': max(0, int(rate_limit_tpd or 0)),
        'created_at': time.time(),
        'last_used_at': None,
        'expires_at': float(expires_at) if expires_at else None,
        'disabled': False,
        'metadata': dict(metadata or {}),
        'user_id': str(user_id or ''),
    }
    with _cache_lock:
        _cache.append(row)
        _persist()
    audit_log('api_key_created', key_id=key_id, name=name,
              scopes=sorted(scope_set), rpm=row['rate_limit_rpm'],
              tpd=row['rate_limit_tpd'], admin=admin)
    logger.info('[ApiKeys] created %s name=%r scopes=%s', key_id, name,
                sorted(scope_set))
    return _public_view(row), plaintext


def revoke_key(key_id: str) -> bool:
    _ensure_loaded()
    with _cache_lock:
        for i, row in enumerate(_cache):
            if row.get('id') == key_id:
                is_bootstrap = (
                    (row.get('metadata') or {}).get('origin')
                    == 'bootstrap_personal_key'
                )
                _cache.pop(i)
                _persist()
                audit_log('api_key_revoked', key_id=key_id,
                          name=row.get('name', ''))
                logger.info('[ApiKeys] revoked %s', key_id)
                # The first-run emergency token is a one-shot copy of
                # the bootstrap key only. Once that key is gone the file
                # is a dead reference, so clear it to avoid handing the
                # user a phantom token.
                if is_bootstrap:
                    _clear_first_run_token('bootstrap key revoked')
                return True
    return False


_UPDATABLE = frozenset({'name', 'scopes', 'rate_limit_rpm',
                         'rate_limit_tpd', 'expires_at', 'disabled',
                         'metadata', 'user_id'})


def update_key(key_id: str, **fields) -> bool:
    """Update an existing key in place.

    NOTE: the ``admin`` scope is NOT grantable through this path. A key's
    privilege tier is fixed at mint time and reflected in its token prefix
    (``tofu_admin_`` vs ``tofu_live_``); letting PATCH add ``admin`` would
    leave a ``tofu_live_`` token silently wielding full privileges. Any
    ``admin`` entry in an incoming ``scopes`` list is dropped here (a
    warning is logged); the key keeps its existing admin-ness, which is
    only ever set by :func:`create_key` with ``admin=True``. To change a
    key's tier, revoke and re-mint.
    """
    _ensure_loaded()
    with _cache_lock:
        for row in _cache:
            if row.get('id') != key_id:
                continue
            had_admin = _ADMIN_SCOPE in (row.get('scopes') or ())
            changed = {}
            for k, v in fields.items():
                if k not in _UPDATABLE:
                    continue
                if k == 'scopes':
                    new_scopes = set(_normalise_scopes(v))
                    requested_admin = _ADMIN_SCOPE in new_scopes
                    if requested_admin and not had_admin:
                        logger.warning('[ApiKeys] refusing to grant admin '
                                       'scope via update_key on %s; revoke '
                                       'and re-mint to change tier', key_id)
                    # Preserve the key's existing admin-ness, never flip it.
                    if had_admin:
                        new_scopes.add(_ADMIN_SCOPE)
                    else:
                        new_scopes.discard(_ADMIN_SCOPE)
                    v = sorted(new_scopes)
                if k in ('rate_limit_rpm', 'rate_limit_tpd'):
                    v = max(0, int(v or 0))
                if k == 'disabled':
                    v = bool(v)
                if k == 'name' and isinstance(v, str):
                    v = v.strip()[:80]
                if k == 'metadata' and not isinstance(v, dict):
                    continue
                if row.get(k) != v:
                    row[k] = v
                    changed[k] = v
            if changed:
                _persist()
                audit_log('api_key_updated', key_id=key_id, fields=changed)
                logger.info('[ApiKeys] updated %s fields=%s', key_id,
                            list(changed))
            return True
    return False


def validate_token(token: str) -> Optional[AuthContext]:
    """Look up a Bearer token. Returns ``AuthContext`` or None.

    Returns None for: empty / wrong-shape token, unknown hash, disabled
    row, expired row.
    """
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token.startswith(('tofu_live_', 'tofu_admin_')):
        return None
    _ensure_loaded()
    h = _hash_token(token)
    now = time.time()
    with _cache_lock:
        for row in _cache:
            # Constant-time compare so a timing side-channel can't reveal
            # how many leading hex chars of the stored hash matched.
            if not hmac.compare_digest(str(row.get('secret_hash') or ''), h):
                continue
            if row.get('disabled'):
                logger.info('[ApiKeys] token rejected (disabled) %s',
                            row.get('id'))
                return None
            exp = row.get('expires_at')
            if exp and exp <= now:
                logger.info('[ApiKeys] token rejected (expired) %s',
                            row.get('id'))
                return None
            scopes = frozenset(row.get('scopes') or ())
            return AuthContext(
                key_id=row.get('id', ''),
                name=row.get('name', ''),
                scopes=scopes,
                rate_limit_rpm=int(row.get('rate_limit_rpm') or 0),
                rate_limit_tpd=int(row.get('rate_limit_tpd') or 0),
                via_tunnel_token=False,
                user_id=str(row.get('user_id') or ''),
            )
    return None


def touch_key(key_id: str) -> None:
    """Record ``last_used_at = now`` for a key. Cheap, fire-and-forget."""
    if not key_id:
        return
    _ensure_loaded()
    with _cache_lock:
        for row in _cache:
            if row.get('id') == key_id:
                row['last_used_at'] = time.time()
                # Don't fsync on every request — periodic rewrites only.
                # Persistence happens via _persist() on next mutation, or
                # via the cleanup hook (TODO if last_used_at drift becomes
                # a problem in practice).
                return


# ── First-boot personal key bootstrap ─────────────────────────────

# Plaintext is dropped here once on first boot so the user (or a
# launcher script) can recover it without grepping the boot log.
# 0600 perms, lives next to api_keys.json so a project-clone never
# inherits the host's key (export.py excludes data/config/* anyway).
_FIRST_RUN_TOKEN_FILE = config_path('.first_run_token')


def _clear_first_run_token(reason: str) -> None:
    """Delete the first-run emergency token file, ignoring absence.

    Called when the bootstrap key it mirrors is revoked, or at startup
    when the persisted token no longer validates (the matched key was
    revoked/replaced while the process was down).
    """
    try:
        os.unlink(_FIRST_RUN_TOKEN_FILE)
    except FileNotFoundError:
        logger.debug('[Auth] .first_run_token already absent (%s)', reason)
        return
    except OSError as e:
        logger.debug('[Auth] could not remove .first_run_token: %s', e)
        return
    logger.warning('[Auth] Stale .first_run_token detected and removed (%s)',
                   reason)


def _purge_stale_first_run_token() -> None:
    """On startup, drop ``.first_run_token`` if its contents no longer auth.

    The file is a one-shot copy of the bootstrap key. If that key was
    revoked or rotated, the on-disk token is a misleading dead reference
    — validating it returns None. Remove it so the next boot starts clean.
    """
    try:
        with open(_FIRST_RUN_TOKEN_FILE, 'r', encoding='utf-8') as fh:
            token = fh.read().strip()
    except FileNotFoundError:
        logger.debug('[Auth] no .first_run_token to purge')
        return
    except OSError as e:
        logger.debug('[Auth] could not read .first_run_token: %s', e)
        return
    if token and validate_token(token) is not None:
        return
    _clear_first_run_token('matched key was revoked')


def has_any_key() -> bool:
    """True iff at least one key is persisted (loaded on first call)."""
    _ensure_loaded()
    with _cache_lock:
        return bool(_cache)


def bootstrap_personal_key(*, name: str = 'personal') -> Optional[str]:
    """Mint a personal admin key when the key store is empty.

    Returns the plaintext token if a new key was minted, ``None``
    otherwise. Idempotent — calling twice mints exactly once.

    Designed to be called from ``server.py`` startup. The plaintext
    is persisted (0600) at ``data/config/.first_run_token`` so users
    who miss the boot banner can recover their token. Delete the file
    after copying.
    """
    _ensure_loaded()
    # Self-heal any stale first-run token left over from a key rotation
    # that happened while the process was down (the file is a one-shot
    # copy of the bootstrap key, never updated by revoke/create).
    _purge_stale_first_run_token()
    with _cache_lock:
        if _cache:
            return None
    try:
        row, plaintext = create_key(
            name=name, scopes=[], admin=True,
            metadata={'origin': 'bootstrap_personal_key'},
        )
    except Exception as e:
        logger.warning('[ApiKeys] bootstrap failed: %s', e, exc_info=True)
        return None
    # Best-effort write of the plaintext token alongside the JSON store.
    # File is 0600. Failures are non-fatal — the boot banner still has
    # the value.
    try:
        _path = _FIRST_RUN_TOKEN_FILE
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        _flag = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        _fd = os.open(_path, _flag, 0o600)
        try:
            os.write(_fd, (plaintext + '\n').encode('utf-8'))
        finally:
            os.close(_fd)
    except OSError as e:
        logger.debug('[ApiKeys] could not persist first-run token: %s', e)
    audit_log('api_key_bootstrap', key_id=row['id'], name=name)
    logger.info('[ApiKeys] bootstrapped personal admin key %s (name=%r)',
                row['id'], name)
    return plaintext


__all__ = [
    'AuthContext', 'ALL_SCOPES', 'local_admin_context',
    'list_keys', 'get_key_by_id', 'validate_token',
    'create_key', 'revoke_key', 'update_key', 'touch_key',
    'has_any_key', 'bootstrap_personal_key',
    '_purge_stale_first_run_token',
]

"""lib/api_keys/_crud.py — Key issuance, mutation, and lookup.

Read + write operations over the shared cache: :func:`list_keys`,
:func:`get_key_by_id`, :func:`create_key`, :func:`revoke_key`,
:func:`update_key`, :func:`touch_key`.

All mutations go through the single ``_cache`` / ``_cache_lock`` owned by
:mod:`lib.api_keys._store` and persist via its :func:`_persist`.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

from lib.log import audit_log, get_logger

from ._context import _ADMIN_SCOPE, _normalise_scopes
from ._store import (
    _cache,
    _cache_lock,
    _ensure_loaded,
    _hash_token,
    _persist,
    _public_view,
)

logger = get_logger(__name__)


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
                    # Lazy import avoids a package-init import cycle
                    # (_firstrun depends on _crud.create_key).
                    from ._firstrun import _clear_first_run_token
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

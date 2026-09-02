"""Bearer-credential service backed exclusively by semantic Sidecar calls."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Optional

from lib.identity import require_user_id
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client

from ._context import _ADMIN_SCOPE, _normalise_scopes

logger = get_logger(__name__)

_UPDATABLE = frozenset({
    'name', 'scopes', 'rate_limit_rpm', 'rate_limit_tpd', 'expires_at',
    'disabled', 'metadata',
})


def _boundary(owner_user_id, tenant_id: str | None) -> dict:
    return {
        'owner_user_id': require_user_id(
            owner_user_id, context='credential owner'),
        'tenant_id': str(tenant_id or '').strip(),
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def list_keys(*, owner_user_id: int, tenant_id: str | None = None) -> list[dict]:
    """List credentials owned by one explicit repository principal."""
    return list(get_storage_client().query(
        'credential.list', _boundary(owner_user_id, tenant_id)))


def get_key_by_id(
    key_id: str,
    *,
    owner_user_id: int,
    tenant_id: str | None = None,
) -> Optional[dict]:
    return get_storage_client().query('credential.get', {
        **_boundary(owner_user_id, tenant_id),
        'credential_id': str(key_id or ''),
    })


def _mint_key(
    name: str,
    *,
    storage_operation: str,
    scopes: list,
    owner_user_id: int,
    account_user_id: str = '',
    tenant_id: str | None = None,
    rate_limit_rpm: int = 60,
    rate_limit_tpd: int = 0,
    expires_at: Optional[float] = None,
    metadata: Optional[dict] = None,
    admin: bool = False,
) -> tuple[dict | None, str]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError('name required')
    normalized_name = name.strip()[:80]
    scope_set = _normalise_scopes(scopes)
    if admin:
        scope_set = scope_set | {_ADMIN_SCOPE}
    if not scope_set:
        raise ValueError('at least one scope required')
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError('metadata must be an object')
    try:
        rpm = max(0, int(rate_limit_rpm or 0))
        tpd = max(0, int(rate_limit_tpd or 0))
        expiration = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError('credential limits and expiry must be numeric') from exc
    token_kind = 'admin' if _ADMIN_SCOPE in scope_set else 'live'
    plaintext = f'tofu_{token_kind}_{secrets.token_hex(16)}'
    prefix = plaintext[:len('tofu_xxxx_') + 6]
    key_id = 'k_' + secrets.token_hex(8)
    if storage_operation not in {
        'credential.create', 'credential.create_if_owner_empty',
    }:
        raise ValueError('unsupported credential creation operation')
    row = get_storage_client(write=True).command(
        storage_operation, {
            **_boundary(owner_user_id, tenant_id),
            'credential_id': key_id,
            'account_user_id': str(account_user_id or '').strip(),
            'name': normalized_name,
            'prefix': prefix,
            'secret_hash': _hash_token(plaintext),
            'scopes': sorted(scope_set),
            'rate_limit_rpm': rpm,
            'rate_limit_tpd': tpd,
            'created_at': time.time(),
            'expires_at': expiration,
            'metadata': dict(metadata or {}),
        },
        (f'credential.create:{key_id}'
         if storage_operation == 'credential.create' else None),
    )
    if row is None:
        return None, ''
    audit_log(
        'api_key_created', key_id=key_id, name=normalized_name,
        owner_user_id=row['owner_user_id'], scopes=sorted(scope_set),
        rpm=rpm, tpd=tpd, admin=admin,
    )
    logger.info('[ApiKeys] created %s name=%r owner=%s scopes=%s',
                key_id, normalized_name, row['owner_user_id'],
                sorted(scope_set))
    return row, plaintext


def create_key(
    name: str,
    *,
    scopes: list,
    owner_user_id: int,
    account_user_id: str = '',
    tenant_id: str | None = None,
    rate_limit_rpm: int = 60,
    rate_limit_tpd: int = 0,
    expires_at: Optional[float] = None,
    metadata: Optional[dict] = None,
    admin: bool = False,
) -> tuple[dict, str]:
    """Mint one credential; plaintext is returned once and never persisted."""
    row, plaintext = _mint_key(
        name,
        storage_operation='credential.create',
        scopes=scopes,
        owner_user_id=owner_user_id,
        account_user_id=account_user_id,
        tenant_id=tenant_id,
        rate_limit_rpm=rate_limit_rpm,
        rate_limit_tpd=rate_limit_tpd,
        expires_at=expires_at,
        metadata=metadata,
        admin=admin,
    )
    assert row is not None
    return row, plaintext


def create_first_owner_key(
    name: str,
    *,
    owner_user_id: int,
    tenant_id: str | None = None,
    metadata: Optional[dict] = None,
) -> tuple[dict, str] | None:
    """Atomically mint the bootstrap admin key only when its owner has none."""
    row, plaintext = _mint_key(
        name,
        storage_operation='credential.create_if_owner_empty',
        scopes=[],
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        metadata=metadata,
        admin=True,
    )
    return None if row is None else (row, plaintext)


def revoke_key(
    key_id: str,
    *,
    owner_user_id: int,
    tenant_id: str | None = None,
) -> bool:
    result = get_storage_client(write=True).command(
        'credential.revoke', {
            **_boundary(owner_user_id, tenant_id),
            'credential_id': str(key_id or ''),
            'revoked_at': time.time(),
        },
        f'credential.revoke:{require_user_id(owner_user_id)}:{key_id}:'
        f'{secrets.token_hex(8)}',
    )
    if not result['revoked']:
        return False
    audit_log('api_key_revoked', key_id=key_id, owner_user_id=owner_user_id)
    logger.info('[ApiKeys] revoked %s owner=%s', key_id, owner_user_id)
    if result['metadata'].get('origin') == 'bootstrap_personal_key':
        from ._firstrun import _clear_first_run_token
        _clear_first_run_token('bootstrap key revoked')
    return True


def update_key(
    key_id: str,
    *,
    owner_user_id: int,
    tenant_id: str | None = None,
    **fields,
) -> bool:
    existing = get_key_by_id(
        key_id, owner_user_id=owner_user_id, tenant_id=tenant_id)
    if existing is None:
        return False
    updates = {}
    had_admin = _ADMIN_SCOPE in (existing.get('scopes') or ())
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        if key == 'scopes':
            scopes = set(_normalise_scopes(value))
            if had_admin:
                scopes.add(_ADMIN_SCOPE)
            else:
                scopes.discard(_ADMIN_SCOPE)
            if not scopes:
                raise ValueError('at least one scope required')
            value = sorted(scopes)
        elif key in {'rate_limit_rpm', 'rate_limit_tpd'}:
            value = max(0, int(value or 0))
        elif key == 'disabled':
            value = bool(value)
        elif key == 'name':
            if not isinstance(value, str) or not value.strip():
                raise ValueError('name required')
            value = value.strip()[:80]
        elif key == 'metadata':
            if not isinstance(value, dict):
                raise ValueError('metadata must be an object')
            value = dict(value)
        elif key == 'expires_at' and value is not None:
            value = float(value)
        if existing.get(key) != value:
            updates[key] = value
    if not updates:
        return True
    result = get_storage_client(write=True).command(
        'credential.update', {
            **_boundary(owner_user_id, tenant_id),
            'credential_id': key_id,
            'updates': updates,
        },
        f'credential.update:{key_id}:{secrets.token_hex(8)}',
    )
    if result is None:
        return False
    audit_log('api_key_updated', key_id=key_id, fields=updates,
              owner_user_id=owner_user_id)
    return True


__all__ = [
    '_UPDATABLE', '_hash_token', 'create_first_owner_key', 'create_key',
    'get_key_by_id', 'list_keys', 'revoke_key', 'update_key',
]

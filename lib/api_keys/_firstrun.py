"""Personal-owner credential bootstrap and one-time recovery-token file."""

from __future__ import annotations

import os
from typing import Optional

from lib.identity import PERSONAL_USER_ID
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client

from ._crud import create_first_owner_key
from ._validate import validate_token

logger = get_logger(__name__)


def _token_file() -> str:
    import lib.api_keys as package
    return package._FIRST_RUN_TOKEN_FILE


def _clear_first_run_token(reason: str) -> None:
    try:
        os.unlink(_token_file())
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug('[Auth] could not remove .first_run_token: %s', exc)
        return
    logger.warning('[Auth] stale .first_run_token removed (%s)', reason)


def _purge_stale_first_run_token() -> None:
    try:
        with open(_token_file(), 'r', encoding='utf-8') as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug('[Auth] could not read .first_run_token: %s', exc)
        return
    if token and validate_token(token) is not None:
        return
    _clear_first_run_token('credential no longer authenticates')


def has_any_key() -> bool:
    """Whether the personal owner already has a credential."""
    result = get_storage_client().query('credential.exists', {
        'owner_user_id': PERSONAL_USER_ID,
        'tenant_id': '',
    })
    return bool(result['exists'])


def bootstrap_personal_key(*, name: str = 'personal') -> Optional[str]:
    """Atomically create the personal admin credential at most once."""
    _purge_stale_first_run_token()
    created = create_first_owner_key(
        name,
        owner_user_id=PERSONAL_USER_ID,
        metadata={'origin': 'bootstrap_personal_key'},
    )
    if created is None:
        return None
    row, plaintext = created
    try:
        path = _token_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, (plaintext + '\n').encode('utf-8'))
        finally:
            os.close(descriptor)
    except OSError as exc:
        logger.debug('[ApiKeys] could not persist first-run token: %s', exc)
    audit_log('api_key_bootstrap', key_id=row['id'], name=name)
    logger.info('[ApiKeys] bootstrapped personal admin key %s', row['id'])
    return plaintext


__all__ = [
    '_clear_first_run_token', '_purge_stale_first_run_token',
    'bootstrap_personal_key', 'has_any_key',
]

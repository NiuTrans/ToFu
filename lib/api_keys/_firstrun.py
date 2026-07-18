"""lib/api_keys/_firstrun.py — First-boot personal-key bootstrap.

Mints the initial ``tofu_admin_…`` key when the store is empty
(:func:`bootstrap_personal_key`), and manages the one-shot
``.first_run_token`` emergency copy (:func:`_clear_first_run_token`,
:func:`_purge_stale_first_run_token`). :func:`has_any_key` reports
whether any key is persisted.

``_FIRST_RUN_TOKEN_FILE`` lives on the package facade (``lib.api_keys``)
so tests can patch it; every reference below resolves it through the
facade at call time.
"""

from __future__ import annotations

import os
from typing import Optional

from lib.log import audit_log, get_logger

from ._crud import create_key
from ._store import _cache, _cache_lock, _ensure_loaded
from ._validate import validate_token

logger = get_logger(__name__)


def _token_file() -> str:
    """Current ``.first_run_token`` path, read from the facade at call time."""
    import lib.api_keys as pkg
    return pkg._FIRST_RUN_TOKEN_FILE


def _clear_first_run_token(reason: str) -> None:
    """Delete the first-run emergency token file, ignoring absence.

    Called when the bootstrap key it mirrors is revoked, or at startup
    when the persisted token no longer validates (the matched key was
    revoked/replaced while the process was down).
    """
    try:
        os.unlink(_token_file())
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
        with open(_token_file(), 'r', encoding='utf-8') as fh:
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
        _path = _token_file()
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

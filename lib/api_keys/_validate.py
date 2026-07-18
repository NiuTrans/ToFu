"""lib/api_keys/_validate.py — Bearer-token validation.

:func:`validate_token` looks up a presented token against the shared
cache and returns a resolved :class:`AuthContext` or ``None``.
"""

from __future__ import annotations

import hmac
import time
from typing import Optional

from lib.log import get_logger

from ._context import AuthContext
from ._store import _cache, _cache_lock, _ensure_loaded, _hash_token

logger = get_logger(__name__)


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

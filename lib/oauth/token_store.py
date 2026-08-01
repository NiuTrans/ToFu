"""lib/oauth/token_store.py — Persistent token storage for OAuth credentials.

Tokens are stored in data/config/oauth/<provider>.json.
"""

import hashlib
import json
import os
import threading
import time

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['load_token', 'save_token', 'delete_token', 'token_path',
           'OAuthExchangeError', 'refresh_singleflight']


# ══════════════════════════════════════════════════════════
#  Refresh singleflight (S2 — CLIProxyAPI codexRefreshGroup parity)
# ══════════════════════════════════════════════════════════
# Provider refresh tokens are SINGLE-USE. Two concurrent callers that both
# see "token expiring" and both refresh will have the SECOND refresh burn
# the FIRST refresh's freshly-issued refresh_token → refresh_token_reused →
# the subscription is force-logged-out. Desktop-egress latency (1-2s agent
# RTT vs ~300ms direct) widens that race window 4-6×, so concurrent
# refreshes of the SAME refresh token are merged here: the winner calls
# upstream, the waiters reuse its result.
_sf_locks: dict = {}
_sf_guard = threading.Lock()


def _sf_lock(provider: str, refresh_tok: str) -> threading.Lock:
    fp = hashlib.sha256(f'{provider}:{refresh_tok}'.encode()).hexdigest()[:16]
    with _sf_guard:
        return _sf_locks.setdefault(fp, threading.Lock())


def refresh_singleflight(provider: str, refresh_tok: str, fn, load=None):
    """Serialize + merge concurrent refreshes of one refresh token.

    ``fn(refresh_tok)`` performs the actual upstream refresh (and persists).
    ``load()`` re-reads the stored token; when a concurrent refresh has
    already replaced ``refresh_tok`` with a fresh, unexpired token, the
    waiter returns THAT instead of firing a second upstream call.
    """
    lock = _sf_lock(provider, refresh_tok)
    with lock:
        if load is not None:
            try:
                current = load() or {}
            except Exception as e:
                logger.debug('[TokenStore] singleflight reload failed: %s', e)
                current = {}
            cur_rt = current.get('refresh_token') or ''
            if (cur_rt and cur_rt != refresh_tok
                    and (current.get('expire') or 0) > time.time() + 60):
                logger.info('[TokenStore] %s refresh merged — reusing '
                            'concurrent result', provider)
                return current
        return fn(refresh_tok)


class OAuthExchangeError(Exception):
    """Raised when an OAuth token exchange/refresh fails upstream.

    Carries the real HTTP status and a human-readable detail so the route
    layer can surface the ACTUAL upstream reason (e.g. a 403 geo-block)
    instead of a generic "code may have expired" message.
    """

    def __init__(self, message: str, *, status_code: int = 0, detail: str = ''):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or message


def token_path(provider: str) -> str:
    """Return the file path for a provider's token store."""
    return _config_path(os.path.join('oauth', f'{provider}.json'))


def load_token(provider: str) -> dict | None:
    """Load stored OAuth token for a provider.

    Returns:
        Token dict or None if not found / invalid.
    """
    path = token_path(provider)
    try:
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning('[TokenStore] Invalid token file for %s (not a dict)', provider)
            return None
        logger.debug('[TokenStore] Loaded token for %s (email=%s)',
                     provider, data.get('email', '?'))
        return data
    except Exception as e:
        logger.warning('[TokenStore] Failed to load token for %s: %s', provider, e)
        return None


def save_token(provider: str, token_data: dict) -> bool:
    """Save OAuth token data for a provider.

    Args:
        provider: Provider name ('claude' or 'codex').
        token_data: Token dict to persist.

    Returns:
        True on success.
    """
    path = token_path(provider)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        token_data['_saved_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with open(path, 'w') as f:
            json.dump(token_data, f, indent=2, ensure_ascii=False)
        logger.info('[TokenStore] Saved token for %s (email=%s)',
                    provider, token_data.get('email', '?'))
        return True
    except Exception as e:
        logger.error('[TokenStore] Failed to save token for %s: %s', provider, e, exc_info=True)
        return False


def delete_token(provider: str) -> bool:
    """Delete stored OAuth token for a provider."""
    path = token_path(provider)
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info('[TokenStore] Deleted token for %s', provider)
        return True
    except Exception as e:
        logger.warning('[TokenStore] Failed to delete token for %s: %s', provider, e)
        return False

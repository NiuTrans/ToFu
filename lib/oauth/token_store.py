"""lib/oauth/token_store.py — Persistent token storage for OAuth credentials.

Tokens are stored in data/config/oauth/<provider>.json.
"""

import hashlib
import math
import os
import re
import threading
import time

from lib.config_dir import config_path as _config_path
from lib.json_store import locked_path, read_json, write_json_atomic
from lib.log import get_logger
from lib.weak_lock_pool import WeakLockPool

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
# Refresh tokens rotate, so retaining one lock for every historical token is a
# process-lifetime leak. Active holders/waiters keep their lock strongly alive;
# the shared weak pool reclaims it as soon as that refresh generation is idle.
_sf_locks = WeakLockPool(threading.Lock)
_store_locks: dict[str, threading.RLock] = {}
_store_guard = threading.Lock()
_PROVIDER_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_SUPPORTED_PROVIDERS = frozenset({'claude', 'codex'})


def _sf_lock(provider: str, refresh_tok: str) -> threading.Lock:
    fp = hashlib.sha256(f'{provider}:{refresh_tok}'.encode()).hexdigest()[:16]
    return _sf_locks.lock_for(fp)


def _store_lock(provider: str) -> threading.RLock:
    with _store_guard:
        return _store_locks.setdefault(provider, threading.RLock())


def refresh_singleflight(provider: str, refresh_tok: str, fn, load=None,
                         *, lock_path: str = ''):
    """Serialize + merge concurrent refreshes of one refresh token.

    ``fn(refresh_tok)`` performs the actual upstream refresh (and persists).
    ``load()`` re-reads the stored token; when a concurrent refresh has
    already replaced ``refresh_tok`` with a fresh, unexpired token, the
    waiter returns THAT instead of firing a second upstream call.

    ``lock_path`` enables the production cross-process guarantee. OAuth
    callers pass a stable provider-scoped path next to the token store; tests
    and pure in-memory users may omit it and retain thread-only singleflight.
    """
    lock = _sf_lock(provider, refresh_tok)
    with lock:
        if lock_path:
            with locked_path(lock_path):
                return _refresh_once(
                    provider, refresh_tok, fn, load=load,
                    require_current=True)
        return _refresh_once(provider, refresh_tok, fn, load=load)


def _refresh_once(provider: str, refresh_tok: str, fn, *, load=None,
                  require_current: bool = False):
    """Re-check persisted state then refresh; caller owns singleflight."""
    if load is not None:
        try:
            current = load() or {}
        except Exception as e:
            logger.debug('[TokenStore] singleflight reload failed: %s', e)
            current = {}
        cur_rt = current.get('refresh_token') or ''
        if require_current and not cur_rt:
            logger.info('[TokenStore] %s refresh cancelled — credential was '
                        'deleted or became unreadable', provider)
            return None
        if cur_rt and cur_rt != refresh_tok:
            try:
                expires_at = float(current.get('expire') or 0)
            except (TypeError, ValueError, OverflowError) as error:
                logger.debug('[TokenStore] %s stored expiry is malformed: %s',
                             provider, error)
                expires_at = 0
            if math.isfinite(expires_at) and expires_at > time.time() + 60:
                logger.info('[TokenStore] %s refresh merged — reusing '
                            'concurrent result', provider)
                return current
            # The originally supplied refresh token was single-use and has
            # already been replaced. If the replacement also needs refresh,
            # advance from THAT token; retrying the obsolete one can revoke
            # the concurrently-issued credential.
            logger.info('[TokenStore] %s refresh advanced to the newest '
                        'stored refresh token', provider)
            return fn(cur_rt)
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
    if (not isinstance(provider, str)
            or not _PROVIDER_RE.fullmatch(provider)
            or provider not in _SUPPORTED_PROVIDERS):
        raise ValueError(f'unsupported OAuth provider: {provider!r}')
    return _config_path(os.path.join('oauth', f'{provider}.json'))


def _harden_token_permissions(path: str) -> None:
    """Best-effort migration of legacy token stores to private modes."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError as e:
        logger.warning('[TokenStore] Could not restrict token directory %s: %s',
                       parent, e)
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.warning('[TokenStore] Could not restrict token file %s: %s',
                           path, e)


def load_token(provider: str) -> dict | None:
    """Load stored OAuth token for a provider.

    Returns:
        Token dict or None if not found / invalid.
    """
    path = token_path(provider)
    try:
        with _store_lock(provider):
            if not os.path.isfile(path):
                return None
            _harden_token_permissions(path)
            data = read_json(path, default=None)
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
    if not isinstance(token_data, dict):
        logger.error('[TokenStore] Refusing non-object token for %s', provider)
        return False
    try:
        payload = dict(token_data)
        payload['_saved_at'] = time.strftime(
            '%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with _store_lock(provider):
            _harden_token_permissions(path)
            write_json_atomic(path, payload, mode=0o600)
        logger.info('[TokenStore] Saved token for %s (email=%s)',
                    provider, payload.get('email', '?'))
        return True
    except Exception as e:
        logger.error('[TokenStore] Failed to save token for %s: %s', provider, e, exc_info=True)
        return False


def delete_token(provider: str) -> bool:
    """Delete stored OAuth token for a provider."""
    path = token_path(provider)
    try:
        # Serialize logout with the whole refresh transaction. If refresh is
        # already in flight, deletion waits and wins afterwards; if logout
        # wins first, the refresh's locked re-read sees no credential and
        # cancels instead of resurrecting it.
        with locked_path(path + '.refresh'):
            with _store_lock(provider):
                if os.path.isfile(path):
                    os.remove(path)
                    logger.info('[TokenStore] Deleted token for %s', provider)
        return True
    except Exception as e:
        logger.warning('[TokenStore] Failed to delete token for %s: %s', provider, e)
        return False

"""lib/oauth/token_store.py — Persistent token storage for OAuth credentials.

Tokens are stored in data/config/oauth/<provider>.json.
"""

import json
import os
import time

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['load_token', 'save_token', 'delete_token', 'token_path',
           'OAuthExchangeError']


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

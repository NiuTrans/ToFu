"""lib/feishu/_state.py — Per-user state, locks, and configuration constants.

Centralizes all mutable module-level state so that other sub-modules
can import from one place rather than relying on globals scattered
across a monolith.
"""

import os
import threading

from lib.feishu.user_state import (
    MAX_FEISHU_HISTORY_MESSAGES,
    feishu_user_sessions,
    get_user_processing_lock,
)
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'APP_ID',
    'APP_SECRET',
    'ENABLED',
    'ALLOWED_USERS',
    'DEFAULT_PROJECT_PATH',
    'WORKSPACE_ROOT',
    'MAX_HISTORY',
    'FEISHU_MSG_LIMIT',
    'apply_config',
    'active_user_count',
    'get_user_lock',
    'pin_user_session',
]

# ── Config from environment ────────────────────────────────
APP_ID = os.getenv('FEISHU_APP_ID', '')
APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
ENABLED = bool(APP_ID and APP_SECRET)

# Comma-separated open_id list — empty = allow all
_allowed_raw = os.getenv('FEISHU_ALLOWED_USERS', '')
ALLOWED_USERS = set(filter(None, _allowed_raw.split(',')))

DEFAULT_PROJECT_PATH = os.getenv(
    'FEISHU_DEFAULT_PROJECT',
    os.path.expanduser('~/Projects/tofu'),
)
WORKSPACE_ROOT = os.getenv(
    'FEISHU_WORKSPACE_ROOT',
    os.path.expanduser('~/Projects'),
)
MAX_HISTORY = MAX_FEISHU_HISTORY_MESSAGES

# Feishu Lark client singleton
_lark_client = None
_lark_client_lock = threading.Lock()
FEISHU_MSG_LIMIT = 4000


def apply_config(feishu: dict) -> bool:
    """Apply a ``server_config.json`` ``feishu`` block in place.

    This is what makes GUI-saved Feishu settings authoritative at runtime:
    the web UI persists to server_config.json and every live reader must see
    the new values without a process restart. Returns True when the app
    credentials changed — callers then know any derived client/connection
    still holds the old app. An explicitly empty ``app_secret`` clears it
    (the settings UI omits the key when the user leaves the field blank).
    ``ALLOWED_USERS`` is mutated in place so modules that imported the set
    by reference observe the update.
    """
    global APP_ID, APP_SECRET, ENABLED
    global DEFAULT_PROJECT_PATH, WORKSPACE_ROOT, _lark_client
    creds_changed = False
    if 'app_id' in feishu:
        new_id = feishu.get('app_id') or ''
        creds_changed = creds_changed or new_id != APP_ID
        APP_ID = new_id
    if 'app_secret' in feishu:
        new_secret = feishu.get('app_secret') or ''
        creds_changed = creds_changed or new_secret != APP_SECRET
        APP_SECRET = new_secret
    ENABLED = bool(APP_ID and APP_SECRET)
    if isinstance(feishu.get('allowed_users'), list):
        ALLOWED_USERS.clear()
        ALLOWED_USERS.update(feishu['allowed_users'])
    if feishu.get('default_project'):
        DEFAULT_PROJECT_PATH = feishu['default_project']
    if feishu.get('workspace_root'):
        WORKSPACE_ROOT = feishu['workspace_root']
    if creds_changed:
        # The messaging singleton was built against the old app; force a
        # rebuild on next send so new credentials actually take effect.
        with _lark_client_lock:
            _lark_client = None
    return creds_changed


def get_user_lock(user_id: str) -> threading.Lock:
    """Get or create a per-user lock for sequential message processing."""
    return get_user_processing_lock(user_id)


def active_user_count() -> int:
    """Return the bounded resident-session count for status diagnostics.

    The wire field retains its historical ``active_users`` name, but this is
    reconstructible process state—not a durable or concurrently active-user
    census. Keeping the projection here prevents routes from reaching into a
    removed dictionary or the store's private internals.
    """
    return len(feishu_user_sessions)


def pin_user_session(user_id: str):
    """Pin reconstructible state for the duration of one external event."""
    return feishu_user_sessions.pin(user_id)

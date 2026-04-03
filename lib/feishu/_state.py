"""lib/feishu/_state.py — Per-user state, locks, and configuration constants.

Centralizes all mutable module-level state so that other sub-modules
can import from one place rather than relying on globals scattered
across a monolith.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

__all__ = [
    'APP_ID',
    'APP_SECRET',
    'ENABLED',
    'ALLOWED_USERS',
    'DEFAULT_PROJECT_PATH',
    'WORKSPACE_ROOT',
    'MAX_HISTORY',
    'MAX_WEB_MESSAGES',
    'FEISHU_MSG_LIMIT',
    'get_user_lock',
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
    os.path.expanduser('~/chatui'),
)
WORKSPACE_ROOT = os.getenv(
    'FEISHU_WORKSPACE_ROOT',
    os.path.expanduser('~/Projects'),
)
MAX_HISTORY = 40  # max conversation turns kept in memory

# ── Per-user mutable state (protected by locks) ───────────
_conversations = {}     # { open_id: [messages] }
_conv_lock = threading.Lock()

_user_models = {}       # { open_id: model_name }
_user_modes = {}        # { open_id: 'chat' | 'tool' }
_user_projects = {}     # { open_id: path_str }
_user_conv_ids = {}     # { open_id: conv_id }
_user_pending = {}      # { open_id: {'type': ..., ...} }
_user_state_lock = threading.Lock()  # Guards the 5 dicts above

# Per-user processing locks — ensures messages are handled sequentially
_user_task_locks = {}
_task_locks = {}
_task_locks_lock = threading.Lock()

# Per-user web-format message list (rich format for DB sync)
_user_web_messages = {}   # user_id → list of web-format messages
_web_msg_lock = threading.Lock()
MAX_WEB_MESSAGES = 40     # cap mirroring MAX_HISTORY

# Feishu Lark client singleton
_lark_client = None
_lark_client_lock = threading.Lock()
FEISHU_MSG_LIMIT = 4000


def get_user_lock(user_id: str) -> threading.Lock:
    """Get or create a per-user lock for sequential message processing."""
    with _task_locks_lock:
        if user_id not in _user_task_locks:
            _user_task_locks[user_id] = threading.Lock()
        return _user_task_locks[user_id]

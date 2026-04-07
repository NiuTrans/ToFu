"""lib/feishu/conversation.py — Conversation history & DB synchronization.

Manages per-user chat history in memory and syncs to the web UI database
so that Feishu conversations appear alongside web conversations.
"""

import uuid

from lib.feishu._state import (
    DEFAULT_PROJECT_PATH,
    MAX_HISTORY,
    MAX_WEB_MESSAGES,
    _conv_lock,
    _conversations,
    _user_conv_ids,
    _user_models,
    _user_modes,
    _user_pending,
    _user_projects,
    _user_state_lock,
    _user_web_messages,
    _web_msg_lock,
)
from lib.utils import safe_json

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['get_history', 'append_message', 'clear_history', 'new_conv_id', 'get_conv_id', 'append_web_message', 'get_web_messages', 'clear_web_messages', 'sync_to_db', 'get_model', 'set_model', 'get_mode', 'set_mode', 'get_project', 'set_project', 'get_pending', 'set_pending', 'clear_pending']


# ── History CRUD ───────────────────────────────────────────

def get_history(user_id: str) -> list:
    """Return a copy of the user's conversation history."""
    with _conv_lock:
        if user_id not in _conversations:
            _conversations[user_id] = []
        return list(_conversations[user_id])


def append_message(user_id: str, role: str, content: str) -> None:
    """Append a message and enforce MAX_HISTORY cap."""
    with _conv_lock:
        if user_id not in _conversations:
            _conversations[user_id] = []
        _conversations[user_id].append({'role': role, 'content': content})
        # Trim from front, keeping system message if present
        while len(_conversations[user_id]) > MAX_HISTORY:
            _conversations[user_id].pop(0)


def clear_history(user_id: str) -> None:
    with _conv_lock:
        _conversations[user_id] = []


# ── Conversation ID management ─────────────────────────────

def new_conv_id(user_id: str) -> str:
    """Create a fresh conversation ID for the user."""
    cid = str(uuid.uuid4())
    with _user_state_lock:
        _user_conv_ids[user_id] = cid
    return cid


def get_conv_id(user_id: str) -> str:
    with _user_state_lock:
        if user_id not in _user_conv_ids:
            cid = str(uuid.uuid4())
            _user_conv_ids[user_id] = cid
        return _user_conv_ids[user_id]


# ── Web message mirror (for DB sync) ──────────────────────

def append_web_message(user_id: str, msg: dict) -> None:
    """Append a web-format message to the user's mirror list."""
    with _web_msg_lock:
        if user_id not in _user_web_messages:
            _user_web_messages[user_id] = []
        _user_web_messages[user_id].append(msg)
        while len(_user_web_messages[user_id]) > MAX_WEB_MESSAGES:
            _user_web_messages[user_id].pop(0)


def get_web_messages(user_id: str) -> list:
    with _web_msg_lock:
        return list(_user_web_messages.get(user_id, []))


def clear_web_messages(user_id: str) -> None:
    with _web_msg_lock:
        _user_web_messages[user_id] = []


# ── DB sync ────────────────────────────────────────────────

def sync_to_db(user_id: str) -> None:
    """Persist the Feishu conversation to the web DB.

    Uses ``get_thread_db()`` (thread-local connection) since Feishu handlers
    run outside Flask request context where ``get_db()`` is unavailable.
    Schema: conversations(id TEXT, user_id INTEGER, title, messages, created_at,
    updated_at, settings, msg_count)  — primary key is (id, user_id).
    """
    conv_id = get_conv_id(user_id)
    web_msgs = get_web_messages(user_id)
    # Guard against messages=None (treat as empty)
    if web_msgs is None:
        web_msgs = []
    if not web_msgs:
        return
    db = None
    try:
        import time

        from lib.database import DOMAIN_CHAT, get_thread_db

        db = get_thread_db(DOMAIN_CHAT)
        db_user_id = 1  # single-user; Feishu users map to this

        # ── Guard: refuse to overwrite non-empty conv with fewer messages ──
        existing = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=?',
            (conv_id, db_user_id)
        ).fetchone()
        if existing:
            # safe_json handles None, empty-string, and corrupt JSON
            # without crashing — returns *default* on any parse failure.
            existing_msgs = safe_json(
                existing['messages'], default=[], label='feishu-sync-messages'
            )
            if not isinstance(existing_msgs, list):
                existing_msgs = []
            if len(existing_msgs) > len(web_msgs):
                logger.warning(
                    '[Feishu] ⚠️ BLOCKED overwrite of conv %s — '
                    'DB has %d msgs but Feishu buffer has only %d. '
                    'Possible stale in-memory state.',
                    conv_id[:12], len(existing_msgs), len(web_msgs),
                )
                return

        title = (web_msgs[0].get('content', '') or 'Feishu')[:80]
        from lib.database import json_dumps_pg
        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(web_msgs)
        search_text = build_search_text(web_msgs)
        now = int(time.time() * 1000)

        db.execute(
            '''INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at, msg_count, search_text, search_tsv)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, to_tsvector('simple', left(?, 50000)))
               ON CONFLICT(id, user_id) DO UPDATE SET
                 title=excluded.title, messages=excluded.messages,
                 updated_at=excluded.updated_at, msg_count=excluded.msg_count,
                 search_text=excluded.search_text, search_tsv=excluded.search_tsv''',
            (conv_id, db_user_id, title, messages_json, now, now, len(web_msgs), search_text, search_text)
        )
        db.commit()
        logger.debug('[Feishu] Synced %d messages for user %s to DB conv %s',
                      len(web_msgs), user_id, conv_id[:12])
    except Exception as e:
        logger.warning('[Feishu] DB sync failed for user %s: %s', user_id, e, exc_info=True)
    finally:
        if db is not None:
            try:
                pass  # thread-local connection managed by get_thread_db; no manual close
            except Exception as e:
                logger.debug('[Feishu] sync_to_db cleanup note: %s', e, exc_info=True)


# ── Model / Mode / Project getters ────────────────────────

def get_model(user_id: str) -> str:
    from lib import LLM_MODEL
    with _user_state_lock:
        return _user_models.get(user_id, LLM_MODEL)


def set_model(user_id: str, model: str) -> None:
    with _user_state_lock:
        _user_models[user_id] = model


def get_mode(user_id: str) -> str:
    with _user_state_lock:
        return _user_modes.get(user_id, 'chat')


def set_mode(user_id: str, mode: str) -> None:
    with _user_state_lock:
        _user_modes[user_id] = mode


def get_project(user_id: str) -> str:
    with _user_state_lock:
        return _user_projects.get(user_id, DEFAULT_PROJECT_PATH)


def set_project(user_id: str, path: str) -> None:
    with _user_state_lock:
        _user_projects[user_id] = path


def get_pending(user_id: str):
    with _user_state_lock:
        return _user_pending.get(user_id)


def set_pending(user_id: str, value) -> None:
    with _user_state_lock:
        _user_pending[user_id] = value


def clear_pending(user_id: str) -> None:
    with _user_state_lock:
        _user_pending.pop(user_id, None)

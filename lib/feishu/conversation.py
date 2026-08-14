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
        from lib.database.conversation_repository import load_conversation
        existing = load_conversation(db, conv_id, user_id=db_user_id)
        if existing is not None:
            existing_msgs = existing.messages
            if len(existing_msgs) > len(web_msgs):
                logger.warning(
                    '[Feishu] ⚠️ BLOCKED overwrite of conv %s — '
                    'DB has %d msgs but Feishu buffer has only %d. '
                    'Possible stale in-memory state.',
                    conv_id[:12], len(existing_msgs), len(web_msgs),
                )
                return

        title = (web_msgs[0].get('content', '') or 'Feishu')[:80]
        from lib.conversations import build_search_text
        search_text = build_search_text(web_msgs)
        now = int(time.time() * 1000)

        from lib.database.conversation_repository import upsert_conversation
        # The Feishu buffer front-trims at MAX_WEB_MESSAGES, so the repository
        # performs a full canonical-row refresh in the same transaction.
        upsert_conversation(
            db, conv_id, web_msgs, user_id=db_user_id, title=title,
            created_at=now, updated_at=now, search_text=search_text, full=True,
            expected_rev=(existing['rev'] if existing is not None else None))
        logger.debug('[Feishu] Synced %d messages for user %s to DB conv %s',
                      len(web_msgs), user_id, conv_id[:12])
    except Exception as e:
        logger.warning('[Feishu] DB sync failed for user %s: %s', user_id, e, exc_info=True)
    finally:
        if db is not None:
            # Feishu handlers run on long-lived bot/event threads. Release the
            # thread-local connection back to the shared pool so it isn't
            # pinned for the thread's whole life (connection-semaphore leak).
            try:
                from lib.database import DOMAIN_CHAT, close_thread_db
                close_thread_db(DOMAIN_CHAT)
            except Exception as e:
                logger.debug('[Feishu] sync_to_db close_thread_db failed: %s', e, exc_info=True)


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

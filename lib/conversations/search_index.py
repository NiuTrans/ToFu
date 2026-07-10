"""Full-text search-indexing helper for conversations.

``build_search_text`` flattens a conversation's messages into a single
plain-text blob used to populate the ``search_text`` column. Moved out of
``routes/conversations.py`` so that lib-layer callers (DB schema bootstrap,
Feishu pipeline, scheduler, task manager / autopilot / endpoint) no longer
import UP into the routes package.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def build_search_text(messages):
    """Extract plain text from messages list for full-text search indexing.

    Concatenates all user/assistant content and thinking fields into a single
    string, separated by newlines.  Tool calls, metadata, and JSON structure
    are stripped — only human-readable text is kept.

    Args:
        messages: List of message dicts (or raw JSON string / None).

    Returns:
        Flattened plain-text string suitable for full-text search.
    """
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Conversations] Failed to parse messages JSON: %s', e)
            return ''
    if not isinstance(messages, list):
        return ''
    parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role', '')
        if role not in ('user', 'assistant'):
            continue
        content = msg.get('content', '')
        if isinstance(content, list):
            # Multi-part content (text + images)
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get('text', ''))
        elif isinstance(content, str) and content:
            parts.append(content)
        thinking = msg.get('thinking', '')
        if isinstance(thinking, str) and thinking:
            parts.append(thinking)
        # Translated content (from translate feature) — must be indexed so
        # users can search in the translated language (e.g. Chinese translation
        # of an English assistant reply).
        translated = msg.get('translatedContent', '')
        if isinstance(translated, str) and translated:
            parts.append(translated)
        # Original pre-translation text (auto-translate-user feature): when a
        # user message is auto-translated to English, `content` holds the
        # translation and the text the user actually typed lives in
        # `originalContent`. Index it too, or the user can't find their own
        # message by the words they wrote (the mirror of translatedContent).
        original = msg.get('originalContent', '')
        if isinstance(original, str) and original:
            parts.append(original)
    return '\n'.join(parts)


def update_conversation_fts(db, conv_id, search_text):
    """Refresh the SQLite FTS5 index row for one conversation.

    No-op on every non-SQLite backend: ``conversations_fts`` is a
    SQLite-only FTS5 virtual table (created in ``_schema_sqlite.py``).
    PostgreSQL has no such table — it serves search via the ``LIKE``
    fallback in ``routes/conversations_search.py`` — so emitting this
    ``INSERT`` on PG raised ``UndefinedTable`` on every conversation
    write, spamming ``error.log``.

    Args:
        db: Open DB connection/cursor handle.
        conv_id: Conversation id whose FTS row to refresh.
        search_text: Flattened search text (from ``build_search_text``);
            falsy values are ignored.
    """
    if not search_text:
        return
    from lib.database import _core
    if getattr(_core, '_BACKEND', 'sqlite') != 'sqlite':
        return
    try:
        # ``conversations_fts`` is a CONTENTLESS FTS5 table (content='').
        # In that mode the inverted index has no backing row to diff
        # against, so a plain ``INSERT OR REPLACE`` does NOT retract the
        # rowid's previously-indexed terms — it just layers the new tokens
        # on top. After a conversation EDIT the OLD text would still MATCH
        # in Phase-1 FTS (a stale search hit). Explicitly DELETE the rowid's
        # index entry first, then insert the fresh tokens.
        row = db.execute(
            "SELECT rowid FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if row is None:
            return
        rowid = row['rowid'] if isinstance(row, dict) else row[0]
        db.execute("DELETE FROM conversations_fts WHERE rowid = ?", (rowid,))
        db.execute(
            "INSERT INTO conversations_fts (rowid, search_text) VALUES (?, ?)",
            (rowid, search_text)
        )
        db.commit()
    except Exception as e:
        logger.debug('[FTS] update failed for conv=%s (non-fatal): %s', conv_id, e)


__all__ = ['build_search_text', 'update_conversation_fts']

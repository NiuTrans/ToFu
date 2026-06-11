"""Chat persistence + task-metadata helpers.

The conversation load/create/persist functions and the task-metadata
extractors (in-memory task dict ↔ ``task_results`` DB row) moved out of
``routes/chat.py`` so the routes file stays a thin HTTP layer. None of these
touch Flask request state — they take an explicit ``db`` handle and plain
dicts — so they belong in lib.
"""

import json
import re
import time

from lib.database import db_execute_with_retry, json_dumps_pg
from lib.database._core_schema import CONVERSATIONS, upsert
from lib.log import get_logger

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors routes/common.py


def extract_db_meta(row):
    """Extract metadata dict from a DB task_results row."""
    meta = {}
    if row['metadata']:
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] Failed to parse task metadata JSON (task_id=%s): %s', row['task_id'], e, exc_info=True)
    return meta


def extract_task_meta(task):
    """Extract metadata fields from an in-memory task dict.

    MUST stay in sync with ``extract_db_meta`` (DB-row equivalent) and
    with the ``meta`` dict built in ``manager.persist_task_result``.  Any
    field added here must also appear in:
      * persist_task_result ’s ``meta`` dict (so it lands in task_results)
      * the chat_poll DB-path field loop (so /api/chat/poll returns it)
      * the cold-replay synth-done in chat_stream (so Last-Event-ID
        replay after server restart returns the same shape)
    Asymmetry between these four paths historically caused "my apiRounds
    disappeared after I came back" / "modifiedFiles missing on reload".
    """
    meta = {}
    if task.get('finishReason'):
        meta['finishReason'] = task['finishReason']
    if task.get('usage'):
        meta['usage'] = task['usage']
    if task.get('preset'):
        meta['preset'] = task['preset']
    if task.get('model'):
        meta['model'] = task['model']
    if task.get('provider_id'):
        meta['provider_id'] = task['provider_id']
    if task.get('thinkingDepth'):
        meta['thinkingDepth'] = task['thinkingDepth']
    if task.get('toolSummary'):
        meta['toolSummary'] = task['toolSummary']
    if task.get('apiRounds'):
        meta['apiRounds'] = task['apiRounds']
    if task.get('modifiedFiles'):
        meta['modifiedFiles'] = task['modifiedFiles']
    if task.get('modifiedFileList'):
        meta['modifiedFileList'] = task['modifiedFileList']
    if task.get('_fallback_model'):
        meta['fallbackModel'] = task['_fallback_model']
    if task.get('_fallback_from'):
        meta['fallbackFrom'] = task['_fallback_from']
    if task.get('_fallback_reason'):
        meta['fallbackReason'] = task['_fallback_reason']
    if task.get('_fallback_kind'):
        meta['fallbackKind'] = task['_fallback_kind']
    return meta


def load_or_create_conv(db, conv_id, config, payload):
    """Load existing conversation messages or create a new one.

    Returns:
        (messages_list, is_new, title) or raises.
    """
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()

    if row:
        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Send] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            messages = []
        return messages, False, row['title']

    # New conversation — create it
    title = (payload.get('text') or 'New Chat')[:60]
    # Strip <notranslate>/<nt> tags from title
    title = re.sub(r'</?(?:notranslate|nt)>', '', title, flags=re.IGNORECASE)
    now_ms = int(time.time() * 1000)
    settings = {}
    if config.get('projectPath'):
        settings['projectPath'] = config['projectPath']
    if payload.get('folderId'):
        settings['folderId'] = payload['folderId']

    db_execute_with_retry(db, '''
        INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at, settings, msg_count, search_text)
        VALUES (?, ?, ?, '[]', ?, ?, ?, 0, '')
    ''', (conv_id, DEFAULT_USER_ID, title, now_ms, now_ms,
          json.dumps(settings, ensure_ascii=False)))

    return [], True, title


def persist_conv_messages(db, conv_id, messages, title, settings_patch=None):
    """Write messages + metadata to the conversation row.

    Backfills stable per-message ``_msgId`` UUIDs before writing.  Every
    code path that mutates ``messages`` and persists the array (send,
    regenerate, edit, continue, chat_continue) goes through this helper,
    so this is the single point of truth for id assignment on the chat
    write side — mirroring ``_assign_message_ids`` calls in
    ``manager.py`` for the partial/result sync paths.  Without this,
    newly appended messages on those flows would have no ``_msgId``,
    forcing PATCH /messages/by-id to silently fall back to index lookup.
    """
    # Lazy import to avoid the lib.chat → lib.tasks_pkg.manager import cycle.
    from lib.tasks_pkg.manager import _assign_message_ids
    _assign_message_ids(messages)
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)

    from lib.conversations import build_search_text
    search_text = build_search_text(messages)

    # Build settings update
    settings_update = {}
    if settings_patch:
        settings_update.update(settings_patch)

    # Always inject lastMsgRole/lastMsgTimestamp
    if messages:
        last = messages[-1]
        settings_update['lastMsgRole'] = last.get('role')
        settings_update['lastMsgTimestamp'] = last.get('timestamp')

    # Merge with existing settings AND preserve original created_at
    existing = db.execute(
        'SELECT settings, created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if existing:
        try:
            settings = json.loads(existing['settings'] or '{}')
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug('[chat] persist_conv_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
            settings = {}
        settings.update(settings_update)
        # ★ Preserve original created_at — INSERT OR REPLACE would overwrite
        #   it with now_ms, causing all conversations to lose their real
        #   creation timestamp on every message send/regenerate/edit.
        created_at = existing['created_at'] or now_ms
    else:
        settings = settings_update
        created_at = now_ms

    settings_json = json.dumps(settings, ensure_ascii=False)

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages_json, 'created_at': created_at, 'updated_at': now_ms,
        'settings': settings_json, 'msg_count': len(messages), 'search_text': search_text,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                    'updated_at', 'settings', 'msg_count', 'search_text'], retry=True)
    from lib.conversations import update_conversation_fts
    update_conversation_fts(db, conv_id, search_text)


__all__ = [
    'extract_db_meta',
    'extract_task_meta',
    'load_or_create_conv',
    'persist_conv_messages',
]

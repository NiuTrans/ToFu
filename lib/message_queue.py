"""lib/message_queue.py — Server-side message queue for conversations.

When a conversation has an active task running, new user messages are
enqueued server-side.  When the task completes, the next queued message
is automatically dispatched as a new task.

This replaces the frontend-only ``pendingMessageQueue`` Map that was lost
on page refresh.
"""

import json
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# Lock for dispatch coordination (prevent double-dispatch races)
_dispatch_lock = threading.Lock()


def _ensure_table():
    """Create the message_queue table if it doesn't exist (migration-safe)."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('''
            CREATE TABLE IF NOT EXISTS message_queue (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                config TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL DEFAULT 1,
                created_at BIGINT NOT NULL
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
        db.commit()
    except Exception as e:
        logger.debug('[Queue] _ensure_table: %s', e)
        try:
            db.rollback()
        except Exception:
            pass

# Auto-create table on module load (safe for existing DBs)
_table_ensured = False

def _maybe_ensure_table():
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True


def enqueue_message(conv_id: str, message_data: dict, config: dict) -> dict:
    """Add a message to the server-side queue for a conversation.

    Args:
        conv_id: Conversation ID.
        message_data: Dict with keys: text, images, pdfTexts, replyQuotes,
                      convRefs, convRefTexts, originalContent, timestamp.
        config: The chat config to use when dispatching this message
                (model, searchMode, tools, etc.).

    Returns:
        Dict with queue_id and position.
    """
    _maybe_ensure_table()

    queue_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    timestamp = message_data.get('timestamp', now_ms)

    db = get_thread_db(DOMAIN_CHAT)

    # Get current queue depth for position
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    position = (row[0] if row else 0) + 1

    payload = json.dumps(message_data, ensure_ascii=False)
    config_json = json.dumps(config, ensure_ascii=False)

    db_execute_with_retry(db, '''
        INSERT INTO message_queue (id, conv_id, payload, config, position, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (queue_id, conv_id, payload, config_json, position, timestamp))

    logger.info('[Queue] Enqueued message %s for conv=%s position=%d text=%d chars',
                queue_id[:8], conv_id[:8], position,
                len(message_data.get('text', '')))

    return {'queueId': queue_id, 'position': position}


def get_queue(conv_id: str) -> list[dict]:
    """Get all queued messages for a conversation, ordered by position.

    Returns:
        List of dicts with keys: queueId, position, text (preview),
        hasImages, hasPdfs, hasRefs, hasQuotes, timestamp.
    """
    _maybe_ensure_table()
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT id, payload, position, created_at FROM message_queue '
        'WHERE conv_id=? ORDER BY position ASC',
        (conv_id,)
    ).fetchall()

    result = []
    for row in rows:
        try:
            data = json.loads(row['payload'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Queue] Failed to parse payload for queue_id=%s: %s', row['id'][:8], e)
            data = {}

        result.append({
            'queueId': row['id'],
            'position': row['position'],
            'text': (data.get('text', '') or '')[:100],
            'hasImages': bool(data.get('images')),
            'hasPdfs': bool(data.get('pdfTexts')),
            'hasRefs': bool(data.get('convRefs')),
            'hasQuotes': bool(data.get('replyQuotes')),
            'timestamp': row['created_at'],
        })

    return result


def remove_from_queue(conv_id: str, queue_id: str) -> bool:
    """Remove a specific message from the queue.

    Returns:
        True if removed, False if not found.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT id FROM message_queue WHERE id=? AND conv_id=?',
        (queue_id, conv_id)
    ).fetchone()
    if not row:
        return False

    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)

    logger.info('[Queue] Removed message %s from conv=%s', queue_id[:8], conv_id[:8])
    return True


def clear_queue(conv_id: str) -> int:
    """Clear all queued messages for a conversation.

    Returns:
        Number of messages removed.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    count = row[0] if row else 0

    if count > 0:
        db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
        logger.info('[Queue] Cleared %d messages from conv=%s', count, conv_id[:8])

    return count



def _renumber_positions(db, conv_id: str):
    """Re-number position column after a deletion to keep them contiguous."""
    rows = db.execute(
        'SELECT id FROM message_queue WHERE conv_id=? ORDER BY position ASC',
        (conv_id,)
    ).fetchall()
    for i, row in enumerate(rows, 1):
        db.execute(
            'UPDATE message_queue SET position=? WHERE id=?',
            (i, row['id'])
        )
    db.commit()


def dequeue_next(conv_id: str) -> dict | None:
    """Pop the next message from the queue (lowest position).

    Returns:
        Full message dict (payload + config) or None if queue is empty.
    """
    db = get_thread_db(DOMAIN_CHAT)

    row = db.execute(
        'SELECT id, payload, config FROM message_queue '
        'WHERE conv_id=? ORDER BY position ASC LIMIT 1',
        (conv_id,)
    ).fetchone()

    if not row:
        return None

    queue_id = row['id']
    try:
        payload = json.loads(row['payload'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse payload for dequeue queue_id=%s: %s', queue_id[:8], e)
        payload = {}

    try:
        config = json.loads(row['config'])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Queue] Failed to parse config for dequeue queue_id=%s: %s', queue_id[:8], e)
        config = {}

    # Remove from queue
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE id=?', (queue_id,))
    _renumber_positions(db, conv_id)

    logger.info('[Queue] Dequeued message %s from conv=%s, text=%d chars',
                queue_id[:8], conv_id[:8], len(payload.get('text', '')))

    return {
        'queueId': queue_id,
        'payload': payload,
        'config': config,
    }


def dispatch_next_queued(conv_id: str) -> str | None:
    """Dispatch the next queued message for a conversation as a new task.

    Called after a task completes.  If there are queued messages, the first
    one is dequeued, its user message is appended to the conversation in the
    DB, and a new task is started.

    Returns:
        The new task_id if dispatched, None if queue was empty.
    """
    with _dispatch_lock:
        item = dequeue_next(conv_id)
        if not item:
            return None

        payload = item['payload']
        config = item['config']
        text = payload.get('text', '')
        # ★ _user_msg: pre-built (and already translated) user message dict
        #   from /api/chat/send.  If present, skip translation and use directly.
        pre_built_user_msg = payload.get('_user_msg')

        logger.info('[Queue] Dispatching queued message for conv=%s text=%d chars pre_built=%s',
                    conv_id[:8], len(text), bool(pre_built_user_msg))

        db = get_thread_db(DOMAIN_CHAT)

        if pre_built_user_msg:
            # ★ New path: /api/chat/send already built + translated the user message.
            #   Just append it to the conversation DB.
            row = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if not row:
                logger.warning('[Queue] Conversation %s not found for dispatch', conv_id[:8])
                return None

            try:
                messages = json.loads(row['messages'] or '[]')
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Queue] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
                messages = []

            messages.append(pre_built_user_msg)

            from lib.database import json_dumps_pg
            now_ms = int(time.time() * 1000)
            db_execute_with_retry(db, '''
                UPDATE conversations SET messages=?, updated_at=?, msg_count=?
                WHERE id=? AND user_id=1
            ''', (json_dumps_pg(messages), now_ms, len(messages), conv_id))

            remaining = _get_queue_depth(db, conv_id)
            logger.info('[Queue] Appended pre-built user msg to conv=%s (now %d msgs)',
                        conv_id[:8], len(messages))

        else:
            # Legacy path: message was enqueued via /api/chat/queue (old API).
            # Need to translate and append to conversation ourselves.
            import re
            auto_translate = config.get('autoTranslate', False)
            has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text)) if text else False
            translated_text = text
            _translate_model = None
            if auto_translate and has_chinese:
                try:
                    from routes.translate import _build_translate_prompt, _translate_one_chunk
                    system_prompt = _build_translate_prompt('English', 'Chinese')
                    result, _usage = _translate_one_chunk(
                        text, system_prompt, chunk_label=':queue',
                        source='Chinese', target='English',
                    )
                    if result and result.strip():
                        translated_text = result.strip()
                        if isinstance(_usage, dict):
                            _disp = _usage.get('_dispatch', {})
                            _translate_model = _disp.get('model', _usage.get('model'))
                        logger.info('[Queue] Auto-translated queued message for conv=%s: %d→%d chars model=%s',
                                    conv_id[:8], len(text), len(translated_text), _translate_model)
                    else:
                        translated_text = text
                except Exception as e:
                    logger.warning('[Queue] Auto-translate failed for conv=%s: %s', conv_id[:8], e)
                    translated_text = text

            # Build user message
            user_msg = {
                'role': 'user',
                'content': translated_text if auto_translate and has_chinese else text,
                'timestamp': payload.get('timestamp', int(time.time() * 1000)),
            }
            if auto_translate and has_chinese and translated_text != text:
                user_msg['originalContent'] = text
                user_msg['_translateDone'] = True
                if _translate_model:
                    user_msg['_translateModel'] = _translate_model
            if payload.get('images'):
                user_msg['images'] = payload['images']
            if payload.get('pdfTexts'):
                user_msg['pdfTexts'] = payload['pdfTexts']
            if payload.get('replyQuotes'):
                user_msg['replyQuotes'] = payload['replyQuotes']
            if payload.get('convRefs'):
                user_msg['convRefs'] = payload['convRefs']
            conv_ref_texts = payload.get('convRefTexts')
            if not conv_ref_texts and payload.get('convRefs'):
                try:
                    from routes.chat import _resolve_conv_refs
                    conv_ref_texts = _resolve_conv_refs(payload['convRefs'])
                except Exception as e:
                    logger.warning('[Queue] Failed to resolve conv refs for conv=%s: %s',
                                   conv_id[:8], e)
            if conv_ref_texts:
                user_msg['convRefTexts'] = conv_ref_texts

            # Append user message to conversation in DB
            row = db.execute(
                'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()

            if not row:
                logger.warning('[Queue] Conversation %s not found for dispatch', conv_id[:8])
                return None

            try:
                messages = json.loads(row['messages'] or '[]')
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Queue] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
                messages = []

            messages.append(user_msg)

            from lib.database import json_dumps_pg
            now_ms = int(time.time() * 1000)
            messages_json = json_dumps_pg(messages)

            remaining = _get_queue_depth(db, conv_id)

            db_execute_with_retry(db, '''
                UPDATE conversations
                SET messages=?, updated_at=?, msg_count=?
                WHERE id=? AND user_id=1
            ''', (messages_json, now_ms, len(messages), conv_id))
        # (Legacy _msg_persisted path removed — no longer used)

        # 3. Build API messages and create task
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        api_messages = build_api_messages_from_db(conv_id, config)
        if not api_messages:
            logger.warning('[Queue] No API messages after building for conv=%s', conv_id[:8])
            return None

        from lib.tasks_pkg import create_task, run_task

        task = create_task(conv_id, api_messages, config)
        task_id = task['id']

        # Update conversation settings with the new activeTaskId
        try:
            settings_row = db.execute(
                'SELECT settings FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            settings = json.loads(settings_row[0] or '{}') if settings_row else {}
            settings['activeTaskId'] = task_id
            db_execute_with_retry(db, '''
                UPDATE conversations SET settings=? WHERE id=? AND user_id=1
            ''', (json.dumps(settings, ensure_ascii=False), conv_id))
        except Exception as e:
            logger.warning('[Queue] Failed to update activeTaskId for conv=%s: %s',
                           conv_id[:8], e, exc_info=True)

        # 4. Start the task in a background thread
        _cfg_model = config.get('model', '?')
        logger.info('[Queue] Starting dispatched task %s for conv=%s model=%s remaining=%d',
                    task_id[:8], conv_id[:8], _cfg_model, remaining)

        try:
            threading.Thread(target=run_task, args=(task,), daemon=True).start()
        except Exception:
            logger.exception('[Queue] Failed to start thread for dispatched task %s', task_id[:8])
            task['status'] = 'error'
            task['error'] = 'Server failed to start queued task thread'
            return None

        # Invalidate meta cache so frontend sees the new task
        try:
            from routes.common import _invalidate_meta_cache
            _invalidate_meta_cache()
        except Exception as e:
            logger.debug('[Queue] meta cache invalidation failed: %s', e)

        return task_id


def _get_queue_depth(db, conv_id: str) -> int:
    """Get number of remaining messages in queue."""
    row = db.execute(
        'SELECT COUNT(*) FROM message_queue WHERE conv_id=?',
        (conv_id,)
    ).fetchone()
    return row[0] if row else 0


def get_queue_depth(conv_id: str) -> int:
    """Public version: get queue depth with its own DB connection."""
    db = get_thread_db(DOMAIN_CHAT)
    return _get_queue_depth(db, conv_id)

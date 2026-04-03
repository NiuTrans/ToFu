"""routes/conversations.py — Conversation CRUD endpoints."""

import json
import time

import psycopg2
from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_db, json_dumps_pg
from lib.log import get_logger
from lib.utils import safe_json as _safe_json
from routes.common import DEFAULT_USER_ID, _db_safe, _invalidate_meta_cache, _refresh_meta_cache_if_stale

logger = get_logger(__name__)

conversations_bp = Blueprint('conversations', __name__)


def build_search_text(messages):
    """Extract plain text from messages list for full-text search indexing.

    Concatenates all user/assistant content and thinking fields into a single
    string, separated by newlines.  Tool calls, metadata, and JSON structure
    are stripped — only human-readable text is kept.

    Args:
        messages: List of message dicts (or raw JSON string / None).

    Returns:
        Flattened plain-text string suitable for ILIKE / trgm search.
    """
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except (json.JSONDecodeError, TypeError):
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
    return '\n'.join(parts)


def _conv_row_to_dict(r):
    """Convert a DB row (with messages column) to a conversation dict."""
    return {
        'id': r['id'], 'title': r['title'],
        'messages': _safe_json(r['messages'], default=[], label='messages'),
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _safe_json(r['settings'], default=None, label='settings'),
    }


@conversations_bp.route('/api/conversations', methods=['GET'])
@_db_safe
def list_convs():
    meta_only = request.args.get('meta') == '1'
    prefetch_id = request.args.get('prefetch', '').strip()
    db = get_db(DOMAIN_CHAT)
    if meta_only:
        payload, etag = _refresh_meta_cache_if_stale(db)

        if prefetch_id:
            prefetch_data = None
            try:
                r = db.execute(
                    'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE id=? AND user_id=?',
                    (prefetch_id, DEFAULT_USER_ID)
                ).fetchone()
                if r:
                    prefetch_data = _conv_row_to_dict(r)
            except Exception as e:
                logger.warning('[Common] prefetch conv %s failed: %s', prefetch_id[:12], e)
            combo = json.dumps({
                'conversations': json.loads(payload),
                'prefetched': prefetch_data,
            }, ensure_ascii=False).encode('utf-8')
            combo_resp = Response(combo, mimetype='application/json')
            combo_resp.headers['Cache-Control'] = 'no-cache'
            return combo_resp

        if request.if_none_match and etag in request.if_none_match:
            return Response(status=304)
        resp = Response(payload, mimetype='application/json')
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'private, max-age=5'
        return resp

    rows = db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE user_id=? ORDER BY updated_at DESC',
        (DEFAULT_USER_ID,)
    ).fetchall()
    convs = [_conv_row_to_dict(r) for r in rows]
    return jsonify(convs)


@conversations_bp.route('/api/conversations/<conv_id>', methods=['GET'])
@_db_safe
def get_conv(conv_id):
    """Fetch a single conversation with full messages."""
    db = get_db(DOMAIN_CHAT)
    r = db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not r:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_conv_row_to_dict(r))


@conversations_bp.route('/api/conversations/<conv_id>/export', methods=['GET'])
def export_conv(conv_id):
    """Export a conversation as formatted plain-text for LLM injection."""
    from lib.conv_ref import get_conversation
    detail_param = (request.args.get('include_tool_details', '1')).lower()
    include_details = detail_param not in ('0', 'false', 'no')
    try:
        result = get_conversation(
            conversation_id=conv_id,
            include_tool_details=include_details,
        )
        return jsonify({'ok': True, 'text': result})
    except Exception as e:
        logger.error('[Common] get_conversation failed for conv_id=%s: %s', conv_id, e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@conversations_bp.route('/api/conversations/<conv_id>', methods=['PUT'])
@_db_safe
def save_conv(conv_id):
    data = request.get_json(silent=True) or {}
    title = data.get('title', 'Untitled')
    raw_messages = data.get('messages', [])
    msg_count = len(raw_messages)
    messages = json_dumps_pg(raw_messages)
    created = data.get('createdAt') or data.get('created_at') or int(time.time() * 1000)
    updated = data.get('updatedAt') or data.get('updated_at') or int(time.time() * 1000)
    # ★ Inject lastMsgRole/lastMsgTimestamp into settings for Case E orphan detection.
    # This ensures metadata shells always have last-message info even when the
    # frontend didn't include it (e.g. server-side syncs from _sync_result_to_conversation).
    settings_dict = data.get('settings') or {}
    if msg_count > 0:
        last_msg = raw_messages[-1]
        settings_dict['lastMsgRole'] = last_msg.get('role')
        settings_dict['lastMsgTimestamp'] = last_msg.get('timestamp')
    else:
        settings_dict.pop('lastMsgRole', None)
        settings_dict.pop('lastMsgTimestamp', None)
    settings = json.dumps(settings_dict, ensure_ascii=False)
    db = get_db(DOMAIN_CHAT)

    if msg_count == 0:
        existing = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()
        if existing:
            existing_msgs = _safe_json(existing['messages'], default=[], label='messages')
            if len(existing_msgs) > 0:
                logger.warning('[save_conv] ⚠️ BLOCKED overwrite of conv %s — '
                               'server has %d msgs but client sent 0. '
                               'This is likely a race condition.',
                               conv_id[:12], len(existing_msgs))
                return jsonify({'ok': False, 'error': 'blocked_empty_overwrite',
                                'serverMsgCount': len(existing_msgs)}), 409
        logger.info('[save_conv] Conv %s — saving with 0 messages (new/empty conv)',
                    conv_id[:12])
    else:
        logger.info('[save_conv] Conv %s — saving %d messages, title=%s',
                    conv_id[:12], msg_count, repr(title[:50]))
    search_text = build_search_text(raw_messages)
    db_execute_with_retry(
        db,
        '''INSERT INTO conversations (id, user_id, title, messages, created_at, updated_at, settings, msg_count, search_text, search_tsv)
           VALUES (?,?,?,?,?,?,?,?,?, to_tsvector('simple', left(?, 50000)))
           ON CONFLICT(id, user_id) DO UPDATE SET title=excluded.title, messages=excluded.messages,
           updated_at=excluded.updated_at, settings=excluded.settings, msg_count=excluded.msg_count,
           search_text=excluded.search_text, search_tsv=excluded.search_tsv''',
        (conv_id, DEFAULT_USER_ID, title, messages, created, updated, settings, msg_count, search_text, search_text)
    )
    _invalidate_meta_cache()
    return jsonify({'ok': True})


@conversations_bp.route('/api/conversations/<conv_id>', methods=['DELETE'])
@_db_safe
def delete_conv(conv_id):
    db = get_db(DOMAIN_CHAT)
    c1 = db.execute('DELETE FROM conversations WHERE id=? AND user_id=?', (conv_id, DEFAULT_USER_ID))
    c2 = db.execute('DELETE FROM task_results WHERE conv_id=?', (conv_id,))
    c3 = db.execute('DELETE FROM transcript_archive WHERE conv_id=?', (conv_id,))
    try:
        db.commit()
    except Exception as exc:
        _is_db_err = isinstance(exc, psycopg2.OperationalError)
        if not _is_db_err:
            raise
        try:
            db.rollback()
        except Exception as _rb_err:
            logger.debug('[Conversations] Rollback after delete retry failed: %s', _rb_err)
        time.sleep(1)
        c1 = db.execute('DELETE FROM conversations WHERE id=? AND user_id=?', (conv_id, DEFAULT_USER_ID))
        c2 = db.execute('DELETE FROM task_results WHERE conv_id=?', (conv_id,))
        c3 = db.execute('DELETE FROM transcript_archive WHERE conv_id=?', (conv_id,))
        db.commit()
    _invalidate_meta_cache()
    logger.info('[delete_conv] Deleted conv %s (rows: conv=%d, tasks=%d, transcripts=%d)',
                conv_id[:12], c1.rowcount, c2.rowcount, c3.rowcount)
    return jsonify({'ok': True})


@conversations_bp.route('/api/conversations/search', methods=['GET'])
def search_convs():
    """Server-side full-text search through conversation messages.

    Two-phase approach for speed:
      Phase 1: tsvector prefix match via GIN index (~0-5ms for most queries).
      Phase 2: If <50 results, ILIKE fallback on left(search_text, 10000) to
               catch substring matches that tsvector misses (~70-120ms).

    Snippets are extracted only for the final result set (max 50 rows).
    """
    query = (request.args.get('q') or '').strip().lower()
    if not query or len(query) < 2:
        return jsonify([])

    t0 = time.monotonic()
    db = get_db(DOMAIN_CHAT)

    MAX_RESULTS = 50
    SNIPPET_RADIUS = 40
    SNIPPET_LEN = SNIPPET_RADIUS * 2 + len(query)

    # ── Phase 1: tsvector prefix search (0-5ms via GIN index) ──
    # Sanitize query for to_tsquery: remove special chars, join words with &
    _tsq_words = query.split()
    _tsq_safe = ' & '.join(
        w.replace("'", "''").replace('\\', '\\\\').replace(':', '')
        for w in _tsq_words if w
    )
    if _tsq_safe:
        _tsq_safe += ':*'  # prefix matching

    result_ids = []
    if _tsq_safe:
        try:
            rows = db.execute(
                """SELECT id FROM conversations
                   WHERE user_id=? AND search_tsv @@ to_tsquery('simple', ?)
                   ORDER BY updated_at DESC LIMIT ?""",
                (DEFAULT_USER_ID, _tsq_safe, MAX_RESULTS)
            ).fetchall()
            result_ids = [r['id'] for r in rows]
        except Exception as e:
            logger.debug('[search_convs] tsvector query failed (will fallback): %s', e)

    # ── Phase 2: ILIKE fallback for substring matches tsvector misses ──
    if len(result_ids) < MAX_RESULTS:
        _like_pattern = '%' + query.replace('%', '\\%').replace('_', '\\_') + '%'
        remaining = MAX_RESULTS - len(result_ids)
        try:
            if result_ids:
                placeholders = ','.join(['?'] * len(result_ids))
                rows = db.execute(
                    f"""SELECT id FROM conversations
                        WHERE user_id=? AND left(search_text, 10000) ILIKE ?
                          AND id NOT IN ({placeholders})
                        ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, *result_ids, remaining)
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id FROM conversations
                       WHERE user_id=? AND left(search_text, 10000) ILIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, remaining)
                ).fetchall()
            result_ids.extend(r['id'] for r in rows)
        except Exception as e:
            logger.warning('[search_convs] ILIKE fallback failed: %s', e)

    if not result_ids:
        elapsed = time.monotonic() - t0
        logger.debug('[search_convs] query=%r, results=0, elapsed=%.3fs', query, elapsed)
        return jsonify([])

    # ── Extract snippets for matched conversations ──
    placeholders = ','.join(['?'] * len(result_ids))
    snippet_rows = db.execute(
        f"""SELECT id,
                   substring(search_text
                             FROM greatest(1, position(? IN lower(search_text)) - ?)
                             FOR ?) AS snippet
            FROM conversations
            WHERE id IN ({placeholders})""",
        (query, SNIPPET_RADIUS, SNIPPET_LEN, *result_ids)
    ).fetchall()

    snippet_map = {}
    for r in snippet_rows:
        snip = (r['snippet'] or '').replace('\n', ' ').strip()
        if snip:
            snip = '…' + snip + '…'
        snippet_map[r['id']] = snip

    results = [
        {
            'id': cid,
            'matchField': 'content',
            'matchSnippet': snippet_map.get(cid, ''),
            'matchRole': 'assistant',
        }
        for cid in result_ids
    ]

    elapsed = time.monotonic() - t0
    logger.debug('[search_convs] query=%r, results=%d, elapsed=%.3fs',
                 query, len(results), elapsed)
    return jsonify(results)

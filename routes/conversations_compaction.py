"""routes/conversations_compaction.py — Compaction archive viewer endpoints.

Extracted from ``routes/conversations.py``. Both routes register on the
same ``conversations_bp`` Blueprint via side-effect import in
``routes/__init__.py``.
"""

import json

from flask import jsonify

from lib.database import DOMAIN_CHAT, get_db
from lib.log import get_logger
from lib.api_response import api_not_found
from routes.common import _db_safe
from routes.conversations import conversations_bp

logger = get_logger(__name__)


@conversations_bp.route('/api/v1/conversations/<conv_id>/compactions', methods=['GET'])
@_db_safe
def list_compactions(conv_id):
    """List compaction archives for a conversation (metadata only).

    Returns one row per compaction event in chronological order — both
    L3 force-compact (trigger='force') and emergency reactive_compact
    (trigger='reactive') flows. The full pre-compaction messages payload
    is intentionally NOT included here: it can be megabytes for long
    conversations, so we lazy-load it from
    ``GET /api/conversations/<id>/compactions/<archive_id>`` only when
    the user expands the corresponding marker.
    """
    db = get_db(DOMAIN_CHAT)
    try:
        rows = db.execute(
            'SELECT id, conv_id, summary, created_at, trigger, task_id, '
            'round_num, model, tokens_before, tokens_after, msgs_before, '
            'msgs_after, reason, length(messages_json) AS payload_size '
            'FROM transcript_archive WHERE conv_id=? '
            'ORDER BY created_at ASC, id ASC',
            (conv_id,),
        ).fetchall()
    except Exception as e:
        # Older installs that haven't migrated yet — fall back to the
        # legacy column set so the route doesn't 500.
        logger.warning('[Compactions] Full-schema query failed (%s) — '
                       'falling back to legacy columns', e)
        try:
            rows = db.execute(
                'SELECT id, conv_id, summary, created_at, '
                "'' AS trigger, '' AS task_id, 0 AS round_num, '' AS model, "
                "0 AS tokens_before, 0 AS tokens_after, 0 AS msgs_before, "
                "0 AS msgs_after, '' AS reason, "
                'length(messages_json) AS payload_size '
                'FROM transcript_archive WHERE conv_id=? '
                'ORDER BY created_at ASC, id ASC',
                (conv_id,),
            ).fetchall()
        except Exception as e2:
            logger.error('[Compactions] Legacy-fallback query failed: %s',
                         e2, exc_info=True)
            return jsonify({'compactions': [], 'error': 'query_failed'}), 200

    out = []
    for r in rows:
        out.append({
            'id':            r['id'],
            'convId':        r['conv_id'],
            'createdAt':     r['created_at'],
            'trigger':       r['trigger'] or 'force',
            'taskId':        r['task_id'] or '',
            'roundNum':      r['round_num'] or 0,
            'model':         r['model'] or '',
            'tokensBefore':  r['tokens_before'] or 0,
            'tokensAfter':   r['tokens_after'] or 0,
            'msgsBefore':    r['msgs_before'] or 0,
            'msgsAfter':     r['msgs_after'] or 0,
            'reason':        r['reason'] or '',
            'payloadSize':   r['payload_size'] or 0,
            'summaryPreview': (r['summary'] or '')[:240],
            'hasSummary':    bool(r['summary']),
        })
    logger.info('[Compactions] conv=%s returned %d archives',
                conv_id[:8], len(out))
    return jsonify({'compactions': out, 'count': len(out)})


@conversations_bp.route('/api/v1/conversations/<conv_id>/compactions/<int:archive_id>',
                        methods=['GET'])
@_db_safe
def get_compaction(conv_id, archive_id):
    """Lazy-load the full pre-compaction message list for one archive.

    Payload can be very large (multiple MB for image-heavy
    conversations) — clients should fetch on-demand only when the user
    expands the marker.

    Returns:
        ``{archive: {...metadata...}, messages: [...]}``
    """
    db = get_db(DOMAIN_CHAT)
    r = db.execute(
        'SELECT id, conv_id, messages_json, summary, created_at, '
        'trigger, task_id, round_num, model, tokens_before, tokens_after, '
        'msgs_before, msgs_after, reason '
        'FROM transcript_archive WHERE id=? AND conv_id=?',
        (archive_id, conv_id),
    ).fetchone()
    if not r:
        # Try legacy schema (no metadata columns)
        try:
            r = db.execute(
                'SELECT id, conv_id, messages_json, summary, created_at '
                'FROM transcript_archive WHERE id=? AND conv_id=?',
                (archive_id, conv_id),
            ).fetchone()
        except Exception as e:
            logger.debug('[Compaction] Legacy fetch fallback failed: %s', e)
            r = None
    if not r:
        return api_not_found('Not found')

    try:
        messages = json.loads(r['messages_json']) if r['messages_json'] else []
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Compaction] Invalid messages_json in archive id=%s: %s',
                       archive_id, e)
        messages = []

    def _maybe(col, default=''):
        try:
            return r[col]
        except (IndexError, KeyError) as _e_audit:
            logger.debug('[conversations_compaction] _maybe caught %s: %s', type(_e_audit).__name__, _e_audit)
            return default

    archive_meta = {
        'id':            r['id'],
        'convId':        r['conv_id'],
        'createdAt':     r['created_at'],
        'trigger':       _maybe('trigger', 'force') or 'force',
        'taskId':        _maybe('task_id', '') or '',
        'roundNum':      _maybe('round_num', 0) or 0,
        'model':         _maybe('model', '') or '',
        'tokensBefore':  _maybe('tokens_before', 0) or 0,
        'tokensAfter':   _maybe('tokens_after', 0) or 0,
        'msgsBefore':    _maybe('msgs_before', 0) or 0,
        'msgsAfter':     _maybe('msgs_after', 0) or 0,
        'reason':        _maybe('reason', '') or '',
        'summary':       r['summary'] or '',
        'messagesCount': len(messages),
    }
    logger.info('[Compaction] conv=%s archive=%d messages=%d size=%dKB',
                conv_id[:8], archive_id, len(messages),
                len(r['messages_json'] or '') // 1024)
    return jsonify({'archive': archive_meta, 'messages': messages})

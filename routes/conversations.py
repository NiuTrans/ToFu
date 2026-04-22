"""routes/conversations.py — Conversation CRUD endpoints."""

import json
import re
import time

import sqlite3
from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_db, json_dumps_pg
from lib.log import audit_log, get_logger
from lib.utils import safe_json as _safe_json
from routes.common import DEFAULT_USER_ID, _db_safe, _invalidate_meta_cache, _refresh_meta_cache_if_stale

# Whitelisted keys for PATCH /messages/<idx> — only these fields can be mutated
# in-place on a single message without writing the whole conversation.
_PATCH_MSG_WHITELIST = {
    'content', 'originalContent', 'images', 'pdfTexts', 'replyQuotes',
    '_showingTranslation', 'translatedContent',
    '_translateModel', '_translateDone', '_translateTaskId', '_translateField',
    '_translateError', '_translatedCache', '_originalContent',
    'timestamp',
}

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



@conversations_bp.route('/api/conversations/<conv_id>/debug-messages', methods=['GET'])
@_db_safe
def debug_messages(conv_id):
    """Return API-ready messages for the debug panel.

    Uses the server-side ``build_api_messages_from_db`` to produce the exact
    messages that the LLM would see — replacing the deprecated frontend
    ``buildApiMessages()`` fallback.
    """
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    system_prompt = request.args.get('systemPrompt', '')
    config = {'systemPrompt': system_prompt}
    try:
        messages = build_api_messages_from_db(conv_id, config)
        if messages is None:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'messages': messages, 'count': len(messages)})
    except Exception as e:
        logger.error('[debug_messages] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500


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
        return jsonify({'error': 'internal_error'}), 500


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

    # ── Guard: prevent stale syncs from overwriting newer data ──
    # A frontend sync captured lightMsgs before an await; by the time the PUT
    # arrives, a fresher sync with MORE messages may have already completed.
    # Reject PUTs with fewer messages unless the client explicitly signals
    # truncation (e.g. regen/edit sends allowTruncate=true).
    allow_truncate = data.get('allowTruncate', False)
    existing_row = db.execute(
        'SELECT msg_count FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    existing_count = existing_row[0] if existing_row else 0

    if msg_count == 0 and existing_count > 0:
        logger.warning('[save_conv] ⚠️ BLOCKED overwrite of conv %s — '
                       'server has %d msgs but client sent 0. '
                       'This is likely a race condition.',
                       conv_id[:12], existing_count)
        return jsonify({'ok': False, 'error': 'blocked_empty_overwrite',
                        'serverMsgCount': existing_count}), 409

    if msg_count > 0 and msg_count < existing_count and not allow_truncate:
        logger.warning('[save_conv] ⚠️ BLOCKED regression of conv %s — '
                       'server has %d msgs but client sent %d (delta=%d). '
                       'This is a stale sync from a concurrent async callback '
                       '(e.g. translate poll). Set allowTruncate=true for '
                       'intentional truncation (regen/edit).',
                       conv_id[:12], existing_count, msg_count,
                       existing_count - msg_count)
        return jsonify({'ok': False, 'error': 'blocked_msg_regression',
                        'serverMsgCount': existing_count,
                        'clientMsgCount': msg_count}), 409

    # ── Guard: prevent stale streaming checkpoint from overwriting completed result ──
    # Root cause: VS Code port forwarding can reload the page at the exact moment
    # the backend _sync_result_to_conversation writes complete data (finishReason,
    # usage, full content).  The frontend's IDB cache has a stale streaming snapshot
    # and PUTs it back, erasing the completed result.
    # Fix: if server has a completed assistant message (finishReason set) but client
    # is sending one without finishReason AND with less content, block the overwrite.
    if msg_count > 0 and msg_count == existing_count and not allow_truncate:
        incoming_last = raw_messages[-1] if raw_messages else {}
        if incoming_last.get('role') == 'assistant' and not incoming_last.get('finishReason'):
            try:
                existing_msgs_row = db.execute(
                    'SELECT messages FROM conversations WHERE id=? AND user_id=?',
                    (conv_id, DEFAULT_USER_ID)
                ).fetchone()
                if existing_msgs_row:
                    existing_msgs = json.loads(existing_msgs_row[0] or '[]')
                    if existing_msgs:
                        existing_last = existing_msgs[-1]
                        existing_fr = existing_last.get('finishReason')
                        if (existing_last.get('role') == 'assistant'
                                and existing_fr
                                and existing_fr not in ('', 'interrupted')
                                and len(existing_last.get('content') or '') > len(incoming_last.get('content') or '')):
                            logger.warning(
                                '[save_conv] ⚠️ BLOCKED stale-checkpoint overwrite of conv %s — '
                                'server has completed assistant msg (finishReason=%s, content=%d chars) '
                                'but client sent incomplete snapshot (no finishReason, content=%d chars). '
                                'This is likely a stale IDB cache sync after page reload.',
                                conv_id[:12], existing_fr,
                                len(existing_last.get('content') or ''),
                                len(incoming_last.get('content') or ''))
                            return jsonify({
                                'ok': False,
                                'error': 'blocked_stale_checkpoint',
                                'serverMsgCount': existing_count,
                            }), 409
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[save_conv] Content regression check parse error: %s', e)

    # ── Preserve server-side translation fields against frontend overwrite ──
    # Root cause (endpoint mode auto-translate bug): backend
    # _trigger_endpoint_auto_translate spawns N translate threads that write
    # translatedContent into the DB while the frontend is unaware.  When
    # finishStream then calls syncConversationToServer() it PUTs a snapshot
    # of the in-memory messages (no translatedContent), and this
    # INSERT OR REPLACE wipes the backend commits.
    #
    # Fix: before the overwrite, read the existing DB messages and merge back
    # the translation fields for any matching message where the incoming
    # snapshot has no translation but the DB does.  Guard with strict
    # content+marker identity so we don't resurrect stale translations onto
    # edited messages.  Skip entirely when allowTruncate=true (edit/regen
    # intentionally rewrites).
    _TRANSLATE_PRESERVE_KEYS = (
        'translatedContent',
        '_showingTranslation',
        '_translateDone',
        '_translateModel',
        '_translateField',
        '_translatedCache',
        'originalContent',
    )
    _preserved_total = 0
    _preserved_per_role = {}
    _lost_total = 0
    if msg_count > 0 and not allow_truncate:
        try:
            _merge_row = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)
            ).fetchone()
            if _merge_row:
                try:
                    _db_msgs = json.loads(_merge_row[0] or '[]') or []
                except (json.JSONDecodeError, TypeError) as _je:
                    logger.warning('[save_conv] Failed to parse existing messages '
                                   'for translation merge conv=%s: %s',
                                   conv_id[:12], _je)
                    _db_msgs = []

                # Iterate up to the overlap; messages BEYOND the incoming
                # length are naturally dropped (caller intent: regen/edit
                # shortened the tail without setting allowTruncate).
                _overlap = min(len(raw_messages), len(_db_msgs))
                for _i in range(_overlap):
                    _dst = raw_messages[_i]
                    _src = _db_msgs[_i]
                    if not isinstance(_dst, dict) or not isinstance(_src, dict):
                        continue
                    _src_tc = _src.get('translatedContent')
                    if not _src_tc:
                        continue  # nothing preserved on server side
                    if _dst.get('translatedContent'):
                        continue  # client already has translation — don't overwrite

                    # Identity check: preserve only when the incoming message
                    # clearly points at the SAME underlying message.  We use
                    # content byte-identity as the strong signal, plus a
                    # relaxed branch that matches role + endpoint markers when
                    # contents are non-empty and equal in length tier (avoids
                    # resurrecting translations on post-hoc edits).
                    _role_ok = _dst.get('role') == _src.get('role')
                    _marker_ok = (
                        bool(_dst.get('_isEndpointPlanner')) == bool(_src.get('_isEndpointPlanner'))
                        and bool(_dst.get('_isEndpointReview')) == bool(_src.get('_isEndpointReview'))
                        and _dst.get('_epIteration') == _src.get('_epIteration')
                    )
                    _content_ok = (
                        isinstance(_dst.get('content'), str)
                        and isinstance(_src.get('content'), str)
                        and _dst.get('content') == _src.get('content')
                    )
                    if not (_role_ok and _marker_ok and _content_ok):
                        # Content mismatch — treat as a genuine edit and let
                        # the safety-net re-translate the new content.
                        _lost_total += 1
                        continue

                    # Don't pollute image-gen messages with stale translations
                    if _dst.get('_igResult') or _dst.get('_isImageGen'):
                        continue

                    # Merge the preserved keys in-place on the incoming dict
                    for _k in _TRANSLATE_PRESERVE_KEYS:
                        if _k in _src and _k not in _dst:
                            _dst[_k] = _src[_k]
                    _preserved_total += 1
                    _tag = 'planner' if _dst.get('_isEndpointPlanner') else (
                        'critic' if _dst.get('_isEndpointReview') else (
                            f"worker#{_dst.get('_epIteration')}" if _dst.get('_epIteration')
                            else (_dst.get('role') or 'other')
                        )
                    )
                    _preserved_per_role[_tag] = _preserved_per_role.get(_tag, 0) + 1

                # Count lost (tail truncation without allowTruncate): these
                # could also contain translations the server persisted.
                if len(_db_msgs) > len(raw_messages):
                    for _i in range(len(raw_messages), len(_db_msgs)):
                        _src = _db_msgs[_i]
                        if isinstance(_src, dict) and _src.get('translatedContent'):
                            _lost_total += 1

                if _preserved_total > 0:
                    # Re-materialize the messages payload so the INSERT below
                    # actually writes the merged translations.
                    messages = json_dumps_pg(raw_messages)
                    logger.info(
                        '[save_conv] 🈯 Preserved %d translatedContent entries '
                        'from DB into incoming payload conv=%s (by role=%s)',
                        _preserved_total, conv_id[:12], _preserved_per_role,
                    )
                if _lost_total > 0:
                    logger.warning(
                        '[save_conv] ⚠️ translatedContent loss conv=%s — '
                        '%d msg(s) lost translation (content mismatch or '
                        'tail-truncated without allowTruncate=true). '
                        'Preserved=%d.',
                        conv_id[:12], _lost_total, _preserved_total,
                    )
        except Exception as _me:
            logger.warning('[save_conv] translation-merge pre-step failed '
                           'conv=%s: %s (continuing without merge)',
                           conv_id[:12], _me, exc_info=True)

    if msg_count == 0:
        logger.info('[save_conv] Conv %s — saving with 0 messages (new/empty conv)',
                    conv_id[:12])
    else:
        logger.info('[save_conv] Conv %s — saving %d messages, title=%s '
                    '(preserved_translations=%d, lost_translations=%d)',
                    conv_id[:12], msg_count, repr(title[:50]),
                    _preserved_total, _lost_total)
    search_text = build_search_text(raw_messages)
    db_execute_with_retry(
        db,
        '''INSERT OR REPLACE INTO conversations (id, user_id, title, messages, created_at, updated_at, settings, msg_count, search_text)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (conv_id, DEFAULT_USER_ID, title, messages, created, updated, settings, msg_count, search_text)
    )
    # Update FTS5 index
    if search_text:
        try:
            db.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (search_text, conv_id)
            )
            db.commit()
        except Exception as e:
            logger.debug('[save_conv] FTS update failed (non-fatal): %s', e)
    _invalidate_meta_cache()
    return jsonify({'ok': True})



@conversations_bp.route('/api/conversations/<conv_id>/settings', methods=['PATCH'])
@_db_safe
def patch_conv_settings(conv_id):
    """Lightweight endpoint to merge new keys into a conversation's settings JSON.

    Unlike PUT (which requires full messages), this only touches the settings
    column — safe to call for shell conversations that haven't loaded messages.

    Body: { folderId?: str|null, pinned?: bool, ... }
    All keys in the body are merged into the existing settings dict.
    """
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'error': 'No settings provided'}), 400

    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    settings = _safe_json(row['settings'], default={}, label='patch_settings')
    settings.update(data)
    settings_json = json.dumps(settings, ensure_ascii=False)

    db_execute_with_retry(
        db,
        'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
        (settings_json, conv_id, DEFAULT_USER_ID)
    )
    _invalidate_meta_cache()
    logger.info('[patch_settings] Conv %s — patched keys: %s', conv_id[:12], list(data.keys()))
    return jsonify({'ok': True})


@conversations_bp.route('/api/conversations/<conv_id>/messages/<int:msg_idx>', methods=['DELETE'])
@_db_safe
def delete_message(conv_id, msg_idx):
    """Delete a specific message (or a user+assistant turn) from a conversation.

    Query params:
        mode: 'single' — delete only the message at msg_idx (default)
              'turn'   — if msg_idx is a user message, also delete the next
                         assistant message (the full turn)

    Returns:
        { ok: true, msgCount: int, deletedIndices: [int, ...] }
    """
    mode = request.args.get('mode', 'single')
    if mode not in ('single', 'turn'):
        return jsonify({'error': 'mode must be "single" or "turn"'}), 400

    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[delete_message] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
        return jsonify({'error': 'Failed to parse conversation messages'}), 500

    if msg_idx < 0 or msg_idx >= len(messages):
        return jsonify({'error': f'Index {msg_idx} out of range (0..{len(messages) - 1})'}), 400

    # Determine which indices to delete
    deleted_indices = [msg_idx]
    target_msg = messages[msg_idx]

    if mode == 'turn' and target_msg.get('role') == 'user':
        # Also delete the following assistant message if it exists
        if msg_idx + 1 < len(messages) and messages[msg_idx + 1].get('role') == 'assistant':
            deleted_indices.append(msg_idx + 1)

    # Remove messages in reverse order to preserve indices
    for i in sorted(deleted_indices, reverse=True):
        messages.pop(i)

    # Persist
    title = row['title']
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)
    search_text = build_search_text(messages)

    # Merge settings — preserve existing, update lastMsg metadata
    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError):
        settings = {}
    if messages:
        last = messages[-1]
        settings['lastMsgRole'] = last.get('role')
        settings['lastMsgTimestamp'] = last.get('timestamp')
    else:
        settings.pop('lastMsgRole', None)
        settings.pop('lastMsgTimestamp', None)
    settings_json = json.dumps(settings, ensure_ascii=False)

    # Preserve original created_at
    existing = db.execute(
        'SELECT created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    created_at = existing['created_at'] if existing else now_ms

    db_execute_with_retry(db, '''
        INSERT OR REPLACE INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                   settings, msg_count, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (conv_id, DEFAULT_USER_ID, title, messages_json, created_at, now_ms,
          settings_json, len(messages), search_text))

    # Update FTS5 index
    if search_text:
        try:
            db.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (search_text, conv_id)
            )
            db.commit()
        except Exception as e:
            logger.debug('[delete_message] FTS update failed (non-fatal): %s', e)

    _invalidate_meta_cache()
    # Invalidate persisted per-day cost cache — the deleted message may have
    # had a usage dict that contributed to a past day's total.
    try:
        from routes.daily_report import invalidate_day_cost_cache
        invalidate_day_cost_cache()
    except Exception as e:
        logger.debug('[delete_message] day-cost cache invalidation skipped: %s', e)
    logger.info('[delete_message] conv=%s deleted indices=%s mode=%s remaining=%d',
                conv_id[:8], deleted_indices, mode, len(messages))

    return jsonify({
        'ok': True,
        'msgCount': len(messages),
        'deletedIndices': deleted_indices,
    })


@conversations_bp.route('/api/conversations/<conv_id>/messages/<int:msg_idx>', methods=['PATCH'])
@_db_safe
def patch_message(conv_id, msg_idx):
    """Targeted single-message mutation for chatInner actions (edit-only,
    translation-visibility toggle, per-message metadata updates).

    Only whitelisted keys (see ``_PATCH_MSG_WHITELIST``) may be merged —
    arbitrary fields are rejected so this endpoint cannot be used to
    bypass the role/structure invariants enforced by ``save_conv``.

    Body: JSON dict with any subset of whitelisted keys.  Special sentinel
    value ``null`` for a key removes that key from the message.

    Returns:
        {ok, msgCount, msg}
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data:
        return jsonify({'error': 'empty_patch'}), 400

    # Reject any key outside the whitelist — refuse silently and tell caller.
    unknown = [k for k in data.keys() if k not in _PATCH_MSG_WHITELIST]
    if unknown:
        logger.warning('[patch_msg] conv=%s idx=%d REJECTED non-whitelisted keys: %s',
                       conv_id[:8], msg_idx, unknown)
        return jsonify({'error': 'unsupported_keys', 'keys': unknown}), 400

    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[patch_msg] conv=%s failed to parse messages: %s', conv_id[:8], e)
        return jsonify({'error': 'Failed to parse conversation messages'}), 500

    if msg_idx < 0 or msg_idx >= len(messages):
        logger.warning('[patch_msg] conv=%s idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, len(messages))
        return jsonify({'error': f'Index {msg_idx} out of range (0..{len(messages) - 1})'}), 400

    msg = messages[msg_idx]
    if not isinstance(msg, dict):
        return jsonify({'error': 'Target message is not a dict'}), 500

    # Apply whitelisted merge. A literal None value deletes the key (lets
    # the frontend clear originalContent after a plain edit).
    applied_keys = []
    for key, value in data.items():
        if value is None:
            if key in msg:
                msg.pop(key, None)
                applied_keys.append('-' + key)
        else:
            msg[key] = value
            applied_keys.append(key)

    # Preserve invariants: if content changed, log a short preview.
    _preview = ''
    if 'content' in data and isinstance(data['content'], str):
        _preview = data['content'][:50]

    # Persist — reuse the same pattern as delete_message/save_conv.
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)
    search_text = build_search_text(messages)

    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError):
        settings = {}
    # Keep lastMsgRole/lastMsgTimestamp in sync with current tail.
    if messages:
        last = messages[-1]
        settings['lastMsgRole'] = last.get('role')
        settings['lastMsgTimestamp'] = last.get('timestamp')
    settings_json = json.dumps(settings, ensure_ascii=False)

    existing = db.execute(
        'SELECT created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    created_at = existing['created_at'] if existing else now_ms
    title = row['title']

    db_execute_with_retry(db, '''
        INSERT OR REPLACE INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                   settings, msg_count, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (conv_id, DEFAULT_USER_ID, title, messages_json, created_at, now_ms,
          settings_json, len(messages), search_text))

    if search_text:
        try:
            db.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (search_text, conv_id)
            )
            db.commit()
        except Exception as e:
            logger.debug('[patch_msg] FTS update failed (non-fatal): %s', e)

    _invalidate_meta_cache()
    logger.info('[patch_msg] conv=%s idx=%d keys=%s preview=%.50s',
                conv_id[:8], msg_idx, applied_keys, _preview)
    try:
        audit_log('msg_patch', conv_id=conv_id, idx=msg_idx, keys=applied_keys)
    except Exception as e:
        logger.debug('[patch_msg] audit_log failed (non-fatal): %s', e)

    return jsonify({
        'ok': True,
        'msgCount': len(messages),
        'msg': msg,
    })


@conversations_bp.route(
    '/api/conversations/<conv_id>/messages/<int:msg_idx>/branches/<int:branch_idx>',
    methods=['DELETE'],
)
@_db_safe
def delete_branch(conv_id, msg_idx, branch_idx):
    """Delete a single branch entry from ``messages[msg_idx].branches``.

    The branch index is positional — after deletion, callers must re-index
    the remaining branches on their side (the DOM remap in ``branch.js``).

    Returns:
        {ok, branchCount}
    """
    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[delete_branch] conv=%s failed to parse messages: %s', conv_id[:8], e)
        return jsonify({'error': 'Failed to parse conversation messages'}), 500

    if msg_idx < 0 or msg_idx >= len(messages):
        logger.warning('[delete_branch] conv=%s msg_idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, len(messages))
        return jsonify({'error': f'msg_idx {msg_idx} out of range'}), 400

    msg = messages[msg_idx]
    branches = msg.get('branches') if isinstance(msg, dict) else None
    if not isinstance(branches, list):
        logger.warning('[delete_branch] conv=%s msg_idx=%d has no branches',
                       conv_id[:8], msg_idx)
        return jsonify({'error': 'Message has no branches'}), 400
    if branch_idx < 0 or branch_idx >= len(branches):
        logger.warning('[delete_branch] conv=%s msg_idx=%d branch_idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, branch_idx, len(branches))
        return jsonify({'error': f'branch_idx {branch_idx} out of range (0..{len(branches) - 1})'}), 400

    branches.pop(branch_idx)
    if not branches:
        msg.pop('branches', None)
    branch_count = len(branches)

    # Persist
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)
    search_text = build_search_text(messages)

    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError):
        settings = {}
    settings_json = json.dumps(settings, ensure_ascii=False)

    existing = db.execute(
        'SELECT created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    created_at = existing['created_at'] if existing else now_ms
    title = row['title']

    db_execute_with_retry(db, '''
        INSERT OR REPLACE INTO conversations (id, user_id, title, messages, created_at, updated_at,
                                   settings, msg_count, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (conv_id, DEFAULT_USER_ID, title, messages_json, created_at, now_ms,
          settings_json, len(messages), search_text))

    if search_text:
        try:
            db.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (search_text, conv_id)
            )
            db.commit()
        except Exception as e:
            logger.debug('[delete_branch] FTS update failed (non-fatal): %s', e)

    _invalidate_meta_cache()
    logger.info('[delete_branch] conv=%s msg_idx=%d branch_idx=%d remaining=%d',
                conv_id[:8], msg_idx, branch_idx, branch_count)
    try:
        audit_log('branch_delete', conv_id=conv_id, msg_idx=msg_idx,
                  branch_idx=branch_idx, remaining=branch_count)
    except Exception as e:
        logger.debug('[delete_branch] audit_log failed (non-fatal): %s', e)

    return jsonify({'ok': True, 'branchCount': branch_count})


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
        _is_db_err = isinstance(exc, sqlite3.OperationalError)
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
    # Invalidate persisted per-day cost cache — a deleted conv may have
    # contributed to any number of past days, so clear everything and let
    # the next calendar render re-fill.
    try:
        from routes.daily_report import invalidate_day_cost_cache
        invalidate_day_cost_cache()
    except Exception as e:
        logger.debug('[delete_conv] day-cost cache invalidation skipped: %s', e)
    logger.info('[delete_conv] Deleted conv %s (rows: conv=%d, tasks=%d, transcripts=%d)',
                conv_id[:12], c1.rowcount, c2.rowcount, c3.rowcount)
    return jsonify({'ok': True})


@conversations_bp.route('/api/conversations/search', methods=['GET'])
def search_convs():
    """Server-side full-text search through conversation messages.

    Two-phase approach:
      Phase 1: FTS5 MATCH for tokenized word matching (fast via inverted index).
      Phase 2: If <50 results, LIKE fallback on search_text to catch
               substring matches that FTS5 tokenization misses.

    Snippets are extracted in Python from the final result set (max 50 rows).
    """
    query = (request.args.get('q') or '').strip().lower()
    if not query or len(query) < 2:
        return jsonify([])

    t0 = time.monotonic()
    db = get_db(DOMAIN_CHAT)

    MAX_RESULTS = 50
    SNIPPET_RADIUS = 40

    # ── Phase 1: FTS5 MATCH search ──
    # Sanitize query for FTS5: remove special chars, add * for prefix matching
    _fts_words = re.sub(r'[^\w\s]', '', query, flags=re.UNICODE).split()
    _fts_query = ' '.join(f'{w}*' for w in _fts_words if w)

    result_ids = []
    if _fts_query:
        try:
            rows = db.execute(
                """SELECT c.id FROM conversations c
                   JOIN conversations_fts f ON f.rowid = c.rowid
                   WHERE c.user_id=? AND f.search_text MATCH ?
                   ORDER BY c.updated_at DESC LIMIT ?""",
                (DEFAULT_USER_ID, _fts_query, MAX_RESULTS)
            ).fetchall()
            result_ids = [r['id'] for r in rows]
        except Exception as e:
            logger.debug('[search_convs] FTS5 query failed (will fallback): %s', e)

    # ── Phase 2: LIKE fallback for substring matches FTS5 misses ──
    if len(result_ids) < MAX_RESULTS:
        _like_pattern = '%' + query.replace('%', '\\%').replace('_', '\\_') + '%'
        remaining = MAX_RESULTS - len(result_ids)
        try:
            if result_ids:
                placeholders = ','.join(['?'] * len(result_ids))
                rows = db.execute(
                    f"""SELECT id FROM conversations
                        WHERE user_id=? AND lower(search_text) LIKE ?
                          AND id NOT IN ({placeholders})
                        ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, *result_ids, remaining)
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id FROM conversations
                       WHERE user_id=? AND lower(search_text) LIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (DEFAULT_USER_ID, _like_pattern, remaining)
                ).fetchall()
            result_ids.extend(r['id'] for r in rows)
        except Exception as e:
            logger.warning('[search_convs] LIKE fallback failed: %s', e)

    if not result_ids:
        elapsed = time.monotonic() - t0
        logger.debug('[search_convs] query=%r, results=0, elapsed=%.3fs', query, elapsed)
        return jsonify([])

    # ── Extract snippets in Python (portable — no PG substring/position) ──
    placeholders = ','.join(['?'] * len(result_ids))
    snippet_rows = db.execute(
        f"SELECT id, search_text FROM conversations WHERE id IN ({placeholders})",
        tuple(result_ids)
    ).fetchall()

    snippet_map = {}
    for r in snippet_rows:
        text = r['search_text'] or ''
        pos = text.lower().find(query)
        if pos >= 0:
            start = max(0, pos - SNIPPET_RADIUS)
            end = min(len(text), pos + len(query) + SNIPPET_RADIUS)
            snip = text[start:end].replace('\n', ' ').strip()
            if snip:
                snip = '…' + snip + '…'
            snippet_map[r['id']] = snip
        else:
            snippet_map[r['id']] = ''

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

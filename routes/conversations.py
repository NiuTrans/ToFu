"""routes/conversations.py — Conversation CRUD endpoints."""

import json
import time

import sqlite3
from flask import Response, jsonify, request

from lib.database import (
    DOMAIN_CHAT,
    async_execute,
    async_fetchall,
    async_fetchone,
    db_execute_with_retry,
    json_dumps_pg,
    run_pooled,
)
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.request_parser import async_parse_body, parse_body  # noqa: F401
from lib.utils import safe_json as _safe_json
from lib.conversations import build_search_text, update_conversation_fts  # noqa: F401  — build_search_text re-exported for back-compat callers
from lib.database._core_schema import CONVERSATIONS, upsert

# Columns written by the conversation upserts in this module. Omits `search_tsv`
# (PG-only; the conversations_search_tsv_trg BEFORE-trigger derives it from
# search_text) — so the partial insert lets the trigger own that column.
_CONV_INSERT_COLS = ['id', 'user_id', 'title', 'messages', 'created_at',
                     'updated_at', 'settings', 'msg_count', 'search_text']
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

from routes.api_v1 import api_v1_conversations_bp as conversations_bp  # noqa: E402
# (alias kept for back-compat with `from routes.conversations import conversations_bp` callers)


# ── Deferred response protocol for run_pooled blocking bodies ──────────
# A *_blocking(db, ...) function runs in the DB executor thread where Quart's
# (async-only) app context is absent, so it MUST NOT call jsonify / api_* —
# those build a Response and need app context. Instead it returns a `_Defer`
# describing the response, and the async wrapper calls `_finish(...)` on the
# loop thread to materialize it (helper semantics — envelope, status — preserved).
class _Defer:
    __slots__ = ('helper', 'args', 'kwargs', 'status')

    def __init__(self, helper, *args, status=None, **kwargs):
        self.helper = helper
        self.args = args
        self.kwargs = kwargs
        self.status = status


def _finish(result):
    """Materialize a ``_Defer`` (or pass through a plain Response/value) on the
    loop thread, where Quart's app context exists."""
    if isinstance(result, _Defer):
        resp = result.helper(*result.args, **result.kwargs)
        if result.status is not None:
            return resp, result.status
        return resp
    return result


# Thread-safe response builders for *_blocking bodies — they return a _Defer
# (no Response constructed in the executor thread); _finish materializes them
# on the loop thread. Use these INSTEAD of api_*/jsonify inside *_blocking fns.
def _ok(*a, **k):
    return _Defer(api_ok, *a, **k)


def _nf(*a, **k):
    return _Defer(api_not_found, *a, **k)


def _br(*a, **k):
    return _Defer(api_bad_request, *a, **k)


def _ie(*a, **k):
    return _Defer(api_internal_error, *a, **k)


def _json(payload, status=None):
    return _Defer(jsonify, payload, status=status)


def _conv_row_to_dict(r):
    """Convert a DB row (with messages column) to a conversation dict."""
    return {
        'id': r['id'], 'title': r['title'],
        'messages': _safe_json(r['messages'], default=[], label='messages'),
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _safe_json(r['settings'], default=None, label='settings'),
    }


@conversations_bp.route('/api/v1/conversations', methods=['GET'])
@_db_safe
async def list_convs():
    """List the user's conversations.

    Default: full conversations (id, title, messages, timestamps, settings).
    With ``?meta=1``: lightweight metadata only, served from an ETag-validated
    cache so the sidebar can refresh cheaply. ``?prefetch=<conv_id>`` adds the
    full payload of one specific conv to the meta response, saving one round-trip
    on tab switch.

    Native-async: the default full-list path uses the await-able DB facade.
    The ``?meta=1`` path delegates to the sync meta-cache helper (which needs a
    real pooled connection) via ``asyncio.to_thread`` so it never blocks the loop.
    """
    import asyncio
    meta_only = request.args.get('meta') == '1'
    prefetch_id = request.args.get('prefetch', '').strip()
    if meta_only:
        def _meta_branch():
            # Runs off-loop: borrow a pooled conn, compute meta + optional
            # prefetch, return plain data for the async handler to serialize.
            from lib.database._core import _pool_get, _pool_put
            db = _pool_get()
            try:
                payload, etag = _refresh_meta_cache_if_stale(db)
                prefetch_data = None
                if prefetch_id:
                    try:
                        r = db.execute(
                            'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE id=? AND user_id=?',
                            (prefetch_id, DEFAULT_USER_ID)
                        ).fetchone()
                        if r:
                            prefetch_data = _conv_row_to_dict(r)
                    except Exception as e:
                        logger.warning('[Common] prefetch conv %s failed: %s', prefetch_id[:12], e)
                return payload, etag, prefetch_data
            finally:
                _pool_put(db)

        payload, etag, prefetch_data = await asyncio.to_thread(_meta_branch)

        if prefetch_id:
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

    rows = await async_fetchall(
        'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE user_id=? ORDER BY updated_at DESC',
        (DEFAULT_USER_ID,), domain=DOMAIN_CHAT)
    convs = [_conv_row_to_dict(r) for r in rows]
    return jsonify(convs)


@conversations_bp.route('/api/v1/conversations/<conv_id>', methods=['GET'])
@_db_safe
async def get_conv(conv_id):
    """Fetch a single conversation with full messages.

    Native-async: uses the await-able DB facade (``async_fetchone``) so the
    query runs on the dedicated DB executor without blocking the event loop.
    """
    r = await async_fetchone(
        'SELECT id, title, messages, created_at, updated_at, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not r:
        return api_not_found('Not found')
    return jsonify(_conv_row_to_dict(r))



@conversations_bp.route('/api/v1/conversations/<conv_id>/debug-messages', methods=['GET'])
@_db_safe
async def debug_messages(conv_id):
    """Return API-ready messages for the debug panel.

    Uses the server-side ``build_api_messages_from_db`` to produce the exact
    messages that the LLM would see — replacing the deprecated frontend
    ``buildApiMessages()`` fallback.

    Base64 image data is stripped via ``_strip_base64_for_snapshot`` so the
    response stays under a few hundred KB even for image-heavy convs — same
    treatment the live ``messages_snapshot`` SSE gets.

    Native-async: the blocking ``build_api_messages_from_db`` (which uses its
    own thread-local DB connection) runs off-loop via ``asyncio.to_thread``.
    """
    import asyncio
    from lib.tasks_pkg.conv_message_builder import _load_messages_from_db
    from lib.tasks_pkg.manager import _strip_base64_for_snapshot
    from lib.tasks_pkg.wire_messages import build_wire_messages
    system_prompt = request.args.get('systemPrompt', '')
    config = {'systemPrompt': system_prompt}

    def _build():
        # WIRE-FORM, side-effect-free reconstruction (mode='snapshot'): runs
        # the SAME pipeline the live snapshot uses — _transform_messages →
        # _inject_system_contexts (throwaway task, empty conv_id) →
        # apply_wire_sanitize — so the cold panel matches the hot panel
        # byte-for-byte given the same provider context. The conv_id is passed
        # to build_wire_messages ONLY for the tool-result sort's cache-prefix
        # gate; inject still runs cache-isolated. Per-round memory/date are a
        # hypothetical first-round (the panel labels this an approximation).
        raw = _load_messages_from_db(conv_id)
        if raw is None:
            return None
        return build_wire_messages(raw, config, mode='snapshot', conv_id=conv_id)

    try:
        messages = await asyncio.to_thread(_build)
        if messages is None:
            return api_not_found('Not found')
        try:
            messages = _strip_base64_for_snapshot(messages)
        except Exception as e:
            logger.warning('[debug_messages] strip_base64 failed for conv=%s: %s — '
                           'returning raw messages', conv_id[:8], e)
        return jsonify({'messages': messages, 'count': len(messages), 'approx': True})
    except Exception as e:
        logger.error('[debug_messages] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


@conversations_bp.route('/api/v1/conversations/<conv_id>/export', methods=['GET'])
async def export_conv(conv_id):
    """Export a conversation as formatted plain-text for LLM injection.

    Native-async: ``get_conversation`` does blocking DB reads, so it runs
    off-loop via ``asyncio.to_thread``.
    """
    import asyncio
    from lib.conv_ref import get_conversation
    detail_param = (request.args.get('include_tool_details', '1')).lower()
    include_details = detail_param not in ('0', 'false', 'no')
    try:
        result = await asyncio.to_thread(
            get_conversation,
            conversation_id=conv_id,
            include_tool_details=include_details,
        )
        return api_ok({'text': result})
    except Exception as e:
        logger.error('[Common] get_conversation failed for conv_id=%s: %s', conv_id, e, exc_info=True)
        return api_internal_error('internal_error')


@conversations_bp.route('/api/v1/conversations/<conv_id>', methods=['PUT'])
@_db_safe
async def save_conv(conv_id):
    """Persist a conversation (full upsert).

    Body is ``{title, messages, createdAt?, updatedAt?, settings?, allowTruncate?}``.
    Stable per-message IDs are assigned via ``_assign_message_ids`` so future
    index-free addressing keeps working. Returns 409 with ``error='blocked_*'``
    when guards fire (stale concurrent sync producing 0 or fewer messages than
    the server has) — the client retries with fresh state. ``allowTruncate=true``
    bypasses the regression guard for intentional truncation (regen / edit).

    Native-async: body awaited, then the entire (unchanged) blocking DB body
    runs off-loop via ``run_pooled`` which hands it a pooled connection.
    """
    data = await async_parse_body()
    return _finish(await run_pooled(lambda db: _save_conv_blocking(db, conv_id, data)))


def _save_conv_blocking(db, conv_id, data):
    title = data.get('title', 'Untitled')
    raw_messages = data.get('messages', [])
    msg_count = len(raw_messages)
    # Backfill stable per-message IDs.  Once present, _msgId carries
    # forward in subsequent loads/syncs.  Index-free addressing depends
    # on every message having an id, so we assign on every write.
    try:
        from lib.tasks_pkg.manager import _assign_message_ids as _amid
        _amid(raw_messages)
    except Exception as _e:
        logger.debug('[save_conv] _assign_message_ids unavailable: %s', _e)
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

    # ── Guard: prevent stale syncs from overwriting newer data ──
    # A frontend sync captured lightMsgs before an await; by the time the PUT
    # arrives, a fresher sync with MORE messages may have already completed.
    # Reject PUTs with fewer messages unless the client explicitly signals
    # truncation (e.g. regen/edit sends allowTruncate=true).
    allow_truncate = data.get('allowTruncate', False)
    existing_row = db.execute(
        'SELECT msg_count, updated_at, title, search_text FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    existing_count = existing_row['msg_count'] if existing_row else 0

    if msg_count == 0 and existing_count > 0:
        # 2026-05-05: this guard fires during NORMAL concurrent syncs
        # (translate poll racing user edit). Log at INFO — the 409 is
        # the success signal for the guard, not an error condition.
        logger.info('[save_conv] BLOCKED overwrite of conv %s — '
                    'server has %d msgs but client sent 0 '
                    '(benign: stale concurrent sync).',
                    conv_id[:12], existing_count)
        return _Defer(jsonify, {'ok': False, 'error': 'blocked_empty_overwrite',
                        'serverMsgCount': existing_count}, status=409)

    if msg_count > 0 and msg_count < existing_count and not allow_truncate:
        # 2026-05-05: this guard fires during NORMAL concurrent syncs
        # (e.g. translate poll). Log at INFO — the 409 already tells the
        # client to retry with fresh state; not worth an error.log entry.
        logger.info('[save_conv] BLOCKED regression of conv %s — '
                    'server has %d msgs but client sent %d (delta=%d). '
                    'This is a stale sync from a concurrent async callback '
                    '(e.g. translate poll). Set allowTruncate=true for '
                    'intentional truncation (regen/edit).',
                    conv_id[:12], existing_count, msg_count,
                    existing_count - msg_count)
        return _Defer(jsonify, {'ok': False, 'error': 'blocked_msg_regression',
                        'serverMsgCount': existing_count,
                        'clientMsgCount': msg_count}, status=409)

    # ── Guard: prevent stale streaming checkpoint from overwriting completed result ──
    # Root cause: VS Code port forwarding can reload the page at the exact moment
    # the backend _sync_result_to_conversation writes complete data (finishReason,
    # usage, full content).  The frontend's IDB cache has a stale streaming snapshot
    # and PUTs it back, erasing the completed result.
    # Fix: if server has a completed assistant message (finishReason set) but client
    # is sending one without finishReason AND with less content, block the overwrite.
    if msg_count > 0 and msg_count == existing_count and not allow_truncate:
        incoming_last = raw_messages[-1] if raw_messages else {}
        incoming_fr = incoming_last.get('finishReason') or ''
        # Block if: (a) no finishReason (stale streaming snapshot), or
        # (b) finishReason='server_offline' (frontend-only verdict after
        #     connection loss — the backend has the complete content).
        if incoming_last.get('role') == 'assistant' and (not incoming_fr or incoming_fr == 'server_offline'):
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
                            return _Defer(jsonify, {
                                'ok': False,
                                'error': 'blocked_stale_checkpoint',
                                'serverMsgCount': existing_count,
                            }, status=409)
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

    # ── Guard: don't bump updated_at on content-less re-saves ──
    # The frontend's saveConversations() stamps updatedAt=Date.now() on every
    # sync, and reconciliation paths (initActiveTasks / loadConversationsFromServer
    # merge, e.g. after a DB migration) re-PUT untouched conversations. Without
    # this guard those no-op syncs rewrite updated_at to "now", making the
    # sidebar show ancient conversations as "just updated" and corrupting the
    # real activity order. When the persisted content is identical (same
    # msg_count, title, and search_text) we keep the server's existing
    # updated_at instead of the client's bumped value. Genuine edits change
    # search_text (or msg_count/title), so they still update the timestamp.
    if (existing_row is not None
            and msg_count == existing_count
            and title == existing_row['title']
            and search_text == (existing_row['search_text'] or '')):
        _existing_updated = existing_row['updated_at']
        if _existing_updated and _existing_updated < updated:
            logger.debug('[save_conv] Conv %s — content unchanged, preserving '
                         'updated_at=%s (ignoring client bump to %s)',
                         conv_id[:12], _existing_updated, updated)
            updated = _existing_updated

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages, 'created_at': created, 'updated_at': updated,
        'settings': settings, 'msg_count': msg_count, 'search_text': search_text,
    }, insert_cols=_CONV_INSERT_COLS, retry=True)
    update_conversation_fts(db, conv_id, search_text)
    # Phase 5 dual-write (flag-gated, best-effort): mirror the full-array PUT
    # into conversation_messages rows. No-op unless TOFU_MESSAGES_ROWS.
    from lib.database.messages_rows import dual_write_conv
    dual_write_conv(db, conv_id, raw_messages, now_ms=updated)
    _invalidate_meta_cache()
    return _Defer(api_ok)


@conversations_bp.route('/api/v1/conversations/<conv_id>/settings', methods=['PATCH'])
@_db_safe
async def patch_conv_settings(conv_id):
    """Lightweight endpoint to merge new keys into a conversation's settings JSON.

    Unlike PUT (which requires full messages), this only touches the settings
    column — safe to call for shell conversations that haven't loaded messages.

    Body: { folderId?: str|null, pinned?: bool, ... }
    All keys in the body are merged into the existing settings dict.

    Native-async: request body is awaited; the SELECT+UPDATE (which preserves
    db_execute_with_retry's lock-retry semantics) runs off-loop on a pooled
    connection via asyncio.to_thread.
    """
    import asyncio
    data = await async_parse_body()
    if not data:
        return api_bad_request('No settings provided')

    def _work():
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        try:
            row = db.execute(
                'SELECT settings FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)
            ).fetchone()
            if not row:
                return None
            settings = _safe_json(row['settings'], default={}, label='patch_settings')
            settings.update(data)
            settings_json = json.dumps(settings, ensure_ascii=False)
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
                (settings_json, conv_id, DEFAULT_USER_ID)
            )
            return True
        finally:
            _pool_put(db)

    ok = await asyncio.to_thread(_work)
    if ok is None:
        return api_not_found('Not found')
    _invalidate_meta_cache()
    logger.info('[patch_settings] Conv %s — patched keys: %s', conv_id[:12], list(data.keys()))
    return api_ok()


@conversations_bp.route('/api/v1/conversations/<conv_id>/title', methods=['PATCH'])
@_db_safe
async def rename_conv(conv_id):
    """Rename a conversation (title column only).

    Body: ``{title: str}``. Touches only the ``title`` column — safe to call
    for shell conversations that haven't loaded messages. Returns the cleaned,
    length-capped title that was persisted.
    """
    from lib.conversations.title_gen import TITLE_MAX_CHARS
    data = await async_parse_body()
    title = (data.get('title') or '').strip()
    if not title:
        return api_bad_request('title is empty', field='title')
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip()

    row = await async_fetchone(
        'SELECT id FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not row:
        return api_not_found('Not found')

    await async_execute(
        'UPDATE conversations SET title=? WHERE id=? AND user_id=?',
        (title, conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    _invalidate_meta_cache()
    logger.info('[rename_conv] Conv %s — title=%.50s', conv_id[:12], title)
    audit_log('conversation_renamed', conv_id=conv_id, title=title[:60])
    return api_ok(title=title)


@conversations_bp.route('/api/v1/conversations/<conv_id>/generate-title',
                        methods=['POST'])
@_db_safe
async def generate_conv_title(conv_id):
    """Generate a short descriptive title for a conversation via a cheap LLM.

    Reads the conversation's messages, asks the cheap model for a title based
    on the opening turn, persists it, and returns ``{title}``. The LLM call
    runs off the event loop (``asyncio.to_thread``). Falls back to the
    truncated-first-message heuristic on model failure — see
    ``lib.conversations.title_gen``.
    """
    import asyncio
    from lib.conversations.title_gen import generate_conversation_title

    data = await async_parse_body()
    lang = (data.get('lang') or '').strip() or None

    row = await async_fetchone(
        'SELECT messages FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not row:
        return api_not_found('Not found')
    messages = _safe_json(row['messages'], default=[], label='messages')
    if not messages:
        return api_bad_request('Conversation has no messages')

    title = await asyncio.to_thread(generate_conversation_title, messages, lang)

    await async_execute(
        'UPDATE conversations SET title=? WHERE id=? AND user_id=?',
        (title, conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    _invalidate_meta_cache()
    logger.info('[generate_title] Conv %s — title=%.50s', conv_id[:12], title)
    audit_log('conversation_title_generated', conv_id=conv_id, title=title[:60])
    return api_ok(title=title)
@conversations_bp.route('/api/v1/conversations/<conv_id>/messages/<int:msg_idx>', methods=['DELETE'])
@_db_safe
async def delete_message(conv_id, msg_idx):
    """Delete a specific message (or a user+assistant turn) from a conversation.

    Query params:
        mode: 'single' — delete only the message at msg_idx (default)
              'turn'   — if msg_idx is a user message, also delete the next
                         assistant message (the full turn)

    Returns:
        { ok: true, msgCount: int, deletedIndices: [int, ...] }

    Native-async: request args read up front; the blocking DB body runs
    off-loop via ``run_pooled``.
    """
    mode = request.args.get('mode', 'single')
    if mode not in ('single', 'turn'):
        return jsonify({'error': 'mode must be "single" or "turn"'}), 400
    return _finish(await run_pooled(lambda db: _delete_message_blocking(db, conv_id, msg_idx, mode)))


def _delete_message_blocking(db, conv_id, msg_idx, mode):
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return _nf('Not found')

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[delete_message] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
        return _ie('Failed to parse conversation messages')

    if msg_idx < 0 or msg_idx >= len(messages):
        return _br(f'Index {msg_idx} out of range (0..{len(messages) - 1})')

    # Determine which indices to delete
    deleted_indices = [msg_idx]
    target_msg = messages[msg_idx]

    if mode == 'turn' and target_msg.get('role') == 'user':
        # Also delete the following assistant message if it exists
        if msg_idx + 1 < len(messages) and messages[msg_idx + 1].get('role') == 'assistant':
            deleted_indices.append(msg_idx + 1)

    # Capture the messages being removed BEFORE popping, so we can scope the
    # cost-cache invalidation to only the day(s) they contributed cost to.
    _deleted_originals = [messages[i] for i in deleted_indices if 0 <= i < len(messages)]

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
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] delete_message caught %s: %s', type(_e_audit).__name__, _e_audit)
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

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages_json, 'created_at': created_at, 'updated_at': now_ms,
        'settings': settings_json, 'msg_count': len(messages), 'search_text': search_text,
    }, insert_cols=_CONV_INSERT_COLS, retry=True)

    update_conversation_fts(db, conv_id, search_text)

    _invalidate_meta_cache()
    # Invalidate persisted per-day cost cache — but ONLY for the day(s) the
    # deleted messages actually contributed cost to.  A whole-table wipe
    # would force the next calendar open to live-rescan the entire month.
    try:
        from lib.daily_report import invalidate_cost_cache_for_messages
        invalidate_cost_cache_for_messages(
            _deleted_originals, conv_start=created_at, conv_end=now_ms)
    except Exception as e:
        logger.debug('[delete_message] day-cost cache invalidation skipped: %s', e)
    logger.info('[delete_message] conv=%s deleted indices=%s mode=%s remaining=%d',
                conv_id[:8], deleted_indices, mode, len(messages))

    return _json({
        'ok': True,
        'msgCount': len(messages),
        'deletedIndices': deleted_indices,
    })


@conversations_bp.route('/api/v1/conversations/<conv_id>/messages/<int:msg_idx>', methods=['PATCH'])
@_db_safe
async def patch_message(conv_id, msg_idx):
    """Targeted single-message mutation for chatInner actions (edit-only,
    translation-visibility toggle, per-message metadata updates).

    Only whitelisted keys (see ``_PATCH_MSG_WHITELIST``) may be merged —
    arbitrary fields are rejected so this endpoint cannot be used to
    bypass the role/structure invariants enforced by ``save_conv``.

    Body: JSON dict with any subset of whitelisted keys.  Special sentinel
    value ``null`` for a key removes that key from the message.

    Returns:
        {ok, msgCount, msg}

    Native-async: body awaited + validated up front; blocking DB body runs
    off-loop via ``run_pooled``.
    """
    data = await async_parse_body()
    if not isinstance(data, dict) or not data:
        return api_bad_request('empty_patch')

    # Reject any key outside the whitelist — refuse silently and tell caller.
    unknown = [k for k in data.keys() if k not in _PATCH_MSG_WHITELIST]
    if unknown:
        logger.warning('[patch_msg] conv=%s idx=%d REJECTED non-whitelisted keys: %s',
                       conv_id[:8], msg_idx, unknown)
        return jsonify({'error': 'unsupported_keys', 'keys': unknown}), 400

    return _finish(await run_pooled(lambda db: _patch_message_blocking(db, conv_id, msg_idx, data)))


def _patch_message_blocking(db, conv_id, msg_idx, data):
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return _nf('Not found')

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[patch_msg] conv=%s failed to parse messages: %s', conv_id[:8], e)
        return _ie('Failed to parse conversation messages')

    if msg_idx < 0 or msg_idx >= len(messages):
        logger.warning('[patch_msg] conv=%s idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, len(messages))
        return _br(f'Index {msg_idx} out of range (0..{len(messages) - 1})')

    msg = messages[msg_idx]
    if not isinstance(msg, dict):
        return _ie('Target message is not a dict')

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

    # Backfill stable per-message IDs for any messages that lack one.
    try:
        from lib.tasks_pkg.manager import _assign_message_ids as _amid
        _amid(messages)
    except Exception as _e:
        logger.debug('[patch_msg] _assign_message_ids unavailable: %s', _e)

    # Persist — reuse the same pattern as delete_message/save_conv.
    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)
    search_text = build_search_text(messages)

    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] patch_message caught %s: %s', type(_e_audit).__name__, _e_audit)
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

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages_json, 'created_at': created_at, 'updated_at': now_ms,
        'settings': settings_json, 'msg_count': len(messages), 'search_text': search_text,
    }, insert_cols=_CONV_INSERT_COLS, retry=True)

    update_conversation_fts(db, conv_id, search_text)

    _invalidate_meta_cache()
    logger.info('[patch_msg] conv=%s idx=%d keys=%s preview=%.50s',
                conv_id[:8], msg_idx, applied_keys, _preview)
    try:
        audit_log('msg_patch', conv_id=conv_id, idx=msg_idx, keys=applied_keys)
    except Exception as e:
        logger.debug('[patch_msg] audit_log failed (non-fatal): %s', e)

    return _json({
        'ok': True,
        'msgCount': len(messages),
        'msg': msg,
    })


@conversations_bp.route('/api/v1/conversations/<conv_id>/messages/by-id/<msg_id>', methods=['PATCH'])
@_db_safe
async def patch_message_by_id(conv_id, msg_id):
    """Same as patch_message but addresses the target by stable ``_msgId``.

    Index-free addressing — robust against concurrent inserts that would
    otherwise shift indices.  Returns 404 if no message with that id exists.
    The whitelist + persistence flow is identical to the index path.

    Native-async: body awaited + validated up front; DB body off-loop.
    """
    data = await async_parse_body()
    if not isinstance(data, dict) or not data:
        return api_bad_request('empty_patch')

    unknown = [k for k in data.keys() if k not in _PATCH_MSG_WHITELIST]
    if unknown:
        logger.warning('[patch_msg_id] conv=%s id=%s REJECTED non-whitelisted keys: %s',
                       conv_id[:8], msg_id[:8], unknown)
        return jsonify({'error': 'unsupported_keys', 'keys': unknown}), 400

    return _finish(await run_pooled(lambda db: _patch_message_by_id_blocking(db, conv_id, msg_id, data)))


def _patch_message_by_id_blocking(db, conv_id, msg_id, data):
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return _nf('Not found')

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[patch_msg_id] conv=%s failed to parse messages: %s', conv_id[:8], e)
        return _ie('Failed to parse conversation messages')

    target_idx = None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get('_msgId') == msg_id:
            target_idx = i
            break
    if target_idx is None:
        logger.info('[patch_msg_id] conv=%s msgId=%s not found in %d messages',
                    conv_id[:8], msg_id[:8], len(messages))
        return _json({'error': 'Message id not found', 'msgCount': len(messages)}, status=404)

    msg = messages[target_idx]
    if not isinstance(msg, dict):
        return _ie('Target message is not a dict')

    applied_keys = []
    for key, value in data.items():
        if value is None:
            if key in msg:
                msg.pop(key, None)
                applied_keys.append('-' + key)
        else:
            msg[key] = value
            applied_keys.append(key)

    _preview = ''
    if 'content' in data and isinstance(data['content'], str):
        _preview = data['content'][:50]

    now_ms = int(time.time() * 1000)
    messages_json = json_dumps_pg(messages)
    search_text = build_search_text(messages)

    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] patch_message_by_id caught %s: %s', type(_e_audit).__name__, _e_audit)
        settings = {}
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

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages_json, 'created_at': created_at, 'updated_at': now_ms,
        'settings': settings_json, 'msg_count': len(messages), 'search_text': search_text,
    }, insert_cols=_CONV_INSERT_COLS, retry=True)

    update_conversation_fts(db, conv_id, search_text)

    _invalidate_meta_cache()
    logger.info('[patch_msg_id] conv=%s id=%s idx=%d keys=%s preview=%.50s',
                conv_id[:8], msg_id[:8], target_idx, applied_keys, _preview)
    try:
        audit_log('msg_patch', conv_id=conv_id, msg_id=msg_id, idx=target_idx, keys=applied_keys)
    except Exception as e:
        logger.debug('[patch_msg_id] audit_log failed (non-fatal): %s', e)

    return _json({
        'ok': True,
        'msgCount': len(messages),
        'msg': msg,
        'idx': target_idx,
    })


@conversations_bp.route(
    '/api/v1/conversations/<conv_id>/messages/<int:msg_idx>/branches/<int:branch_idx>',
    methods=['DELETE'],
)
@_db_safe
async def delete_branch(conv_id, msg_idx, branch_idx):
    """Delete a single branch entry from ``messages[msg_idx].branches``.

    The branch index is positional — after deletion, callers must re-index
    the remaining branches on their side (the DOM remap in ``branch.js``).

    Returns:
        {ok, branchCount}

    Native-async: blocking DB body runs off-loop via ``run_pooled``.
    """
    return _finish(await run_pooled(lambda db: _delete_branch_blocking(db, conv_id, msg_idx, branch_idx)))


def _delete_branch_blocking(db, conv_id, msg_idx, branch_idx):
    row = db.execute(
        'SELECT messages, title, settings FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    if not row:
        return _nf('Not found')

    try:
        messages = json.loads(row['messages'] or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[delete_branch] conv=%s failed to parse messages: %s', conv_id[:8], e)
        return _ie('Failed to parse conversation messages')

    if msg_idx < 0 or msg_idx >= len(messages):
        logger.warning('[delete_branch] conv=%s msg_idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, len(messages))
        return _br(f'msg_idx {msg_idx} out of range')

    msg = messages[msg_idx]
    branches = msg.get('branches') if isinstance(msg, dict) else None
    if not isinstance(branches, list):
        logger.warning('[delete_branch] conv=%s msg_idx=%d has no branches',
                       conv_id[:8], msg_idx)
        return _br('Message has no branches')
    if branch_idx < 0 or branch_idx >= len(branches):
        logger.warning('[delete_branch] conv=%s msg_idx=%d branch_idx=%d OUT OF RANGE (len=%d)',
                       conv_id[:8], msg_idx, branch_idx, len(branches))
        return _br(f'branch_idx {branch_idx} out of range (0..{len(branches) - 1})')

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
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] delete_branch caught %s: %s', type(_e_audit).__name__, _e_audit)
        settings = {}
    settings_json = json.dumps(settings, ensure_ascii=False)

    existing = db.execute(
        'SELECT created_at FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    created_at = existing['created_at'] if existing else now_ms
    title = row['title']

    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': DEFAULT_USER_ID, 'title': title,
        'messages': messages_json, 'created_at': created_at, 'updated_at': now_ms,
        'settings': settings_json, 'msg_count': len(messages), 'search_text': search_text,
    }, insert_cols=_CONV_INSERT_COLS, retry=True)

    update_conversation_fts(db, conv_id, search_text)

    _invalidate_meta_cache()
    logger.info('[delete_branch] conv=%s msg_idx=%d branch_idx=%d remaining=%d',
                conv_id[:8], msg_idx, branch_idx, branch_count)
    try:
        audit_log('branch_delete', conv_id=conv_id, msg_idx=msg_idx,
                  branch_idx=branch_idx, remaining=branch_count)
    except Exception as e:
        logger.debug('[delete_branch] audit_log failed (non-fatal): %s', e)

    return _ok({'branchCount': branch_count})
@conversations_bp.route('/api/v1/conversations/<conv_id>', methods=['DELETE'])
@_db_safe
async def delete_conv(conv_id):
    return _finish(await run_pooled(lambda db: _delete_conv_blocking(db, conv_id)))


def _delete_conv_blocking(db, conv_id):
    # Capture the conv's messages + timestamps BEFORE deleting so we can scope
    # the cost-cache invalidation to only the day(s) it contributed cost to.
    _conv_msgs, _conv_created, _conv_updated = [], 0, 0
    try:
        _row = db.execute(
            'SELECT messages, created_at, updated_at FROM conversations '
            'WHERE id=? AND user_id=?', (conv_id, DEFAULT_USER_ID)
        ).fetchone()
        if _row:
            _conv_msgs = _safe_json(_row['messages'], default=[], label='del-conv-cost')
            _conv_created = _row['created_at'] or 0
            _conv_updated = _row['updated_at'] or 0
    except Exception as _e:
        logger.debug('[delete_conv] cost-day capture skipped: %s', _e)

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
    # Invalidate persisted per-day cost cache — but ONLY for the day(s) this
    # conversation actually contributed cost to, so other days' cache survives
    # (a whole-table wipe forces a full-month live rescan on the next open).
    try:
        from lib.daily_report import invalidate_cost_cache_for_messages
        invalidate_cost_cache_for_messages(
            _conv_msgs, conv_start=_conv_created, conv_end=_conv_updated)
    except Exception as e:
        logger.debug('[delete_conv] day-cost cache invalidation skipped: %s', e)
    logger.info('[delete_conv] Deleted conv %s (rows: conv=%d, tasks=%d, transcripts=%d)',
                conv_id[:12], c1.rowcount, c2.rowcount, c3.rowcount)
    return _ok()


# ════════════════════════════════════════════════════════════════════════════
#  Endpoints moved to companion modules
# ════════════════════════════════════════════════════════════════════════════
#  /api/conversations/<id>/compactions[/<archive_id>] → routes/conversations_compaction.py
#  /api/conversations/search                          → routes/conversations_search.py
# Both register on the same conversations_bp via side-effect imports in routes/__init__.py.

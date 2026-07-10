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
    json_dumps_pg,
    run_pooled,
)
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.openapi import api_meta
from lib.request_parser import async_parse_body, parse_body  # noqa: F401
from lib.utils import safe_json as _safe_json
from lib.conversations import build_search_text, update_conversation_fts  # noqa: F401  — build_search_text re-exported for back-compat callers
from lib.conversations.segments_backfill import (
    collect_taskids_needing_segments,
    fill_messages_with_segments,
)
from lib.database._core_schema import CONVERSATIONS, upsert

# Columns written by the conversation upserts in this module. Omits `search_tsv`
# (PG-only; the conversations_search_tsv_trg BEFORE-trigger derives it from
# search_text) — so the partial insert lets the trigger own that column.
_CONV_INSERT_COLS = ['id', 'user_id', 'title', 'messages', 'created_at',
                     'updated_at', 'settings', 'msg_count', 'search_text']
from routes.common import DEFAULT_USER_ID, _db_safe, _invalidate_meta_cache, _notify_conv_changed, _refresh_meta_cache_if_stale

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
        'rev': _row_rev(r),
    }


def _row_rev(r):
    """Server-issued monotonic message-version for a conversation row, or 0
    when the column is absent (a pre-v37 row / a SELECT that didn't list it).
    Exposed on the GET shape so a CAS-aware client can round-trip it; a client
    that ignores it is unaffected (fail-open)."""
    try:
        keys = r.keys()
    except Exception:
        keys = ()
    if 'rev' in keys:
        try:
            return int(r['rev'] or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _conv_row_to_meta_dict(r):
    """Convert a DB row to a metadata-only conversation dict — same shape as
    ``_conv_row_to_dict`` MINUS the (potentially huge) message BODIES. Reports
    ``msgCount`` from the stored column so a caller still gets the count
    without deserializing every message. Used by the default (non-?full=1)
    ``GET /api/v1/conversations`` list path so a headless caller doesn't pull
    megabytes of message bodies it didn't ask for."""
    return {
        'id': r['id'], 'title': r['title'],
        'msgCount': r['msg_count'], 'msg_count': r['msg_count'],
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _safe_json(r['settings'], default=None, label='settings'),
        'rev': _row_rev(r),
    }


@conversations_bp.route('/api/v1/conversations', methods=['GET'])
@_db_safe
@api_meta(
    summary='List conversations (metadata-only by default)',
    description=(
        'Returns the caller\'s conversations ordered by `updatedAt` desc.\n\n'
        '**Response shape depends on query params:**\n'
        '* *(default, no param)* — metadata only: `id`, `title`, `msgCount` '
        '(+ `msg_count`), `createdAt`/`created_at`, `updatedAt`/`updated_at`, '
        '`settings`. Message BODIES are NOT included, so a headless caller '
        'does not download megabytes of transcript it did not request. To read '
        'a conversation\'s messages, either pass `?full=1` here or GET '
        '`/api/v1/conversations/{id}`.\n'
        '* `?full=1` — the full shape: every field above PLUS the `messages` '
        'array for each conversation (the pre-2026-07 default; opt-in now).\n'
        '* `?meta=1` — lightweight metadata served from an ETag-validated '
        'cache (the UI sidebar path). `?prefetch=<conv_id>` additionally '
        'embeds one conversation\'s full payload to save a round-trip on tab '
        'switch.'
    ),
    tags=['conversations'], scope='conversations',
    parameters=[
        {'name': 'full', 'in': 'query', 'required': False,
         'schema': {'type': 'string', 'enum': ['1']},
         'description': 'Set to `1` to include the `messages` array for every '
                        'conversation (legacy full shape). Omit for metadata-only.'},
        {'name': 'meta', 'in': 'query', 'required': False,
         'schema': {'type': 'string', 'enum': ['1']},
         'description': 'Set to `1` for the ETag-cached lightweight metadata '
                        'shape used by the UI sidebar.'},
        {'name': 'prefetch', 'in': 'query', 'required': False,
         'schema': {'type': 'string'},
         'description': 'With `?meta=1`, a conversation id whose full payload '
                        'is embedded in the meta response.'},
    ],
    responses={
        '200': {'description': (
            'A JSON array of conversations. By default each element is '
            'metadata-only (no `messages`); with `?full=1` each element also '
            'carries its `messages` array.'),
            'content': {'application/json': {'schema': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'string'},
                        'title': {'type': 'string'},
                        'msgCount': {'type': 'integer',
                                     'description': 'Message count (metadata default).'},
                        'createdAt': {'type': 'integer'},
                        'updatedAt': {'type': 'integer'},
                        'settings': {'type': 'object', 'nullable': True,
                                     'additionalProperties': True},
                        'messages': {'type': 'array',
                                     'description': 'Present only with ?full=1.',
                                     'items': {'$ref': '#/components/schemas/ChatMessage'}},
                    },
                    'required': ['id', 'title'],
                }}}}},
    },
)
async def list_convs():
    """List the user's conversations.

    Default: metadata only (id, title, msgCount, timestamps, settings) — NO
    message bodies, so a headless caller doesn't pull megabytes it didn't ask
    for. With ``?full=1``: the legacy full shape including every message body.
    With ``?meta=1``: lightweight metadata served from an ETag-validated cache
    so the sidebar can refresh cheaply. ``?prefetch=<conv_id>`` adds the full
    payload of one specific conv to the meta response, saving one round-trip on
    tab switch.

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
                            'SELECT id, title, messages, created_at, updated_at, settings, rev FROM conversations WHERE id=? AND user_id=?',
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

    # Default: metadata-only (no message BODIES) — a headless caller listing
    # conversations almost never needs every message of every conv, and
    # returning them can be megabytes (the UI already uses ?meta=1). Full
    # message bodies are opt-in via ?full=1 for the rare caller that wants the
    # legacy shape. This does NOT touch the cached ?meta=1 sidebar path above.
    full = request.args.get('full') == '1'
    if full:
        rows = await async_fetchall(
            'SELECT id, title, messages, created_at, updated_at, settings, rev FROM conversations WHERE user_id=? ORDER BY updated_at DESC',
            (DEFAULT_USER_ID,), domain=DOMAIN_CHAT)
        convs = [_conv_row_to_dict(r) for r in rows]
        return jsonify(convs)

    # Metadata-only: skip the messages column entirely (msg_count is a stored
    # column, so we still report message counts without deserializing bodies).
    rows = await async_fetchall(
        'SELECT id, title, msg_count, created_at, updated_at, settings, rev FROM conversations WHERE user_id=? ORDER BY updated_at DESC',
        (DEFAULT_USER_ID,), domain=DOMAIN_CHAT)
    convs = [_conv_row_to_meta_dict(r) for r in rows]
    return jsonify(convs)


def _conv_has_live_task(conv_id):
    """True when a task for this conv is CURRENTLY pending/running in the
    local TaskRuntime.

    This is the load-bearing gate for GET-path reconcile: ``classify_ghost_tail``
    returns ``'delete'`` for a ``{role:'assistant', content:''}`` tail with no
    finishReason/usage — which is BYTE-IDENTICAL to a fresh streaming placeholder
    in the window between ``create_task`` (which registers the task as running
    BEFORE the first delta) and the first streamed token. Reconciling then would
    delete the live stream's target and PERSIST that deletion — a data-corruption
    regression worse than the frontend patch we're retiring. We therefore gate on
    the RUNTIME task state (not the frontend-synced ``settings.activeTaskId``,
    which is null/stale after a mid-stream crash), mirroring the frontend's
    ``activeStreams.has(convId) || conv.activeTaskId`` predicate on the server.
    """
    try:
        from lib.tasks_pkg.manager import _latest_task_for_conv, _chat_runtime
        tid = _latest_task_for_conv(conv_id)
        if not tid:
            return False
        t = _chat_runtime.get(tid)
        return bool(t and t.get('status') in ('pending', 'running'))
    except Exception as e:
        # Fail SAFE: if we can't prove the conv is idle, assume it may be live
        # and skip reconcile (never delete a possibly-live placeholder).
        logger.warning('[get_conv] live-task probe failed for conv=%s: %s — '
                       'skipping GET-path reconcile (fail-safe)', conv_id[:8], e)
        return True


def _rehydrate_segments_from_task_results(db, conv_id, messages):
    """Backstop for segment-timeline delivery (epic pt_cb8f98b0cb9b47fb).

    Fill ``segments`` on any assistant message that LACKS them from the
    backend-authoritative ``task_results.segments`` row (the thin persisted
    form — id/name/input/result, exactly what renderSegmentTimelineHTML
    consumes), keyed on the message's ``_taskId``. This recovers segments for
    turns persisted BEFORE the save_conv preservation fix shipped (the client
    PUT had already stripped them and the old GET path never put them back).

    Display-only: enriches the SERVED payload in place, does NOT write back —
    so no GET-path write-amplification and no rev bump. Once a turn re-syncs
    through the fixed save_conv, segments live in the messages column and this
    backstop finds them already present and no-ops for that message. Falls
    through cleanly (leaves the message segment-less → grouped render) when no
    ``task_results`` row exists. Best-effort: never break the GET response.

    Returns the number of messages rehydrated.
    """
    _need = collect_taskids_needing_segments(messages)
    if not _need:
        return 0
    filled = 0
    try:
        placeholders = ','.join('?' for _ in _need)
        rows = db.execute(
            'SELECT task_id, segments FROM task_results WHERE task_id IN (%s)'
            % placeholders,
            tuple(_need.keys())).fetchall()
        # Reuse the shared fill core (single source of truth with the backfill
        # migration — see lib/conversations/segments_backfill.py).
        segs_by_tid = {row[0]: row[1] for row in rows}
        filled = fill_messages_with_segments(_need, segs_by_tid)
    except Exception as e:
        logger.warning('[get_conv] segments rehydrate from task_results failed '
                       'conv=%s: %s', conv_id[:8], e, exc_info=True)
        return filled
    if filled:
        logger.info('[get_conv] 🧩 Rehydrated segments on %d message(s) from '
                    'task_results for conv=%s (display-only, no persist)',
                    filled, conv_id[:8])
    return filled


def _reconcile_conv_on_get_blocking(db, conv_id, r):
    """Server-authoritative ghost reconcile on the conversation GET path.

    Runs the SAME pure verdict as startup recovery
    (``reconcile_conversation_messages``) so the frontend never has to INFER
    settled lifecycle state (separation-of-concerns directive). Returns the
    (possibly cleaned) row dict to serialize.

    Four gates (see the epic pt_e02044f4ab084dff design):
      1. LIVE-TASK gate — never reconcile a conv with a pending/running task
         (checked by the caller BEFORE this runs); a live empty placeholder
         must not be deleted+persisted.
      2. changed-only — skip the DB write entirely when the verdict changed
         nothing (no write-amplification on every conversation open).
      3. NO ``updated_at`` bump — persist the cleaned list WITHOUT touching the
         timestamp, else the sidebar re-sorts to "now" on every open (backend
         mirror of the ``saveConversations`` load-time-restamp gotcha).
      4. cache-neutral — pass the LIVE ``get_cache_prefix_count`` so the sweep
         never removes an in-prefix message (which would bust the prompt cache);
         and signal ``notify_history_rewrite`` so cache-break detection names the
         cause honestly instead of laundering it into a false "PROVEN" verdict.
    """
    messages = _safe_json(r['messages'], default=[], label='messages')
    if not messages:
        return _conv_row_to_dict(r)

    try:
        from lib.conversations.reconcile import reconcile_conversation_messages
        from lib.tasks_pkg.cache_tracking import (
            get_cache_prefix_count, notify_history_rewrite,
        )
    except Exception as e:
        logger.warning('[get_conv] reconcile deps unavailable for conv=%s: %s',
                       conv_id[:8], e)
        return _conv_row_to_dict(r)

    prefix_n = 0
    try:
        prefix_n = get_cache_prefix_count(conv_id)
    except Exception as e:
        logger.debug('[get_conv] get_cache_prefix_count failed conv=%s: %s',
                     conv_id[:8], e)

    cleaned, changed = reconcile_conversation_messages(messages, prefix_n)
    if not changed:
        d = _conv_row_to_dict(r)
        # Backstop: fill segments lost before the save_conv preservation fix
        # (display-only, no persist). New/re-synced turns already carry them.
        try:
            _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
        except Exception as _rse:
            logger.debug('[get_conv] segments rehydrate (unchanged path) '
                         'conv=%s: %s', conv_id[:8], _rse)
        return d

    # ── Persist the cleaned list in the SAME request that serves it.
    #    NO updated_at bump (gate 3): re-use the stored value verbatim so the
    #    sidebar sort order is untouched. Stamp settings._reconciledAt so the
    #    frontend Case-D path DEFERS (it is gated on !conv._reconciledAt) and
    #    does not re-infer lifecycle state. ──
    settings_dict = _safe_json(r['settings'], default={}, label='settings') or {}
    if not isinstance(settings_dict, dict):
        settings_dict = {}
    settings_dict['_reconciledAt'] = int(time.time() * 1000)

    messages_json = json_dumps_pg(cleaned)
    settings_json = json.dumps(settings_dict, ensure_ascii=False)
    search_text = build_search_text(cleaned)
    db.execute(
        'UPDATE conversations SET messages=?, settings=?, msg_count=?, '
        'search_text=? WHERE id=? AND user_id=?',
        (messages_json, settings_json, len(cleaned), search_text,
         conv_id, DEFAULT_USER_ID))
    db.commit()

    try:
        notify_history_rewrite(conv_id)
    except Exception as e:
        logger.debug('[get_conv] notify_history_rewrite failed conv=%s: %s',
                     conv_id[:8], e)

    logger.info('[get_conv] Reconciled ghost message(s) on GET for conv=%s '
                '(%d→%d msgs, no updated_at bump)',
                conv_id[:8], len(messages), len(cleaned))

    d = {
        'id': r['id'], 'title': r['title'],
        'messages': cleaned,
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': settings_dict,
    }
    # Backstop: fill segments lost before the save_conv preservation fix
    # (display-only — the reconcile write above already committed the cleaned
    # list; rehydration enriches ONLY the served dict, never re-persisted).
    try:
        _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
    except Exception as _rse:
        logger.debug('[get_conv] segments rehydrate (changed path) '
                     'conv=%s: %s', conv_id[:8], _rse)
    return d


@conversations_bp.route('/api/v1/conversations/<conv_id>', methods=['GET'])
@_db_safe
async def get_conv(conv_id):
    """Fetch a single conversation with full messages.

    Native-async: uses the await-able DB facade (``async_fetchone``) so the
    query runs on the dedicated DB executor without blocking the event loop.

    On this path the server also runs the authoritative ghost reconcile
    (``reconcile_conversation_messages``) so the frontend no longer has to infer
    settled lifecycle state — GATED so it never touches a conv with a live task
    (see ``_conv_has_live_task`` / ``_reconcile_conv_on_get_blocking``).
    """
    r = await async_fetchone(
        'SELECT id, title, messages, created_at, updated_at, settings, rev FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not r:
        return api_not_found('Not found')

    # GATE 1 (live-task): reconcile ONLY when the conv is idle. A pending/running
    # task's empty placeholder is indistinguishable from a ghost tail; deleting
    # it would corrupt the live stream. Skip reconcile AND leave _reconciledAt
    # unstamped so the frontend keeps deferring rather than treating it as clean.
    if _conv_has_live_task(conv_id):
        return jsonify(_conv_row_to_dict(r))

    try:
        cleaned = await run_pooled(
            lambda db: _reconcile_conv_on_get_blocking(db, conv_id, r))
        return jsonify(cleaned)
    except Exception as e:
        logger.warning('[get_conv] GET-path reconcile failed for conv=%s: %s — '
                       'serving unreconciled row', conv_id[:8], e, exc_info=True)
        return jsonify(_conv_row_to_dict(r))


@conversations_bp.route('/api/v1/conversations/<conv_id>/preview', methods=['GET'])
@_db_safe
async def conv_preview(conv_id):
    """Lightweight hover-preview of a conversation: its title + opening question.

    Powers the Project Brain panel's hover previews over opaque conversation
    IDs (activity chips, board owner chips, peer roster, the peer-message
    thread), so a reader can tell what a conversation is about without opening
    it. Returns ``{id, title, firstUserMessage, msgCount}`` — the message
    BODIES are parsed only to extract the first user turn, so the response is a
    few hundred bytes even for a huge conversation.

    Native-async: the row read + first-user extraction run off-loop.
    """
    import asyncio
    r = await async_fetchone(
        'SELECT id, title, messages, msg_count FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    if not r:
        # The Project Brain panel — and the peer-message feed payload it renders
        # from — sometimes carries a TRUNCATED 8-char conversation id (the [:8]
        # display form) instead of the full 14-char id. A peer note's `toConv`,
        # in particular, is stored short whenever the acting agent addressed the
        # target by its displayed short id, so an exact lookup misses and the
        # hover falls back to "Untitled / no messages". Resolve the short id by
        # unique prefix; accept the match only when it is unambiguous.
        rows = await async_fetchall(
            'SELECT id, title, messages, msg_count FROM conversations '
            'WHERE id LIKE ? AND user_id=? LIMIT 2',
            (conv_id + '%', DEFAULT_USER_ID), domain=DOMAIN_CHAT)
        if len(rows) == 1:
            r = rows[0]
        else:
            logger.debug('[conv_preview] no unique match for id/prefix=%s (%d rows)',
                         conv_id[:12], len(rows))
            return api_not_found('Not found')

    def _extract():
        from lib.conversations import first_user_text
        msgs = _safe_json(r['messages'], default=[], label='messages')
        return first_user_text(msgs)

    try:
        first_user = await asyncio.to_thread(_extract)
    except Exception as e:
        logger.warning('[conv_preview] extract first-user failed for conv=%s: %s',
                       conv_id[:8], e)
        first_user = ''
    return api_ok({
        'id': r['id'],
        'title': r['title'] or '',
        'firstUserMessage': first_user,
        'msgCount': r['msg_count'] or 0,
    })


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
        'SELECT msg_count, updated_at, title, search_text, rev FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()
    existing_count = existing_row['msg_count'] if existing_row else 0
    server_rev = _row_rev(existing_row) if existing_row else 0

    # ── Backend-authoritative ghost-husk sweep on the WRITE seam ──
    # Mirror the GET-path reconcile (``_reconcile_conv_on_get_blocking``) HERE so
    # an empty ghost assistant placeholder — pushed by a frontend reconnect /
    # queue-drain recovery path (``main_send_pipeline.js`` _checkForQueuedTask /
    # _recoverTimedOutChatTask) and PUT verbatim — can never PERSIST, not even
    # transiently, instead of relying on a later GET to scrub it. Reuses the SAME
    # pure verdict (``reconcile_conversation_messages``: buried-ghost sweep + tail
    # delete/interrupt) as the GET path and ``_sync_partial_to_conversation``'s
    # anti-husk guard, making the write seam symmetric.
    #
    # GATE (identical to the GET path): skip when a task is pending/running — a
    # live streaming placeholder is byte-identical to a ghost tail, so sweeping
    # mid-stream would delete the live stream's target and PERSIST that deletion.
    # Also skip on allowTruncate (edit/regen owns the tail) and 0-msg writes.
    #
    # ORDERING TRAP: the sweep runs BEFORE the count-regression / stale-checkpoint
    # guards, and those guards compare against a husk-FREE view of BOTH sides
    # (``_existing_effective_count``). Otherwise a clean shorter PUT against an
    # already-husk-bloated row (or this sweep's own shrunk output vs a still-
    # bloated row) would trip ``blocked_msg_regression`` and the guard would
    # actively PRESERVE the husks. Cache-neutral: passes the live
    # ``get_cache_prefix_count`` so the buried sweep never removes an in-prefix
    # message. No ``updated_at`` bump (the shared no-op guard below keeps the
    # stored timestamp when the swept content matches — consistent with GET).
    _existing_effective_count = existing_count
    _husk_swept = 0
    if msg_count > 0 and not allow_truncate and not _conv_has_live_task(conv_id):
        try:
            from lib.conversations.reconcile import reconcile_conversation_messages
            from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
            try:
                _prefix_n = get_cache_prefix_count(conv_id)
            except Exception as _pe:
                logger.debug('[save_conv] get_cache_prefix_count failed conv=%s: %s',
                             conv_id[:12], _pe)
                _prefix_n = 0
            # (a) Sweep the INCOMING payload so husks never land.
            _cleaned, _changed = reconcile_conversation_messages(raw_messages, _prefix_n)
            if _changed:
                _husk_swept = len(raw_messages) - len(_cleaned)
                raw_messages = _cleaned
                msg_count = len(raw_messages)
                messages = json_dumps_pg(raw_messages)
                # Re-derive lastMsgRole/lastMsgTimestamp (Case-E orphan detection)
                # from the POST-sweep tail so settings don't point at a removed husk.
                if msg_count > 0:
                    _lm = raw_messages[-1]
                    settings_dict['lastMsgRole'] = _lm.get('role')
                    settings_dict['lastMsgTimestamp'] = _lm.get('timestamp')
                    settings = json.dumps(settings_dict, ensure_ascii=False)
                logger.info('[save_conv] \U0001f9f9 Swept %d ghost husk(s) from '
                            'incoming PUT conv=%s (%d\u2192%d) — write-seam symmetry '
                            'with GET reconcile', _husk_swept, conv_id[:12],
                            _husk_swept + msg_count, msg_count)
            # (b) Husk-free view of the EXISTING row for the guards below (only
            #     needed when the incoming is NOT a strict growth).
            if existing_row is not None and existing_count > 0 and msg_count <= existing_count:
                _ex_row = db.execute(
                    'SELECT messages FROM conversations WHERE id=? AND user_id=?',
                    (conv_id, DEFAULT_USER_ID)).fetchone()
                if _ex_row:
                    try:
                        _ex_msgs = json.loads(_ex_row[0] or '[]') or []
                    except (json.JSONDecodeError, TypeError):
                        _ex_msgs = []
                    if _ex_msgs:
                        _ex_clean, _ = reconcile_conversation_messages(_ex_msgs, _prefix_n)
                        _existing_effective_count = len(_ex_clean)
        except Exception as _sw:
            logger.warning('[save_conv] ghost-husk sweep failed conv=%s: %s '
                           '(continuing unswept)', conv_id[:12], _sw, exc_info=True)

    # ── Guard: compare-and-swap on the server-issued monotonic `rev` ──
    # A CAS-aware client sends the `rev` it last saw (baseRev). The write is
    # accepted only if baseRev == the row's current rev; otherwise the client's
    # base is stale (another tab/device/server-write advanced rev) and a blind
    # overwrite would clobber fresh server truth. We reject with 409 +
    # blocked_rev_conflict AND the server's current row so the client can
    # three-way rebase its un-acked tail and re-PUT with the new baseRev.
    #
    # FAIL-OPEN (non-negotiable): baseRev is only enforced when the client
    # actually sent one AND the row exists. A client that omits baseRev (old
    # bundle mid-rollout, or the compat/headless surfaces that never learned
    # about rev) falls straight through to the legacy count-regression guards
    # below — a v36-era client against a rev=0 row must never start eating 409s.
    # The CAS is ALSO scoped to message-bearing writes: a settings/title-only
    # PUT (msg_count==existing_count with identical messages) never asserts CAS,
    # matching the trigger which does not bump rev on those.
    base_rev = data.get('baseRev')
    if base_rev is not None and existing_row is not None and not allow_truncate:
        try:
            base_rev_int = int(base_rev)
        except (TypeError, ValueError):
            base_rev_int = None
            logger.debug('[save_conv] conv=%s ignoring non-int baseRev=%r (fail-open)',
                         conv_id[:12], base_rev)
        if base_rev_int is not None and base_rev_int != server_rev:
            logger.info('[save_conv] BLOCKED rev conflict conv=%s — '
                        'client baseRev=%d but server rev=%d (concurrent write '
                        'from another tab/device/server). Client must rebase + retry.',
                        conv_id[:12], base_rev_int, server_rev)
            return _Defer(jsonify, {'ok': False, 'error': 'blocked_rev_conflict',
                            'serverRev': server_rev,
                            'serverMsgCount': existing_count}, status=409)

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

    if msg_count > 0 and msg_count < _existing_effective_count and not allow_truncate:
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
    if msg_count > 0 and msg_count == _existing_effective_count and not allow_truncate:
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


    # ── Preserve server-authored `segments` against frontend overwrite ──
    # Same class of bug as the translation merge above: `segments` is the
    # backend-authoritative typed-timeline SoT (epic pt_cb8f98b0cb9b47fb),
    # written onto the message by _sync_result_to_conversation. The client
    # NEVER echoes it back — _trimMsgForPersist (core/conversations.js) strips
    # it on every PUT. Without merging it back here, the FIRST post-turn full-
    # conversation sync overwrites the message with segments gone, and the GET
    # path (which does not re-derive them from the messages column) then serves
    # a segment-less message → the frontend timeline gate falls back to the
    # grouped render. Fix: before the overwrite, re-attach segments from the
    # existing DB message onto the incoming (stripped) one, keyed on stable
    # identity (_msgId, falling back to _taskId), only when the client's copy
    # lacks them. Skip on allowTruncate (edit/regen intentionally rewrites, and
    # a regenerated turn's stale segments must NOT be resurrected).
    _seg_preserved = 0
    if msg_count > 0 and not allow_truncate:
        try:
            _seg_row = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=?',
                (conv_id, DEFAULT_USER_ID)
            ).fetchone()
            if _seg_row:
                try:
                    _seg_db_msgs = json.loads(_seg_row[0] or '[]') or []
                except (json.JSONDecodeError, TypeError) as _sje:
                    logger.warning('[save_conv] Failed to parse existing messages '
                                   'for segments merge conv=%s: %s',
                                   conv_id[:12], _sje)
                    _seg_db_msgs = []

                # Index the DB messages that carry segments by their stable id
                # so we can match regardless of any positional shift.
                _seg_by_msgid = {}
                _seg_by_taskid = {}
                for _dbm in _seg_db_msgs:
                    if not isinstance(_dbm, dict):
                        continue
                    _dbsegs = _dbm.get('segments')
                    if not _dbsegs:
                        continue
                    _mid = _dbm.get('_msgId')
                    _tid = _dbm.get('_taskId')
                    if _mid:
                        _seg_by_msgid[_mid] = _dbsegs
                    if _tid:
                        _seg_by_taskid[_tid] = _dbsegs

                if _seg_by_msgid or _seg_by_taskid:
                    for _dst in raw_messages:
                        if not isinstance(_dst, dict):
                            continue
                        if _dst.get('segments'):
                            continue  # client already carries segments — leave it
                        _src_segs = None
                        _mid = _dst.get('_msgId')
                        if _mid and _mid in _seg_by_msgid:
                            _src_segs = _seg_by_msgid[_mid]
                        else:
                            _tid = _dst.get('_taskId')
                            if _tid and _tid in _seg_by_taskid:
                                _src_segs = _seg_by_taskid[_tid]
                        if _src_segs:
                            _dst['segments'] = _src_segs
                            _seg_preserved += 1

                if _seg_preserved > 0:
                    # Re-materialize the payload so the write below carries the
                    # merged segments back into the messages column.
                    messages = json_dumps_pg(raw_messages)
                    logger.info(
                        '[save_conv] 🧩 Preserved segments on %d message(s) '
                        'from DB into incoming payload conv=%s',
                        _seg_preserved, conv_id[:12],
                    )
        except Exception as _seg_me:
            logger.warning('[save_conv] segments-merge pre-step failed '
                           'conv=%s: %s (continuing without merge)',
                           conv_id[:12], _seg_me, exc_info=True)

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
            and msg_count == _existing_effective_count
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
    # Return the post-write rev (the trigger bumped it iff messages changed) so
    # a CAS-aware client advances its baseRev in lockstep and its NEXT PUT
    # carries a fresh base. A client that ignores `rev` is unaffected.
    new_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=?',
                         (conv_id, DEFAULT_USER_ID)).fetchone()
    new_rev = _row_rev(new_row) if new_row else server_rev
    # Event-driven cross-device sync: push the change (carrying the new rev) so
    # a sibling device reconciles this conv without a manual refresh. Rev-gated
    # on the client → self-echo is a cheap no-op.
    _notify_conv_changed(conv_id, rev=new_rev)
    return _Defer(api_ok, rev=new_rev)


@conversations_bp.route('/api/v1/conversations/<conv_id>/settings', methods=['PATCH'])
@_db_safe
async def patch_conv_settings(conv_id):
    """Lightweight endpoint to merge new keys into a conversation's settings JSON.

    Unlike PUT (which requires full messages), this only touches the settings
    column — safe to call for shell conversations that haven't loaded messages.

    Body: { folderId?: str|null, pinned?: bool, ... }
    All keys in the body are merged into the existing settings dict.

    Native-async: request body is awaited; the serialized read-merge-write
    (settings_store, which guards against clobbering a concurrent settings
    write) runs off-loop on a pooled connection via asyncio.to_thread.
    """
    import asyncio
    data = await async_parse_body()
    if not data:
        return api_bad_request('No settings provided')

    def _work():
        # Serialized read-merge-write (see settings_store) so a settings PATCH
        # doesn't clobber a concurrent activeTaskId / autopilot / tool-state
        # write on the same row.
        from lib.conversations import set_conversation_settings
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        try:
            res = set_conversation_settings(
                conv_id, data, user_id=DEFAULT_USER_ID, db=db)
            return True if res is not None else None
        finally:
            _pool_put(db)

    ok = await asyncio.to_thread(_work)
    if ok is None:
        return api_not_found('Not found')
    # Metadata-only change (folder move / pin / activeTaskId): rev unchanged →
    # client does a debounced sidebar refresh, not a body refetch.
    _notify_conv_changed(conv_id, rev=None)
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
    # Metadata-only change (rev=None): the DB rev trigger only bumps on a
    # messages change, so a rename doesn't move rev — the client falls back to
    # a debounced sidebar refresh (title/folder) rather than a body refetch.
    _notify_conv_changed(conv_id, rev=None)
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
    _notify_conv_changed(conv_id, rev=None)
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
    # Stable-id addressing (mirrors chat_regenerate's truncateToMsgId): the
    # client sends the target's ``_msgId`` so a list-length drift between its
    # read and this request (e.g. a server-side ghost-sweep / reconcile shrank
    # the persisted messages) resolves to the CURRENT index instead of a stale
    # one — the root cause of "delete does nothing / deletes the wrong turn".
    msg_id = request.args.get('msgId') or None
    return _finish(await run_pooled(
        lambda db: _delete_message_blocking(db, conv_id, msg_idx, mode, msg_id)))


def _delete_message_blocking(db, conv_id, msg_idx, mode, msg_id=None):
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

    # ── Stable-id resolution: msgId is authoritative, index is the fallback ──
    if msg_id:
        from lib.tasks_pkg.manager import find_message_by_id
        _resolved_idx, _ = find_message_by_id(messages, msg_id)
        if _resolved_idx is not None:
            if _resolved_idx != msg_idx:
                logger.info('[delete_message] conv=%s msgId=%s resolved to index %d '
                            '(client sent index %d — drift corrected)',
                            conv_id[:8], str(msg_id)[:12], _resolved_idx, msg_idx)
            msg_idx = _resolved_idx
        else:
            logger.debug('[delete_message] conv=%s msgId=%s did not resolve — '
                         'using index %d', conv_id[:8], str(msg_id)[:12], msg_idx)

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

    _dm_rev_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=?',
                             (conv_id, DEFAULT_USER_ID)).fetchone()
    _notify_conv_changed(conv_id, rev=(_row_rev(_dm_rev_row) if _dm_rev_row else None))
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
        # ── Keep the segment SoT consistent with the edited deliverable ──
        # A finished assistant/critic/VU turn stores its answer BOTH as
        # ``content`` and as the terminal deliverable ``text`` segment in
        # ``segments`` (the authoritative render/wire source — deliverable_text
        # / derive_content read it first). An in-place edit only PATCHes
        # ``content``; without this the segment list keeps the PRE-EDIT answer
        # and a segment-driven read (headless/compat, next-turn wire rebuild)
        # resurfaces the stale text. Realign the terminal deliverable segment
        # in place (no-op when absent/already consistent). Best-effort — a
        # failure here must never block the content edit itself.
        seg_list = msg.get('segments')
        if seg_list:
            try:
                from lib.tasks_pkg.segments import apply_edited_deliverable
                _realigned = apply_edited_deliverable(seg_list, data['content'])
                if _realigned is not None:
                    msg['segments'] = _realigned
                    logger.info('[patch_msg] conv=%s idx=%d realigned terminal '
                                'deliverable segment to edited content',
                                conv_id[:8], msg_idx)
            except Exception as _seg_e:
                logger.warning('[patch_msg] conv=%s idx=%d segment realign '
                               'skipped: %s', conv_id[:8], msg_idx, _seg_e)

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

    _pm_rev_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=?',
                             (conv_id, DEFAULT_USER_ID)).fetchone()
    _notify_conv_changed(conv_id, rev=(_row_rev(_pm_rev_row) if _pm_rev_row else None))
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

    _pmi_rev_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=?',
                              (conv_id, DEFAULT_USER_ID)).fetchone()
    _notify_conv_changed(conv_id, rev=(_row_rev(_pmi_rev_row) if _pmi_rev_row else None))
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

    # Event-driven cross-device sync: a branch delete changes the conversation
    # body, so carry the post-write rev → a sibling tab with this conv open
    # refetches without a manual refresh. notify_conv_changed also invalidates
    # the sidebar meta cache, so it replaces the bare _invalidate_meta_cache().
    _db_rev_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=?',
                             (conv_id, DEFAULT_USER_ID)).fetchone()
    _notify_conv_changed(conv_id, rev=(_row_rev(_db_rev_row) if _db_rev_row else None))
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
    _deleted_committed = False
    try:
        db.commit()
        _deleted_committed = True
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
        _deleted_committed = True
    # Event-driven cross-device sync: tell siblings this conv is gone so they
    # drop it from the sidebar (+ IDB cache) without a manual refresh.
    if _deleted_committed:
        _notify_conv_changed(conv_id, deleted=True)
    else:
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

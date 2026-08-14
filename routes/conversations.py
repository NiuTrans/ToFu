"""routes/conversations.py — Conversation CRUD endpoints."""

import json
import time

from quart import Response, request

from lib.database import (
    DOMAIN_CHAT,
    async_execute,
    async_fetchall,
    async_fetchone,
    run_pooled,
)
from lib.log import audit_log, get_logger
from lib.api_response import (
    api_bad_request, api_conflict, api_error, api_internal_error, api_not_found,
    api_ok, api_payload,
)
from lib.openapi import api_meta
from lib.request_parser import async_parse_body, parse_body  # noqa: F401
from lib.utils import safe_json as _safe_json
from lib.conversations import build_search_text, update_conversation_fts  # noqa: F401  — build_search_text re-exported for back-compat callers
from lib.conversations.segments_backfill import (
    collect_taskids_needing_segments,
    fill_messages_with_segments,
)

# Columns written by the conversation upserts in this module. Omits `search_tsv`
# (PG-only; the conversations_search_tsv_trg BEFORE-trigger derives it from
# search_text) — so the partial insert lets the trigger own that column.
from routes.common import DEFAULT_USER_ID, _db_safe, _invalidate_meta_cache, _notify_conv_changed, _refresh_meta_cache_if_stale, _request_user_id

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

# A browser may override its normal 60-message first-paint window, but a query
# parameter must never turn the bounded endpoint back into an accidental
# multi-gigabyte response. ``window=0`` remains the explicit compatibility
# escape hatch for callers that truly need the full legacy body.
_MAX_CONV_WINDOW = 500

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
    # api_payload = the passthrough primitive (contract-clean superset:
    # payload keys byte-identical top-level, additive request_id on 4xx).
    # Positional status arg — _Defer swallows a status= kwarg for its own
    # bookkeeping, which would break the tuple _finish returns.
    return _Defer(api_payload, payload, status or 200)


def _prefetch_reconciled_dict(db, conv_id, r):
    """Build the prefetch payload for a conversation, running the SAME
    server-authoritative ghost reconcile the single-conv GET handler runs.

    The ``?meta=1&prefetch=<id>`` branch of ``list_convs`` returns this conv's
    full body inline so the frontend can render it without a second round-trip.
    The frontend then sets ``pc._needsLoad = false``, which SKIPS the
    reconciling Phase-2 GET (``loadConversationMessages`` is gated on
    ``_needsLoad``). So without reconciling HERE, a prefetched active conv with
    an interrupted ghost tail reaches the client unreconciled and with
    ``settings._reconciledAt`` unstamped — the sole remaining render path the
    frontend Case-D ``_classifyGhostTail`` belt exists for.

    Gate on the live-task probe exactly as ``get_conv`` does: a pending/running
    task's empty placeholder is byte-identical to a ghost tail and must NOT be
    swept (that would delete+persist the live stream's target). For an idle
    conv, delegate to ``_reconcile_conv_on_get_blocking`` (persist-in-place, no
    ``updated_at`` bump, stamp ``_reconciledAt``).
    """
    if _conv_has_live_task(conv_id):
        return _conv_row_to_dict(r)
    return _reconcile_conv_on_get_blocking(db, conv_id, r)


def _prefetch_windowed_dict(db, conv_id, window):
    """Build the startup combo payload without loading the full JSONB blob.

    This is the synchronous twin of ``get_conv(...?window=N)`` used inside the
    metadata worker. It preserves the old full-body prefetch when no window is
    requested, while the first-party UI opts into the same verified row-store
    / authoritative projected-JSON fallback as a normal conversation open.
    """
    from lib.database import _BACKEND
    from lib.database.messages_rows import rows_read_enabled
    from lib.database.conversation_repository import (
        conversation_rows_authoritative,
    )

    live = _conv_has_live_task(conv_id)
    authority = conversation_rows_authoritative()
    use_rows = bool(rows_read_enabled() and (authority or not live))
    if authority and not use_rows:
        raise RuntimeError('normalized transcript authority is unavailable')
    r = None
    if use_rows:
        r = db.execute(
            'SELECT id, title, created_at, updated_at, settings, rev, msg_count '
            'FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)).fetchone()
        if not r:
            return None
        from lib.conv_ref._detail import row_window_usable
        use_rows = row_window_usable(db, conv_id, int(r['msg_count'] or 0))
        if authority and not use_rows:
            raise RuntimeError(
                f'canonical message rows are not current for {conv_id}')

    if use_rows:
        served, changed, cleaned_full, settings_dict = _windowed_served_readonly(
            db, conv_id, r, window, None)
    else:
        slice_sql, _ = _projected_blob_window_sql(_BACKEND, None)
        r = db.execute(
            slice_sql, (conv_id, DEFAULT_USER_ID, window)).fetchone()
        if not r:
            return None
        served, changed, cleaned_full, settings_dict = \
            _windowed_projected_blob_readonly(
                db, conv_id, r, None, allow_reconcile=not live)

    # Unlike the normal GET, the combo response makes the frontend skip its
    # second GET. Preserve the historical prefetch guarantee by persisting a
    # rare ghost-tail repair inline, guarded by the revision we inspected.
    if changed and cleaned_full is not None:
        new_rev = _persist_reconcile(
            db, conv_id, cleaned_full, settings_dict,
            expected_rev=_row_rev(r))
        if new_rev >= 0:
            served['rev'] = new_rev
    return served


def _conv_row_to_dict(r):
    """Convert a DB row (with messages column) to a conversation dict."""
    return {
        'id': r['id'], 'title': r['title'],
        'messages': _decode_json_value(
            r['messages'], default=[], label='messages'),
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _decode_json_value(
            r['settings'], default=None, label='settings'),
        'rev': _row_rev(r),
}


def _decode_json_value(raw, *, default, label):
    """Accept both driver JSON text and repository-decoded JSON values."""
    if isinstance(raw, (dict, list)):
        return raw
    return _safe_json(raw, default=default, label=label)


def _row_rev(r):
    """Server-issued monotonic message-version for a conversation row, or 0
    when the column is absent (a pre-v37 row / a SELECT that didn't list it).
    Exposed on the GET shape so a CAS-aware client can round-trip it; a client
    that ignores it is unaffected (fail-open)."""
    try:
        keys = r.keys()
    except Exception as e:
        logger.debug('[conversations] _row_rev: row has no keys(): %s', e)
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
        'settings': _decode_json_value(
            r['settings'], default=None, label='settings'),
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
            'Envelope ``{ok, items}`` — ``items`` is the array of '
            'conversations (api-contract §4 batch 9: the bare top-level array '
            'moved under ``items``). By default each element is metadata-only '
            '(no `messages`); with `?full=1` each element also carries its '
            '`messages` array.'),
            'content': {'application/json': {'schema': {
                'type': 'object',
                'properties': {
                    'ok': {'type': 'boolean'},
                    'items': {
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
                        }},
                },
                'required': ['ok', 'items'],
            }}}},
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
    # ── Folder-scoped / keyset-paginated metadata query ──────────────────
    # Evaluated FIRST so it can never be short-circuited by the ?meta=1 cached
    # branch below. This path is DELIBERATELY un-cached: the sidebar's 60s poll
    # uses the top-N ?meta=1 cache, but a folder view (?folderId=) or a
    # "load more" page (?before=) is a click-time direct read of a DIFFERENT
    # result set. Routing it through the cache would (a) pollute the top-N blob
    # and (b) move a full-table json_extract scan onto the 60s poll — exactly
    # what constraint keeps them physically separated.
    #
    # A folder's members are resolved by their real folderId (stored in the
    # settings JSON), INDEPENDENT of the global "most-recent-N" window, so a
    # folder whose members all sort past the sidebar cap is still returned in
    # full. json_extract(settings,'$.folderId') is dialect-translated to a PG
    # jsonb accessor by lib/database/_sql_translate.py, so this runs on both
    # SQLite and PG unchanged.
    folder_id = request.args.get('folderId', '').strip()
    before_updated = request.args.get('before', '').strip()
    before_id = request.args.get('before_id', '').strip()
    try:
        req_limit = int(request.args.get('limit', '') or 0)
    except (TypeError, ValueError):
        req_limit = 0
    if folder_id or before_updated:
        where = ['user_id=?']
        params = [DEFAULT_USER_ID]
        if folder_id:
            where.append("json_extract(settings,'$.folderId')=?")
            params.append(folder_id)
        if before_updated:
            # Keyset cursor on (updated_at, id) — strictly "older than" the last
            # loaded row, tie-broken by id so a same-timestamp boundary neither
            # skips nor duplicates. Both halves of the OR carry updated_at so the
            # comparison stays index-friendly.
            where.append('(updated_at < ? OR (updated_at = ? AND id < ?))')
            params.extend([before_updated, before_updated, before_id])
        page_limit = req_limit if req_limit > 0 else (300 if folder_id else 100)
        sql = ('SELECT id, title, msg_count, created_at, updated_at, settings, rev '
               'FROM conversations WHERE ' + ' AND '.join(where) +
               ' ORDER BY updated_at DESC, id DESC LIMIT ?')
        # Fetch one extra row to compute hasMore without a second COUNT.
        params.append(page_limit + 1)
        rows = await async_fetchall(sql, tuple(params), domain=DOMAIN_CHAT)
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        convs = [_conv_row_to_meta_dict(r) for r in rows]
        envelope = {'conversations': convs, 'hasMore': has_more}
        if rows:
            last = rows[-1]
            envelope['nextBefore'] = last['updated_at']
            envelope['nextBeforeId'] = last['id']
        if folder_id:
            # Real member count so the frontend distinguishes a genuinely empty
            # folder from one whose members simply aren't loaded yet.
            cnt = await async_fetchone(
                "SELECT COUNT(*) AS c FROM conversations "
                "WHERE user_id=? AND json_extract(settings,'$.folderId')=?",
                (DEFAULT_USER_ID, folder_id), domain=DOMAIN_CHAT)
            envelope['totalCount'] = (cnt['c'] if cnt else 0)
        return api_ok(envelope)

    meta_only = request.args.get('meta') == '1'
    prefetch_id = request.args.get('prefetch', '').strip()
    try:
        prefetch_window = int(request.args.get('window', '') or 0)
    except (TypeError, ValueError):
        prefetch_window = 0
    prefetch_window = max(0, min(prefetch_window, _MAX_CONV_WINDOW))
    if meta_only:
        def _meta_branch(db):
            # Runs off-loop: borrow a pooled conn, compute meta + optional
            # prefetch, return plain data for the async handler to serialize.
            payload, etag = _refresh_meta_cache_if_stale(db)
            # Authoritative global total (captured at cache rebuild, read
            # from the cached entry — no extra query on a warm poll). Powers
            # the sidebar "N earlier not loaded" affordance (C4).
            from lib.conversations.meta_cache import get_cached_total
            total = get_cached_total()
            prefetch_data = None
            if prefetch_id:
                try:
                    if prefetch_window > 0:
                        prefetch_data = _prefetch_windowed_dict(
                            db, prefetch_id, prefetch_window)
                    else:
                        from lib.database.conversation_repository import load_conversation
                        r = load_conversation(
                            db, prefetch_id, user_id=DEFAULT_USER_ID,
                            metadata_columns=(
                                'title', 'created_at', 'updated_at',
                                'settings'))
                        if r is not None:
                            prefetch_data = _prefetch_reconciled_dict(
                                db, prefetch_id, r)
                except Exception as e:
                    logger.warning('[Common] prefetch conv %s failed: %s', prefetch_id[:12], e)
            return payload, etag, prefetch_data, total

        payload, etag, prefetch_data, total = await run_pooled(
            _meta_branch, domain=DOMAIN_CHAT)

        if prefetch_id:
            combo = json.dumps({
                'conversations': json.loads(payload),
                'prefetched': prefetch_data,
            }, ensure_ascii=False).encode('utf-8')
            combo_resp = Response(combo, mimetype='application/json')
            combo_resp.headers['Cache-Control'] = 'no-cache'
            if total is not None:
                combo_resp.headers['X-Total-Count'] = str(total)
            return combo_resp

        if request.if_none_match and etag in request.if_none_match:
            return Response(status=304)
        resp = Response(payload, mimetype='application/json')
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'private, max-age=5'
        if total is not None:
            resp.headers['X-Total-Count'] = str(total)
        return resp

    # Default: metadata-only (no message BODIES) — a headless caller listing
    # conversations almost never needs every message of every conv, and
    # returning them can be megabytes (the UI already uses ?meta=1). Full
    # message bodies are opt-in via ?full=1 for the rare caller that wants the
    # legacy shape. This does NOT touch the cached ?meta=1 sidebar path above.
    full = request.args.get('full') == '1'
    if full:
        def _load_full_list(db):
            from lib.database.conversation_repository import load_conversation
            ids = db.execute(
                'SELECT id FROM conversations WHERE user_id=? '
                'ORDER BY updated_at DESC', (DEFAULT_USER_ID,)).fetchall()
            snapshots = [
                load_conversation(
                    db, row['id'], user_id=DEFAULT_USER_ID,
                    metadata_columns=(
                        'title', 'created_at', 'updated_at', 'settings'))
                for row in ids
            ]
            return [_conv_row_to_dict(s) for s in snapshots if s is not None]
        convs = await run_pooled(_load_full_list)
        # Coordinated bare-array migration (contract §4, batch 9): the
        # array moves under ``items``. No first-party consumer (the UI
        # lists via ?meta=1 / ?before envelope); documented shape change.
        return api_ok({'items': convs})

    # Metadata-only: skip the messages column entirely (msg_count is a stored
    # column, so we still report message counts without deserializing bodies).
    rows = await async_fetchall(
        'SELECT id, title, msg_count, created_at, updated_at, settings, rev FROM conversations WHERE user_id=? ORDER BY updated_at DESC',
        (DEFAULT_USER_ID,), domain=DOMAIN_CHAT)
    convs = [_conv_row_to_meta_dict(r) for r in rows]
    # Same bare-array migration as the ?full=1 branch above.
    return api_ok({'items': convs})


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


def _compute_reconcile(conv_id, r):
    """READ-ONLY ghost-reconcile verdict — the pure judgment half, no DB write.

    Runs the SAME verdict as startup recovery
    (``reconcile_conversation_messages``) so the frontend never has to INFER
    settled lifecycle state (separation-of-concerns directive), but performs
    NO ``UPDATE``/``commit`` — that is deferred to ``_persist_reconcile`` so the
    conversation GET read path never does an inline (FUSE-fsync) write.

    Returns ``(cleaned, changed, settings_dict)``:
      • ``(None, False, None)``   — empty history or reconcile deps unavailable;
        the caller serves the unreconciled row verbatim.
      • ``(cleaned, False, None)``— reconcile changed nothing (no write needed).
      • ``(cleaned, True, sd)``   — history was rewritten; ``sd`` is the settings
        dict with ``_reconciledAt`` stamped, ready to persist + serve.

    Cache-neutral: passes the LIVE ``get_cache_prefix_count`` so the sweep never
    removes an in-prefix message (which would bust the prompt cache).
    """
    messages = _decode_json_value(
        r['messages'], default=[], label='messages')
    if not messages:
        return None, False, None

    try:
        from lib.conversations.reconcile import reconcile_conversation_messages
        from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
    except Exception as e:
        logger.warning('[get_conv] reconcile deps unavailable for conv=%s: %s',
                       conv_id[:8], e)
        return None, False, None

    prefix_n = 0
    try:
        prefix_n = get_cache_prefix_count(conv_id)
    except Exception as e:
        logger.debug('[get_conv] get_cache_prefix_count failed conv=%s: %s',
                     conv_id[:8], e)

    cleaned, changed = reconcile_conversation_messages(messages, prefix_n)
    if not changed:
        return cleaned, False, None

    settings_dict = _decode_json_value(
        r['settings'], default={}, label='settings') or {}
    if not isinstance(settings_dict, dict):
        settings_dict = {}
    settings_dict['_reconciledAt'] = int(time.time() * 1000)
    return cleaned, True, settings_dict


def _persist_reconcile(db, conv_id, cleaned, settings_dict, expected_rev=None):
    """WRITE half of the reconcile — the (FUSE-fsync) ``UPDATE``+``commit`` that
    the GET read path defers to a background task (see ``_schedule_reconcile_persist``).

    Caller guarantees the verdict was ``changed=True``. Gates preserved:
      • NO ``updated_at`` bump — re-use the stored value verbatim so the sidebar
        sort order is untouched (backend mirror of the ``saveConversations``
        load-time-restamp gotcha).
      • ``_reconciledAt`` already stamped into ``settings_dict`` by ``_compute_reconcile``.
      • ★ rev-CAS. ``cleaned`` was computed from a row read EARLIER, and this
        write runs on a BACKGROUND task (``_schedule_reconcile_persist``), so an
        append that landed in between would be erased by this stale array —
        the whole-blob read-modify-write data loss this module is being audited
        for (conv ms3sfyrmn31omb: 13 VU appends, 8 survivors). ``expected_rev``
        pins the version the verdict was computed from; a lost race is a no-op
        (rev unchanged → 0 rows), NOT an overwrite, and the caller re-reads.
        Reconcile is idempotent, so dropping the write is always safe: the next
        GET recomputes the same verdict against the newer row.
        ``expected_rev=None`` keeps the legacy unconditional write for callers
        that hold the row inside the same transaction.

    After the write it signals ``notify_history_rewrite`` (honest cache-break
    naming) AND emits ``push_event('conv', conv_id, {kind:'history_rewrite', rev})``
    so every client that has this conversation open re-aligns WITHOUT a manual
    refresh. Returns the post-write ``rev`` (0 if unreadable, -1 if the CAS lost
    its race and nothing was written).
    """
    from lib.tasks_pkg.cache_tracking import notify_history_rewrite

    settings_json = json.dumps(settings_dict, ensure_ascii=False)
    search_text = build_search_text(cleaned)
    from lib.database.conversation_repository import replace_messages
    result = replace_messages(
        db, conv_id, cleaned, user_id=DEFAULT_USER_ID,
        expected_rev=expected_rev,
        metadata={
            'settings': settings_json,
            'msg_count': len(cleaned),
            'search_text': search_text,
        },
        full=True)
    if not result.applied:
        # Another writer advanced rev after the reconcile read. Standing down
        # preserves that writer and the next GET recomputes idempotently.
        logger.info(
            '[get_conv] reconcile persist STOOD DOWN conv=%s expected_rev=%s '
            '— a concurrent writer advanced the row; not overwriting with a '
            'stale %d-message array (next GET will recompute)',
            conv_id[:8], expected_rev, len(cleaned))
        return -1
    new_rev = result.rev or 0

    try:
        notify_history_rewrite(conv_id)
    except Exception as e:
        logger.debug('[get_conv] notify_history_rewrite failed conv=%s: %s',
                     conv_id[:8], e)

    try:
        from lib.push import push_event
        push_event('conv', conv_id, {'kind': 'history_rewrite', 'rev': new_rev})
    except Exception as e:
        logger.debug('[get_conv] push history_rewrite failed conv=%s: %s',
                     conv_id[:8], e)

    logger.info('[get_conv] Reconciled ghost message(s) for conv=%s '
                '(→%d msgs, no updated_at bump, rev=%d)',
                conv_id[:8], len(cleaned), new_rev)
    return new_rev


def _reconcile_conv_served_readonly(db, conv_id, r):
    """Build the GET response dict WITHOUT writing the DB (read path zero-fsync).

    Returns ``(served, changed, cleaned, settings_dict)``. When ``changed`` is
    True the ``served`` dict already carries the ``cleaned`` messages + stamped
    settings (so the OPENING client sees correct state immediately), but the row
    is NOT yet persisted — the caller schedules ``_persist_reconcile`` off the
    request; its post-write ``push_event`` (rev+1) then re-aligns every other
    open client. ``served.rev`` stays the pre-write value on purpose so a client
    can tell the push's newer rev apart.
    """
    cleaned, changed, settings_dict = _compute_reconcile(conv_id, r)
    if not changed:
        d = _conv_row_to_dict(r)
        # Backstop: fill segments lost before the save_conv preservation fix
        # (display-only, no persist). New/re-synced turns already carry them.
        try:
            _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
        except Exception as _rse:
            logger.debug('[get_conv] segments rehydrate (unchanged path) '
                         'conv=%s: %s', conv_id[:8], _rse)
        return d, False, None, None

    d = {
        'id': r['id'], 'title': r['title'],
        'messages': cleaned,
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': settings_dict,
        'rev': _row_rev(r),
    }
    try:
        _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
    except Exception as _rse:
        logger.debug('[get_conv] segments rehydrate (changed path) '
                     'conv=%s: %s', conv_id[:8], _rse)
    return d, True, cleaned, settings_dict


# Strong refs to in-flight background reconcile-persist tasks (create_task does
# not keep its own ref; without this they can be GC'd mid-flight).
_bg_reconcile_persist_tasks: set = set()


def _schedule_reconcile_persist(conv_id, cleaned, settings_dict, expected_rev=None):
    """Fire-and-forget the reconcile WRITE off the GET request.

    The read handler has already returned the cleaned dict to the opening
    client; this persists the same verdict on a background task so the request
    never blocks on a (FUSE-fsync) ``UPDATE``+``commit``. On completion
    ``_persist_reconcile`` emits the ``history_rewrite`` push so all open
    clients converge. Reconcile is idempotent, so a redundant re-fire from a
    concurrent GET is harmless (the second verdict is ``changed=False``).
    """
    import asyncio

    async def _run():
        try:
            await run_pooled(
                lambda db: _persist_reconcile(db, conv_id, cleaned, settings_dict,
                                              expected_rev=expected_rev))
        except Exception as e:
            logger.warning('[get_conv] background reconcile persist failed '
                           'conv=%s: %s', conv_id[:8], e, exc_info=True)
        finally:
            _bg_reconcile_persist_tasks.discard(asyncio.current_task())

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # No running loop (non-async caller): skip — reconcile is idempotent, so
        # the next GET from an async context will re-detect and persist. The
        # served dict was already correct, so nothing is lost visually.
        logger.debug('[get_conv] no running loop; deferring reconcile persist '
                     'conv=%s to next GET', conv_id[:8])
        return
    t = loop.create_task(_run())
    _bg_reconcile_persist_tasks.add(t)


def _reconcile_conv_on_get_blocking(db, conv_id, r):
    """SYNCHRONOUS persist-in-place reconcile (compute + write in one call).

    Retained for the ``?prefetch=<id>`` branch of ``list_convs``
    (``_prefetch_reconciled_dict``), which inlines the body so the frontend sets
    ``_needsLoad=false`` and never issues the reconciling GET — that path MUST
    persist within the request. The GET read path itself no longer calls this;
    it uses ``_reconcile_conv_served_readonly`` + a background
    ``_schedule_reconcile_persist`` so the read never does an inline fsync write.
    """
    cleaned, changed, settings_dict = _compute_reconcile(conv_id, r)
    if not changed:
        d = _conv_row_to_dict(r)
        try:
            _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
        except Exception as _rse:
            logger.debug('[get_conv] segments rehydrate (unchanged path) '
                         'conv=%s: %s', conv_id[:8], _rse)
        return d

    new_rev = _persist_reconcile(
        db, conv_id, cleaned, settings_dict, expected_rev=_row_rev(r))
    d = {
        'id': r['id'], 'title': r['title'],
        'messages': cleaned,
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': settings_dict,
        'rev': new_rev,
    }
    try:
        _rehydrate_segments_from_task_results(db, conv_id, d['messages'])
    except Exception as _rse:
        logger.debug('[get_conv] segments rehydrate (changed path) '
                     'conv=%s: %s', conv_id[:8], _rse)
    return d


def _parse_window_args():
    """Read the windowed-read query params. Returns ``(window, before_seq)``:
    ``window`` is the tail size (0/absent = no window), ``before_seq`` is the
    page-up cursor (None = tail). Both best-effort int-parsed; junk → default.
    """
    try:
        window = int(request.args.get('window', '') or 0)
    except (TypeError, ValueError):
        window = 0
    if window < 0:
        window = 0
    elif window > _MAX_CONV_WINDOW:
        window = _MAX_CONV_WINDOW
    before_seq = None
    bs = request.args.get('before_seq', '')
    if bs:
        try:
            before_seq = int(bs)
        except (TypeError, ValueError):
            before_seq = None
    return window, before_seq


def _projected_blob_window_sql(backend, before_seq=None):
    """Build an authoritative JSON-array slice query for one conversation.

    The previous "safe blob" path selected the complete ``messages`` value
    into Python and sliced it there.  That bounded browser traffic but still
    made PostgreSQL send and Python decode up to hundreds of MB per GET.  This
    query computes the same absolute-index window inside the database and
    returns only the selected JSON objects plus their pagination bounds.

    Returns ``(sql, has_before_bind)``.  Bind order is always ``id, user_id``,
    optional ``before_seq``, then ``window``.
    """
    from lib.database.messages_rows import _window_meta_expr

    if backend == 'pg':
        json_value = (
            "CASE WHEN jsonb_typeof(messages)='array' THEN messages "
            "ELSE '[]'::jsonb END"
        )
        length_expr = 'jsonb_array_length(all_messages)'
        clamp_end = ('LEAST(total_count, GREATEST(0, ?))'
                     if before_seq is not None else 'total_count')
        clamp_start = 'GREATEST(0, slice_end - ?)'
        # Materialize each selected element once. Without this fence PostgreSQL
        # inlines ``all_messages -> g.i`` at every reference inside the light
        # projection; a single 48 MB tool message was extracted repeatedly and
        # made the supposedly bounded fallback slower than the legacy read.
        projected_item = _window_meta_expr('pg', 'item')
        aggregate = (
            "COALESCE((WITH items AS MATERIALIZED ("
            "SELECT g.i, all_messages -> g.i AS item "
            "FROM generate_series(slice_start, slice_end - 1) AS g(i)) "
            "SELECT jsonb_agg(" + projected_item + " ORDER BY i) FROM items), "
            "'[]'::jsonb)"
        )
    else:
        json_value = (
            "CASE WHEN json_valid(messages) THEN messages ELSE '[]' END"
        )
        length_expr = 'json_array_length(all_messages)'
        clamp_end = ('min(total_count, max(0, ?))'
                     if before_seq is not None else 'total_count')
        clamp_start = 'max(0, slice_end - ?)'
        # json_each emits array keys in ascending order.  The key predicates
        # select the exact absolute-index window; json(value) prevents JSON
        # objects from being double-quoted by json_group_array.
        projected_item = _window_meta_expr('sqlite', 'j.value')
        aggregate = (
            "COALESCE((SELECT json_group_array(json(" + projected_item + ")) "
            "FROM json_each(all_messages) AS j "
            "WHERE CAST(j.key AS INTEGER) >= slice_start "
            "AND CAST(j.key AS INTEGER) < slice_end), '[]')"
        )
    sql = (
        'WITH target AS ('
        'SELECT id, title, created_at, updated_at, settings, rev, ' +
        json_value + ' AS all_messages FROM conversations '
        'WHERE id=? AND user_id=?), '
        'sized AS (SELECT *, ' + length_expr + ' AS total_count FROM target), '
        'bounded AS (SELECT *, ' + clamp_end + ' AS slice_end FROM sized), '
        'windowed AS (SELECT *, ' + clamp_start + ' AS slice_start FROM bounded) '
        'SELECT id, title, created_at, updated_at, settings, rev, '
        'total_count, slice_start, slice_end, ' + aggregate + ' AS messages '
        'FROM windowed'
    )
    return sql, before_seq is not None


def _windowed_served_readonly(db, conv_id, r, window, before_seq):
    """Serve a WINDOW of the conversation from the normalized row store.

    The root-cause fix for slow first-open of long conversations: read only the
    tail ``window`` messages via ``load_message_window`` (cost O(window), not
    O(history)) instead of detoasting + parsing the whole ``messages`` blob.

    reconcile runs WITHIN the tail window: a ghost tail / superseded-error-husk
    can only be at the very end, so the tail window is sufficient to reach the
    same verdict WITHOUT deserializing the full history. The cache-prefix
    protection guards the HEAD prefix, which the tail window never includes, so
    a windowed reconcile can never delete a prefix message. When the window
    reconcile shortens the tail, the change is persisted off-request exactly
    like the full path (``_schedule_reconcile_persist`` on the full cleaned
    list) so the authoritative JSONB + rows stay correct.

    Returns ``(served, changed, cleaned_full, settings_dict)`` mirroring
    ``_reconcile_conv_served_readonly``; ``cleaned_full`` is None unless a
    tail-window reconcile fired (then it is the FULL cleaned list to persist).
    Raises on any row-store failure so the caller can fail open to the blob.
    """
    from lib.database.messages_rows import load_message_window

    win = load_message_window(
        db, conv_id, limit=window, before_seq=before_seq,
        project_heavy=True,
        # Older internal callers/tests pass a legacy metadata row without the
        # materialized count; retain the helper's self-contained COUNT fallback
        # for that shape while production GETs reuse the already-verified value.
        known_total=(r['msg_count'] if 'msg_count' in r.keys() else None),
    )
    win_msgs = win['messages']

    base = {
        'id': r['id'], 'title': r['title'],
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _decode_json_value(
            r['settings'], default={}, label='settings') or {},
        'rev': _row_rev(r),
        # pagination envelope the frontend uses for scroll-up loading
        'windowed': True,
        'trimmed': True,
        'totalCount': win['totalCount'],
        'firstLoadedSeq': win['firstLoadedSeq'],
        'lastLoadedSeq': win['lastLoadedSeq'],
        'hasMore': win['hasMore'],
    }

    # A page-up request (before_seq set) is a pure slice — never reconcile it,
    # only the tail (before_seq=None) can carry a ghost/husk.
    if before_seq is not None:
        base['messages'] = _trim_heavy_for_window(win_msgs)
        return base, False, None, None

    # Tail window: run reconcile on the window only. reconcile is idempotent and
    # operates on trailing pairs, so the tail window verdict matches the full
    # verdict for trailing ghost/husk.
    try:
        from lib.conversations.reconcile import reconcile_conversation_messages
        cleaned_win, changed = reconcile_conversation_messages(win_msgs, 0)
    except Exception as e:
        logger.debug('[get_conv] windowed reconcile skipped conv=%s: %s',
                     conv_id[:8], e)
        cleaned_win, changed = win_msgs, False

    # Match the authoritative JSON projection exactly: reconcile needs the
    # complete round objects, but first paint does not. Serving the normalized
    # mirror must never reintroduce the multi-MB tool/image payloads that the
    # blob window path deliberately keeps off the wire.
    base['messages'] = _trim_heavy_for_window(cleaned_win)
    if not changed:
        return base, False, None, None

    # The window shortened. Compute the FULL cleaned list to persist off-request
    # (the persisted JSONB stays authoritative). Reconcile the full array once
    # here — this is the ONLY full deserialize, and only when the tail actually
    # changed (rare), so the common open stays O(window).
    settings_dict = base['settings'] if isinstance(base['settings'], dict) else {}
    settings_dict['_reconciledAt'] = int(time.time() * 1000)
    base['settings'] = settings_dict
    # Authoritative FULL verdict + settings for the deferred persist. This is
    # the only full-array deserialize on the windowed path, and only when the
    # tail actually changed (rare) — the common open stays O(window).
    cleaned_full, settings_for_persist = None, None
    # ``r`` may be metadata-only on the normalized-row fast path.  Fetch the
    # authoritative blob only after a tail reconcile actually changed
    # something (rare); the common windowed GET never transfers it.
    from lib.database.conversation_repository import load_conversation
    full_r = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('title', 'created_at', 'updated_at', 'settings'))
    c_full, ch_full, sd_full = _compute_reconcile(conv_id, full_r or r)
    if ch_full:
        cleaned_full, settings_for_persist = c_full, sd_full
    return base, bool(cleaned_full is not None), cleaned_full, settings_for_persist



# Heavy per-message fields stripped from a WINDOWED serve so the response body
# is bounded by BYTES, not just message count. These restate content already
# rendered elsewhere or drive only the (lazy) tool-timeline / finish fold:
#   • segments / toolRounds — the interleaved tool timeline + raw per-round
#     transcripts (the bulk of a heavy assistant turn's bytes).
#   • _continueToolRounds — regen/continue-only, never first-paint.
#   • toolSummary — written by the stream but READ by no renderer (dead weight).
# The renderer degrades gracefully to a plain content/thinking render when these
# are absent (chat_render.js guards on Array.isArray(segments)&&length, and
# getToolRoundsFromMsg → []); the frontend lazy-hydrates the full message via the
# existing _needsLoad + full (non-windowed) refetch seam when the tool timeline
# is exercised. This trim is READ-ONLY on the serve path — the authoritative
# blob keeps every field, and the PUT path refills any trimmed field back from
# the stored blob by _msgId (see _save_conv_blocking's heavy-field preservation).
#
# ★ apiRounds / _continueApiRounds are DELIBERATELY NOT removed: the cost
#   popover's per-round breakdown table renders only when apiRounds.length > 1
#   (finish_info.js), and it is NEVER refilled by hydrateFullConversation (only
#   the tool-timeline button triggers that) — so trimming it made a reloaded
#   long conversation silently lose its per-round token/cost table. Their bulk
#   (usage._wire_fp, ~226 KB/round) is already stripped at persist time by
#   the shared storage projection. Historical rows can predate that cleanup,
#   so the transport projection runs the same sanitizer again: the round/cost/
#   token fields survive, while backend-only ``usage._wire_*`` diagnostics never
#   cross into the browser. The exact field tuple lives beside the pure helper
#   in ``lib.storage_projection`` so DB and HTTP projections cannot drift.


def _trim_heavy_for_window(messages):
    """Return a shallow-copied message list with heavy fields stripped for
    transport, each trimmed message stamped ``_trimmed=True`` + light counts so
    the frontend knows tool activity existed and can lazy-hydrate on demand.

    Pure + read-only: never mutates the input dicts (shallow-copies only the
    messages it trims), so the caller's authoritative array is untouched.
    """
    # One pure source is shared by row persistence, HTTP transport, and the
    # offline projection compactor. It retains every UI-visible usage/cost
    # field while stripping timeline bulk and all historical wire diagnostics.
    from lib.storage_projection import project_message_for_window
    return [project_message_for_window(message) for message in messages]


def _windowed_blob_slice_readonly(conv_id, r, window, before_seq):
    """Serve a WINDOW by tail-slicing the AUTHORITATIVE ``messages`` JSONB blob.

    The safe, migration-flag-independent default for windowed reads. It parses
    the whole authoritative blob (cheap: ~0.02s even at 6 MB — the blob was
    never the bottleneck) but returns only the tail ``window`` messages, so the
    *response body* shrinks from megabytes to the window size. That is what cuts
    the client-side first-open cliff (network transfer + browser JSON parse over
    the tunnel), NOT server CPU.

    Unlike :func:`_windowed_served_readonly` (which reads the ``conversation_messages``
    row store and is gated on ``rows_read_enabled()``), this path reads the same
    always-complete, always-authoritative array every other read path uses — so
    it is correct for the 116 not-yet-backfilled convs where the row store would
    serve an empty/short window and risk a PUT truncating real history.

    ``seq`` is the message's index in the full array — identical to the row
    store's ``seq`` (``backfill_conv`` assigns ``enumerate``), so the envelope
    and the ``before_seq`` page-up cursor are interchangeable between the two
    paths and the frontend needs no branch.

    Returns ``(served, changed, cleaned_full, settings_dict)`` mirroring
    :func:`_windowed_served_readonly`. Raises on a genuinely malformed blob so
    the caller fails open to the full-blob path.
    """
    messages = _decode_json_value(
        r['messages'], default=[], label='messages')
    if not isinstance(messages, list):
        messages = []
    total = len(messages)

    # Resolve the slice bounds. seq == array index. Tail window = the newest
    # `window`; page-up (before_seq set) = the `window` messages ending just
    # before that seq.
    if before_seq is not None:
        end = max(0, min(int(before_seq), total))
        start = max(0, end - window)
    else:
        start = max(0, total - window)
        end = total
    win_msgs = messages[start:end]

    first_seq = start if win_msgs else None
    last_seq = (end - 1) if win_msgs else None
    has_more = bool(first_seq is not None and first_seq > 0)

    base = {
        'id': r['id'], 'title': r['title'],
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _decode_json_value(
            r['settings'], default={}, label='settings') or {},
        'rev': _row_rev(r),
        'windowed': True,
        # Heavy per-message fields (toolRounds/segments/apiRounds/...) are
        # trimmed from the served messages for transport; the frontend
        # lazy-hydrates them on demand and the PUT path refills them from the
        # stored blob so nothing is ever persisted trimmed.
        'trimmed': True,
        'totalCount': total,
        'firstLoadedSeq': first_seq,
        'lastLoadedSeq': last_seq,
        'hasMore': has_more,
    }

    # A page-up request is a pure slice — never reconcile it; only the tail
    # (before_seq=None) can carry a trailing ghost/husk. Trim heavy fields for
    # transport (read-only; the authoritative array is untouched).
    if before_seq is not None:
        base['messages'] = _trim_heavy_for_window(win_msgs)
        return base, False, None, None

    # Tail window: reconcile the window only, on the UNTRIMMED slice (reconcile
    # inspects toolRounds to spot a trailing ghost), THEN trim for transport.
    # reconcile operates on trailing pairs, so the tail-window verdict matches
    # the full verdict for a trailing ghost/husk (the HEAD prefix — which the
    # window excludes — is never touched).
    try:
        from lib.conversations.reconcile import reconcile_conversation_messages
        cleaned_win, changed = reconcile_conversation_messages(win_msgs, 0)
    except Exception as e:
        logger.debug('[get_conv] blob-slice reconcile skipped conv=%s: %s',
                     conv_id[:8], e)
        cleaned_win, changed = win_msgs, False

    base['messages'] = _trim_heavy_for_window(cleaned_win)
    if not changed:
        return base, False, None, None

    # The tail shortened — compute the FULL cleaned list to persist off-request
    # so the authoritative JSONB converges (same as the full path). This is the
    # only place we walk the whole array, and only when the tail actually
    # changed (rare), so the common open stays bounded to the window.
    cleaned_full, settings_for_persist = None, None
    c_full, ch_full, sd_full = _compute_reconcile(conv_id, r)
    if ch_full:
        cleaned_full, settings_for_persist = c_full, sd_full
    return base, bool(cleaned_full is not None), cleaned_full, settings_for_persist


def _windowed_projected_blob_readonly(db, conv_id, r, before_seq,
                                      allow_reconcile=True):
    """Serve a database-projected authoritative message window.

    ``r['messages']`` is already the exact absolute-index slice produced by
    :func:`_projected_blob_window_sql`; unlike
    :func:`_windowed_blob_slice_readonly`, this function never receives the
    full JSON array.  Pagination and reconcile semantics stay identical.
    """
    messages = _decode_json_value(
        r['messages'], default=[], label='messages-window')
    if not isinstance(messages, list):
        raise ValueError('projected messages window is not a list')
    try:
        total = int(r['total_count'] or 0)
        start = int(r['slice_start'] or 0)
        end = int(r['slice_end'] or 0)
    except (TypeError, ValueError) as e:
        raise ValueError('invalid projected window bounds') from e
    if start < 0 or end < start or end > total or len(messages) != end - start:
        raise ValueError(
            'projected window bounds mismatch: total=%d start=%d end=%d rows=%d'
            % (total, start, end, len(messages)))

    base = {
        'id': r['id'], 'title': r['title'],
        'createdAt': r['created_at'], 'created_at': r['created_at'],
        'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
        'settings': _decode_json_value(
            r['settings'], default={}, label='settings') or {},
        'rev': _row_rev(r),
        'windowed': True,
        'trimmed': True,
        'totalCount': total,
        'firstLoadedSeq': start if messages else None,
        'lastLoadedSeq': (end - 1) if messages else None,
        'hasMore': bool(messages and start > 0),
    }

    # A page-up is always a pure slice.  A live-task window is also pure: its
    # trailing placeholder must never be classified while the worker owns it,
    # but serving the authoritative tail itself is safe and prevents every
    # live sync probe from falling back to a hundreds-of-MB full GET.
    if before_seq is not None or not allow_reconcile:
        base['messages'] = _trim_heavy_for_window(messages)
        return base, False, None, None

    try:
        from lib.conversations.reconcile import reconcile_conversation_messages
        cleaned_win, changed = reconcile_conversation_messages(messages, 0)
    except Exception as e:
        logger.debug('[get_conv] projected-window reconcile skipped conv=%s: %s',
                     conv_id[:8], e)
        cleaned_win, changed = messages, False
    base['messages'] = _trim_heavy_for_window(cleaned_win)
    if not changed:
        return base, False, None, None

    # Reconcile changed the tail.  Fetch the full authoritative row only in
    # this rare repair case so the deferred write can preserve the untouched
    # prefix exactly.
    from lib.database.conversation_repository import load_conversation
    full_r = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('title', 'created_at', 'updated_at', 'settings'))
    if not full_r:
        return base, False, None, None
    cleaned_full, changed_full, settings_for_persist = _compute_reconcile(
        conv_id, full_r)
    return (base, bool(changed_full),
            cleaned_full if changed_full else None,
            settings_for_persist if changed_full else None)


def _maybe_backfill_narration_on_open(conv_id, conv_dict):
    """FORWARD fix for the "tool prose reappears but stays English" gap.

    A turn translated before its narration segments were stamped keeps its
    Chinese only in the bottom ``translatedContent`` — the interleaved narration
    segments lack ``translatedText`` and no client/server path re-requests it
    (both ``needsTranslation`` and the retro guard treat the turn as done). On
    conversation OPEN, when auto-translate is on for this conv and it carries any
    such candidate, spawn a BACKGROUND task that translates + stamps the missing
    narration segments (reusing the live translate core; rev-CAS neutral). The
    stamped Chinese then surfaces on the next open / re-render.

    Fire-and-forget: makes LLM calls, so it MUST run off the GET path and never
    block or fail the response. Guarded on a cheap candidate pre-check so the
    common (fully-stamped) conversation does zero extra work. No-op when
    auto-translate is off or no event loop is running (sync caller).
    """
    try:
        settings = conv_dict.get('settings') if isinstance(conv_dict, dict) else None
        from lib.conv_config import resolve_auto_translate
        if not resolve_auto_translate(settings if isinstance(settings, dict) else {}):
            return
        from lib.translate.segment_backfill import (
            backfill_conv_narration_segments, conv_has_backfill_candidates,
            is_backfill_inflight)
        if is_backfill_inflight(conv_id):
            # A backfill for this conv is already running; the candidate gate
            # reads the still-uncommitted served messages, so spawning again
            # would burn duplicate LLM calls for the same segments.
            return
        if not conv_has_backfill_candidates(conv_dict.get('messages')):
            return
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(backfill_conv_narration_segments(conv_id))
        logger.info('[get_conv] conv=%s spawned on-open narration backfill', conv_id[:8])
    except RuntimeError:
        # No running loop (sync context) — skip; the migration covers offline rows.
        pass
    except Exception as e:
        logger.debug('[get_conv] narration backfill spawn skipped conv=%s: %s',
                     conv_id[:8], e)


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

    With a ``window`` query param, the conversation is sliced inside the
    database before crossing into Python. During migration, a verified row
    mirror is the O(window) fast path and the JSON archive remains fallback.
    After row-authority cutover, even live streams use their transactionally
    maintained rows and no path can fall back to the frozen archive.
    """
    _window, _before_seq = _parse_window_args()
    _is_live = _conv_has_live_task(conv_id)
    r = None

    # ── Windowed read (fail-open). Select metadata only for a verified row
    # store, or ask the authoritative JSON column to return only the requested
    # absolute-index slice.  In both cases the full blob never crosses the
    # DB/Python boundary on the common path. ──
    if _window > 0:
        try:
            try:
                from lib.database.messages_rows import rows_read_enabled
                from lib.database.conversation_repository import (
                    conversation_rows_authoritative,
                )
                _authority = conversation_rows_authoritative()
                _use_rows = rows_read_enabled()
            except Exception as e:
                logger.debug('[get_conv] rows_read_enabled check failed conv=%s: %s',
                             conv_id[:8], e)
                _authority = False
                _use_rows = False
            # Transitional mirrors remain best-effort during a live task. In
            # authority mode, row writes are part of the SAME transaction as
            # the revision/metadata update, so live reads must use them too.
            _use_rows = bool(_use_rows and (_authority or not _is_live))
            if _authority and not _use_rows:
                raise RuntimeError('normalized transcript authority is unavailable')
            if _use_rows:
                r = await async_fetchone(
                    'SELECT id, title, created_at, updated_at, settings, rev, msg_count '
                    'FROM conversations WHERE id=? AND user_id=?',
                    (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
                if not r:
                    return api_not_found('Not found')
                # A global flag proves only operator intent, not that THIS
                # conversation has a complete mirror.  Fail closed to the
                # authoritative JSON projection on missing OR stale-extra rows.
                from lib.conv_ref._detail import row_window_usable
                _use_rows = await run_pooled(
                    lambda db: row_window_usable(
                        db, conv_id, int(r['msg_count'] or 0)))
                if _authority and not _use_rows:
                    raise RuntimeError(
                        f'canonical message rows are not current for {conv_id}')
            if _use_rows:
                served, changed, cleaned_full, sd = await run_pooled(
                    lambda db: _windowed_served_readonly(
                        db, conv_id, r, _window, _before_seq))
            else:
                from lib.database import _BACKEND
                slice_sql, has_before = _projected_blob_window_sql(
                    _BACKEND, _before_seq)
                slice_params = [conv_id, DEFAULT_USER_ID]
                if has_before:
                    slice_params.append(_before_seq)
                slice_params.append(_window)
                r = await async_fetchone(
                    slice_sql, tuple(slice_params), domain=DOMAIN_CHAT)
                if not r:
                    return api_not_found('Not found')
                served, changed, cleaned_full, sd = await run_pooled(
                    lambda db: _windowed_projected_blob_readonly(
                        db, conv_id, r, _before_seq,
                        allow_reconcile=not _is_live))
            if changed and cleaned_full is not None:
                _schedule_reconcile_persist(conv_id, cleaned_full, sd,
                                            expected_rev=_row_rev(r))
            _maybe_backfill_narration_on_open(conv_id, served)
            return api_ok(served)
        except Exception as e:
            logger.warning('[get_conv] windowed read failed conv=%s: %s — '
                           'delegating to repository authority path',
                           conv_id[:8], e)
            r = None

    # Full read is now reserved for explicit ``window=0``, live tasks (whose
    # in-flight placeholder must stay intact), and the fail-open path above.
    if r is None:
        from lib.database.conversation_repository import load_conversation
        r = await run_pooled(lambda db: load_conversation(
            db, conv_id, user_id=DEFAULT_USER_ID,
            metadata_columns=(
                'title', 'created_at', 'updated_at', 'settings')))
    if not r:
        return api_not_found('Not found')

    # GATE 1 (live-task): reconcile ONLY when the conv is idle. A pending/running
    # task's empty placeholder is indistinguishable from a ghost tail; deleting
    # it would corrupt the live stream. Skip reconcile AND leave _reconciledAt
    # unstamped so the frontend keeps deferring rather than treating it as clean.
    if _is_live:
        _served = _conv_row_to_dict(r)
        _maybe_backfill_narration_on_open(conv_id, _served)
        return api_ok(_served)

    try:
        served, changed, cleaned, settings_dict = await run_pooled(
            lambda db: _reconcile_conv_served_readonly(db, conv_id, r))
        # ── Read path is now write-free: the response carries the cleaned
        #    state immediately (correct for the OPENING client), and the
        #    persist + history_rewrite push are deferred off-request so this GET
        #    never blocks on a FUSE-fsync UPDATE+commit. ──
        if changed:
            _schedule_reconcile_persist(conv_id, cleaned, settings_dict,
                                        expected_rev=_row_rev(r))
        _maybe_backfill_narration_on_open(conv_id, served)
        return api_ok(served)
    except Exception as e:
        logger.warning('[get_conv] GET-path reconcile failed for conv=%s: %s — '
                       'serving unreconciled row', conv_id[:8], e, exc_info=True)
        _served = _conv_row_to_dict(r)
        _maybe_backfill_narration_on_open(conv_id, _served)
        return api_ok(_served)


_MESSAGE_ACTIVITY_FIELDS = (
    'segments', 'toolRounds', 'apiRounds',
    '_continueToolRounds', '_continueApiRounds', 'toolSummary',
)


@conversations_bp.route(
    '/api/v1/conversations/<conv_id>/messages/by-id/<msg_id>/activity',
    methods=['GET'],
)
@_db_safe
@api_meta(
    summary='Load one message execution activity by stable id',
    description=(
        'Returns only the execution-history fields stripped from windowed '
        'conversation reads. The stable `_msgId` must resolve exactly once; '
        'missing and duplicate ids fail instead of falling back to an array '
        'position.'),
    tags=['conversations'], scope='conversations',
)
async def get_message_activity(conv_id, msg_id):
    """Load one message's heavy execution fields without hydrating its chat."""
    return _finish(await run_pooled(
        lambda db: _get_message_activity_blocking(db, conv_id, msg_id)))


def _get_message_activity_blocking(db, conv_id, msg_id):
    from lib.database.conversation_repository import load_conversation

    snapshot = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID)
    if snapshot is None:
        return _nf('Conversation not found')

    messages = snapshot.messages
    if not isinstance(messages, list):
        logger.error('[message_activity] conv=%s messages is not an array',
                     conv_id[:8])
        return _ie('invalid_messages')

    matches = [
        (idx, message)
        for idx, message in enumerate(messages)
        if isinstance(message, dict) and message.get('_msgId') == msg_id
    ]
    if not matches:
        logger.info('[message_activity] conv=%s msgId=%s not found in %d messages',
                    conv_id[:8], msg_id[:12], len(messages))
        return _nf('message_id_not_found')
    if len(matches) != 1:
        logger.error('[message_activity] conv=%s msgId=%s is ambiguous (%d matches)',
                     conv_id[:8], msg_id[:12], len(matches))
        return _Defer(
            api_conflict, 'duplicate_message_id', matchCount=len(matches))

    idx, message = matches[0]
    activity = {
        field: message[field]
        for field in _MESSAGE_ACTIVITY_FIELDS
        if field in message
    }
    return _ok({
        'msgId': msg_id,
        'idx': idx,
        'activity': activity,
    })


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

    # A hover preview needs ONE user message, not the whole conversation. On a
    # verified row mirror, select metadata first and read exactly that row.
    # A stale/missing mirror fails closed to the legacy blob path below.
    row_candidate = None
    try:
        from lib.database.messages_rows import rows_read_enabled
        use_rows = rows_read_enabled()
    except Exception as exc:
        logger.debug('[conv_preview] row-store flag probe failed: %s', exc)
        use_rows = False

    if use_rows:
        row_candidate = await async_fetchone(
            'SELECT id, title, msg_count, rev FROM conversations '
            'WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
        if not row_candidate:
            metadata_rows = await async_fetchall(
                'SELECT id, title, msg_count, rev FROM conversations '
                'WHERE id LIKE ? AND user_id=? LIMIT 2',
                (conv_id + '%', DEFAULT_USER_ID), domain=DOMAIN_CHAT)
            if len(metadata_rows) == 1:
                row_candidate = metadata_rows[0]
            elif len(metadata_rows) > 1:
                logger.debug('[conv_preview] ambiguous row-store prefix=%s',
                             conv_id[:12])
                return api_not_found('Not found')
        if row_candidate:
            def _read_row_preview(db):
                from lib.conversations import first_user_text
                from lib.database.messages_rows import (
                    mirror_is_current,
                    row_to_message,
                )
                resolved_id = row_candidate['id']
                if not mirror_is_current(
                        db, resolved_id,
                        expected_count=int(row_candidate['msg_count'] or 0),
                        expected_rev=int(row_candidate['rev'] or 0)):
                    return False, ''
                first = db.execute(
                    'SELECT meta FROM conversation_messages '
                    "WHERE conv_id=? AND role='user' ORDER BY seq LIMIT 1",
                    (resolved_id,)).fetchone()
                message = row_to_message(first) if first else None
                return True, first_user_text(
                    [message] if message is not None else [])

            usable, first_user = await run_pooled(_read_row_preview)
            if usable:
                return api_ok({
                    'id': row_candidate['id'],
                    'title': row_candidate['title'] or '',
                    'firstUserMessage': first_user,
                    'msgCount': row_candidate['msg_count'] or 0,
                })

    # Compatibility path: resolve the id/prefix, then let the repository choose
    # rows vs the transitional archive from one consistent snapshot.
    resolved_id = row_candidate['id'] if row_candidate else conv_id
    from lib.database.conversation_repository import load_conversation
    r = await run_pooled(lambda db: load_conversation(
        db, resolved_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('title',)))
    if not r and row_candidate is None:
        # The Project Brain panel — and the peer-message feed payload it renders
        # from — sometimes carries a TRUNCATED 8-char conversation id (the [:8]
        # display form) instead of the full 14-char id. A peer note's `toConv`,
        # in particular, is stored short whenever the acting agent addressed the
        # target by its displayed short id, so an exact lookup misses and the
        # hover falls back to "Untitled / no messages". Resolve the short id by
        # unique prefix; accept the match only when it is unambiguous.
        rows = await async_fetchall(
            'SELECT id FROM conversations '
            'WHERE id LIKE ? AND user_id=? LIMIT 2',
            (conv_id + '%', DEFAULT_USER_ID), domain=DOMAIN_CHAT)
        if len(rows) == 1:
            r = await run_pooled(lambda db: load_conversation(
                db, rows[0]['id'], user_id=DEFAULT_USER_ID,
                metadata_columns=('title',)))
        else:
            logger.debug('[conv_preview] no unique match for id/prefix=%s (%d rows)',
                         conv_id[:12], len(rows))
            return api_not_found('Not found')
    elif not r:
        return api_not_found('Not found')

    def _extract():
        from lib.conversations import first_user_text
        return first_user_text(r.messages)

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
        return build_wire_messages(
            raw, config, mode='snapshot', conv_id=conv_id,
            return_manifest=True)

    try:
        built = await asyncio.to_thread(_build)
        if built is None:
            return api_not_found('Not found')
        messages, manifest = built
        try:
            messages = _strip_base64_for_snapshot(messages)
        except Exception as e:
            logger.warning('[debug_messages] strip_base64 failed for conv=%s: %s — '
                           'returning raw messages', conv_id[:8], e)
        return api_ok({
            'messages': messages, 'count': len(messages), 'approx': True,
            'contextManifest': manifest,
        })
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
    # Durable one-time heal for send-race duplicate user rows (consecutive
    # user rows sharing one ``timestamp`` → keep the LAST, the server-built
    # copy). The send route guards via ``append_user_msg_idempotent``, but
    # THIS full-conv PUT is the bypass seam that can re-plant the pair —
    # heal here (mirroring the ghost-husk sweep precedent below) so the DB
    # never holds it and the rebuild-side dedup stays a no-op. Never raises:
    # a heal failure must not break the write path.
    try:
        from lib.tasks_pkg.conv_message_builder._dedup import _dedup_duplicate_user_messages
        _healed = _dedup_duplicate_user_messages(raw_messages)
        if len(_healed) != len(raw_messages):
            logger.warning('[save_conv] Healed %d duplicate same-timestamp '
                           'user row(s) on incoming PUT conv=%s — durable '
                           'one-time fix (race-planted copies)',
                           len(raw_messages) - len(_healed), conv_id[:12])
            audit_log('conv_user_dup_healed', conv_id=conv_id,
                      dropped=len(raw_messages) - len(_healed), seam='save_conv_put')
        raw_messages = _healed
    except Exception as _de:
        logger.warning('[save_conv] user-dup heal failed conv=%s: %s '
                       '(continuing unhealed)', conv_id[:12], _de)
    msg_count = len(raw_messages)
    # Backfill stable per-message IDs.  Once present, _msgId carries
    # forward in subsequent loads/syncs.  Index-free addressing depends
    # on every message having an id, so we assign on every write.
    try:
        from lib.tasks_pkg.manager import _assign_message_ids as _amid
        _amid(raw_messages)
    except Exception as _e:
        logger.debug('[save_conv] _assign_message_ids unavailable: %s', _e)
    created = data.get('createdAt') or data.get('created_at') or int(time.time() * 1000)
    updated = data.get('updatedAt') or data.get('updated_at') or int(time.time() * 1000)
    # ★ Inject lastMsgRole/lastMsgTimestamp into settings for Case E orphan detection.
    # This ensures metadata shells always have last-message info even when the
    # frontend didn't include it (e.g. server-side syncs from _sync_result_to_conversation).
    # ★ ALSO re-derive the settled-turn sidebar facts (lastFinishReason /
    # lastMsgError / lastMsgHasOutput) from the AUTHORITATIVE posted tail —
    # never from the client's settings payload, whose whitelist omits them:
    # previously every full-conv PUT silently clobbered the manager-stamped
    # error facts, and the meta-only sidebar shell lost its error/incomplete
    # dot until somebody re-opened the conversation.
    from lib.chat.persistence import settled_turn_facts
    settings_dict = data.get('settings') or {}
    if msg_count > 0:
        settings_dict.update(settled_turn_facts(raw_messages[-1]))
    else:
        for _fact_k in ('lastMsgRole', 'lastMsgTimestamp', 'lastFinishReason',
                        'lastMsgError', 'lastMsgHasOutput'):
            settings_dict.pop(_fact_k, None)
    settings = json.dumps(settings_dict, ensure_ascii=False)

    # ── Guard: prevent stale syncs from overwriting newer data ──
    # A frontend sync captured lightMsgs before an await; by the time the PUT
    # arrives, a fresher sync with MORE messages may have already completed.
    # Reject PUTs with fewer messages unless the client explicitly signals
    # truncation (e.g. regen/edit sends allowTruncate=true).
    allow_truncate = data.get('allowTruncate', False)
    from lib.database.conversation_repository import load_conversation
    existing_row = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=(
            'updated_at', 'title', 'search_text', 'created_at', 'settings'))
    existing_count = existing_row['msg_count'] if existing_row else 0
    server_rev = _row_rev(existing_row) if existing_row else 0

    # Several correctness guards below need the authoritative old message
    # array.  They historically issued their own SELECT each, so one full PUT
    # could detoast and transfer the same hundreds-of-MB JSONB value up to four
    # times.  Load and parse it at most once per request.  The closure remains
    # lazy so fresh conversations and paths that need metadata only pay zero
    # blob I/O.
    _existing_messages_loaded = existing_row is not None
    _existing_messages_cache = (
        existing_row.messages if existing_row is not None else [])

    def _load_existing_messages_once():
        nonlocal _existing_messages_loaded, _existing_messages_cache
        if _existing_messages_loaded:
            return _existing_messages_cache
        return _existing_messages_cache

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
                # Re-derive lastMsgRole/lastMsgTimestamp (Case-E orphan detection)
                # + the settled-turn sidebar facts from the POST-sweep tail so
                # settings don't point at a removed husk.
                if msg_count > 0:
                    settings_dict.update(settled_turn_facts(raw_messages[-1]))
                    settings = json.dumps(settings_dict, ensure_ascii=False)
                logger.info('[save_conv] \U0001f9f9 Swept %d ghost husk(s) from '
                            'incoming PUT conv=%s (%d\u2192%d) — write-seam symmetry '
                            'with GET reconcile', _husk_swept, conv_id[:12],
                            _husk_swept + msg_count, msg_count)
            # (b) Husk-free view of the EXISTING row for the guards below (only
            #     needed when the incoming is NOT a strict growth).
            if existing_row is not None and existing_count > 0 and msg_count <= existing_count:
                _ex_msgs = _load_existing_messages_once()
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
            return _json({'ok': False, 'error': 'blocked_rev_conflict',
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
        return _json({'ok': False, 'error': 'blocked_empty_overwrite',
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
        return _json({'ok': False, 'error': 'blocked_msg_regression',
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
                existing_msgs = _load_existing_messages_once()
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
                        return _json({
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
            _db_msgs = _load_existing_messages_once()
            if _db_msgs:

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
    # ── Heavy-field preservation (data-loss guard for windowed/trimmed reads) ──
    # A windowed serve strips heavy per-message fields (segments, toolRounds,
    # apiRounds, _continue*) for transport, and _trimMsgForPersist strips some of
    # them on EVERY PUT regardless. A blind full-array replace would then drop
    # those fields from the authoritative blob permanently. So before the write,
    # refill any heavy field the incoming message LACKS from the stored blob,
    # matched by stable _msgId (fallback _taskId) so positional drift / a tail
    # window can never mismatch. Generalizes the original segments-only merge to
    # every trimmable heavy field. Skipped on allow_truncate (edit/regen owns
    # the tail and may legitimately drop fields).
    _HEAVY_PRESERVE_FIELDS = (
        'segments', 'toolRounds', 'apiRounds',
        '_continueToolRounds', '_continueApiRounds',
    )
    _seg_preserved = 0
    if msg_count > 0 and not allow_truncate:
        try:
            _seg_db_msgs = _load_existing_messages_once()
            if _seg_db_msgs:

                # Index DB messages that carry ANY heavy field, by stable id, so
                # we can refill regardless of positional shift. Value is a dict
                # of {field: stored_value} for the fields that DB row carries.
                _heavy_by_msgid = {}
                _heavy_by_taskid = {}
                for _dbm in _seg_db_msgs:
                    if not isinstance(_dbm, dict):
                        continue
                    _present = {f: _dbm[f] for f in _HEAVY_PRESERVE_FIELDS
                                if _dbm.get(f)}
                    if not _present:
                        continue
                    _mid = _dbm.get('_msgId')
                    _tid = _dbm.get('_taskId')
                    if _mid:
                        _heavy_by_msgid[_mid] = _present
                    if _tid:
                        _heavy_by_taskid[_tid] = _present

                if _heavy_by_msgid or _heavy_by_taskid:
                    for _dst in raw_messages:
                        if not isinstance(_dst, dict):
                            continue
                        _mid = _dst.get('_msgId')
                        _src = _heavy_by_msgid.get(_mid) if _mid else None
                        if _src is None:
                            _tid = _dst.get('_taskId')
                            _src = _heavy_by_taskid.get(_tid) if _tid else None
                        if not _src:
                            continue
                        for _f, _val in _src.items():
                            # Only refill a field the client did NOT send — never
                            # overwrite a fresh client value (e.g. a regen that
                            # legitimately rewrote toolRounds).
                            if not _dst.get(_f):
                                _dst[_f] = _val
                                _seg_preserved += 1
                        # A message served trimmed carries the _trimmed marker;
                        # once refilled it's whole again — drop the transient flag
                        # so it never persists into the authoritative blob.
                        _dst.pop('_trimmed', None)
                        _dst.pop('_trimmedToolRoundCount', None)

                if _seg_preserved > 0:
                    logger.info(
                        '[save_conv] 🧩 Preserved %d heavy field(s) from DB into '
                        'incoming payload conv=%s (windowed/trimmed-read guard)',
                        _seg_preserved, conv_id[:12],
                    )
        except Exception as _seg_me:
            logger.warning('[save_conv] heavy-field merge pre-step failed '
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

    # When the optional row mirror is engaged, derive its exact dirty set from
    # old authoritative truth BEFORE replacing the blob.  This closes the
    # same-count/non-tail hole in the old count heuristic without paying a
    # full mirror rebuild: O(message-count) comparisons in memory, then only
    # the genuinely changed rows are upserted.  The old array is already needed
    # by the preservation guards on normal PUTs, so this usually adds no DB I/O.
    _mirror_changed_seqs = None
    try:
        from lib.database.messages_rows import (
            changed_message_seqs, rows_write_enabled,
        )
        if rows_write_enabled():
            _mirror_old = _load_existing_messages_once()
            _mirror_changed_seqs = changed_message_seqs(
                _mirror_old, raw_messages)
    except Exception as _mirror_diff_err:
        logger.warning('[save_conv] mirror dirty-set derivation failed conv=%s: %s '
                       '(falling back to tail heuristic)',
                       conv_id[:12], _mirror_diff_err)

    from lib.database.conversation_repository import (
        replace_messages,
        upsert_conversation,
    )
    if existing_row is None:
        write_result = upsert_conversation(
            db, conv_id, raw_messages, user_id=DEFAULT_USER_ID, title=title,
            created_at=created, updated_at=updated, settings=settings,
            search_text=search_text, changed_seqs=_mirror_changed_seqs,
            full=False)
    else:
        write_result = replace_messages(
            db, conv_id, raw_messages, user_id=DEFAULT_USER_ID,
            expected_rev=server_rev,
            metadata={
                'title': title,
                'created_at': existing_row.get('created_at') or created,
                'updated_at': updated,
                'settings': settings,
            },
            changed_seqs=_mirror_changed_seqs,
            full=False)
        if not write_result.applied:
            logger.info('[save_conv] conv=%s repository CAS lost after guards; '
                        'refusing stale full-array overwrite', conv_id[:12])
            return _json({
                'ok': False, 'error': 'blocked_rev_conflict',
                'serverRev': server_rev,
            }, status=409)
    # Return the post-write rev (the trigger bumped it iff messages changed) so
    # a CAS-aware client advances its baseRev in lockstep and its NEXT PUT
    # carries a fresh base. A client that ignores `rev` is unaffected.
    new_rev = write_result.rev if write_result.rev is not None else server_rev
    # Event-driven cross-device sync: push the change (carrying the new rev) so
    # a sibling device reconciles this conv without a manual refresh. Rev-gated
    # on the client → self-echo is a cheap no-op.
    _notify_conv_changed(conv_id, rev=new_rev, user_id=_request_user_id())
    return _Defer(api_ok, rev=new_rev)


@conversations_bp.route('/api/v1/conversations/<conv_id>/settings', methods=['PATCH'])
@_db_safe
async def patch_conv_settings(conv_id):
    """Lightweight endpoint to merge new keys into a conversation's settings JSON.

    Unlike PUT (which requires full messages), this only touches the settings
    column — safe to call for shell conversations that haven't loaded messages.

    Body: { folderId?: str|null, pinned?: bool, ... }
    All keys in the body are merged into the existing settings dict. This write
    is settings-only: it deliberately never touches ``updated_at`` — opening /
    browsing a conversation must not rewrite its recency (owner directive
    2026-08-14; the old ``touchUpdatedAt`` open-bump was removed).

    Native-async: request body is awaited; the serialized read-merge-write
    (settings_store, which guards against clobbering a concurrent settings
    write) runs off-loop on a pooled connection via asyncio.to_thread.
    """
    data = await async_parse_body()
    if not data:
        return api_bad_request('No settings provided')

    # A stale bundle may still send the removed ``touchUpdatedAt`` control
    # flag; pop it so it cannot pollute the settings JSON (it no longer bumps
    # ``updated_at`` — browsing must not rewrite recency).
    data.pop('touchUpdatedAt', None)

    def _work(db):
        # Serialized read-merge-write (see settings_store) so a settings PATCH
        # doesn't clobber a concurrent activeTaskId / autopilot / tool-state
        # write on the same row.
        from lib.conversations import set_conversation_settings
        # A settings-only PATCH may carry ONLY the touch flag (no settings
        # keys left after the pop). set_conversation_settings with an empty
        # dict is a row-existence check that skips the UPDATE — exactly what
        # we want when the caller's sole intent is the updated_at bump.
        res = set_conversation_settings(
            conv_id, data, user_id=DEFAULT_USER_ID, db=db)
        return True if res is not None else None

    ok = await run_pooled(_work, domain=DOMAIN_CHAT)
    if ok is None:
        return api_not_found('Not found')
    # Metadata-only change (folder move / pin / activeTaskId): rev unchanged →
    # client does a debounced sidebar refresh, not a body refetch.
    _notify_conv_changed(conv_id, rev=None, user_id=_request_user_id())
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
    _notify_conv_changed(conv_id, rev=None, user_id=_request_user_id())
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

    from lib.database.conversation_repository import load_conversation
    snapshot = await run_pooled(lambda db: load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID))
    if snapshot is None:
        return api_not_found('Not found')
    messages = snapshot.messages
    if not messages:
        return api_bad_request('Conversation has no messages')

    title = await asyncio.to_thread(generate_conversation_title, messages, lang)

    await async_execute(
        'UPDATE conversations SET title=? WHERE id=? AND user_id=?',
        (title, conv_id, DEFAULT_USER_ID), domain=DOMAIN_CHAT)
    _notify_conv_changed(conv_id, rev=None, user_id=_request_user_id())
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
        return api_error('mode must be "single" or "turn"', status=400)
    # Stable-id addressing (mirrors chat_regenerate's truncateToMsgId): the
    # client sends the target's ``_msgId`` so a list-length drift between its
    # read and this request (e.g. a server-side ghost-sweep / reconcile shrank
    # the persisted messages) resolves to the CURRENT index instead of a stale
    # one — the root cause of "delete does nothing / deletes the wrong turn".
    msg_id = request.args.get('msgId') or None
    return _finish(await run_pooled(
        lambda db: _delete_message_blocking(db, conv_id, msg_idx, mode, msg_id)))


def _delete_message_blocking(db, conv_id, msg_idx, mode, msg_id=None):
    from lib.database.conversation_repository import load_conversation
    row = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('title', 'settings', 'created_at'))
    if row is None:
        return _nf('Not found')
    messages = row.messages

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
    now_ms = int(time.time() * 1000)

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
    created_at = row.get('created_at') or now_ms

    from lib.database.conversation_repository import replace_messages
    write_result = replace_messages(
        db, conv_id, messages, user_id=DEFAULT_USER_ID,
        expected_rev=row['rev'],
        metadata={'updated_at': now_ms, 'settings': settings_json},
        full=True)
    if not write_result.applied:
        return _json({'ok': False, 'error': 'blocked_rev_conflict'}, status=409)

    _notify_conv_changed(conv_id, rev=write_result.rev,
                         user_id=_request_user_id())
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
        return api_error('unsupported_keys', status=400, keys=unknown)

    return _finish(await run_pooled(lambda db: _patch_message_blocking(db, conv_id, msg_idx, data)))


def _patch_message_blocking(db, conv_id, msg_idx, data):
    from lib.database.conversation_repository import load_conversation
    row = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('settings',))
    if row is None:
        return _nf('Not found')
    messages = row.messages

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

    from lib.database.conversation_repository import replace_messages
    write_result = replace_messages(
        db, conv_id, messages, user_id=DEFAULT_USER_ID,
        expected_rev=row['rev'],
        metadata={'updated_at': now_ms, 'settings': settings_json},
        changed_seqs=[msg_idx], full=False)
    if not write_result.applied:
        return _json({'ok': False, 'error': 'blocked_rev_conflict'}, status=409)

    _notify_conv_changed(conv_id, rev=write_result.rev,
                         user_id=_request_user_id())
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
        return api_error('unsupported_keys', status=400, keys=unknown)

    return _finish(await run_pooled(lambda db: _patch_message_by_id_blocking(db, conv_id, msg_id, data)))


def _patch_message_by_id_blocking(db, conv_id, msg_id, data):
    from lib.database.conversation_repository import load_conversation
    row = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('settings',))
    if row is None:
        return _nf('Not found')
    messages = row.messages

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

    from lib.database.conversation_repository import replace_messages
    write_result = replace_messages(
        db, conv_id, messages, user_id=DEFAULT_USER_ID,
        expected_rev=row['rev'],
        metadata={'updated_at': now_ms, 'settings': settings_json},
        changed_seqs=[target_idx], full=False)
    if not write_result.applied:
        return _json({'ok': False, 'error': 'blocked_rev_conflict'}, status=409)

    _notify_conv_changed(conv_id, rev=write_result.rev,
                         user_id=_request_user_id())
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

    Query params:
        msgId: the anchor message's stable ``_msgId``. Authoritative when
            present — the server resolves the CURRENT absolute index by id, so
            a windowed-read client (whose ``conv.messages`` holds only a tail
            window, making its local ``msg_idx`` NOT the absolute index) still
            targets the correct message. ``msg_idx`` is the fallback.

    Returns:
        {ok, branchCount}

    Native-async: blocking DB body runs off-loop via ``run_pooled``.
    """
    msg_id = request.args.get('msgId') or None
    return _finish(await run_pooled(
        lambda db: _delete_branch_blocking(db, conv_id, msg_idx, branch_idx, msg_id)))


def _delete_branch_blocking(db, conv_id, msg_idx, branch_idx, msg_id=None):
    from lib.database.conversation_repository import load_conversation
    row = load_conversation(
        db, conv_id, user_id=DEFAULT_USER_ID,
        metadata_columns=('settings',))
    if row is None:
        return _nf('Not found')
    messages = row.messages

    # ── Stable-id resolution: msgId is authoritative, index is the fallback
    #    (mirrors delete_message). Drift-proof under windowed reads. ──
    if msg_id:
        from lib.tasks_pkg.manager import find_message_by_id
        _resolved_idx, _ = find_message_by_id(messages, msg_id)
        if _resolved_idx is not None:
            if _resolved_idx != msg_idx:
                logger.info('[delete_branch] conv=%s msgId=%s resolved to index %d '
                            '(client sent %d — drift corrected)',
                            conv_id[:8], str(msg_id)[:12], _resolved_idx, msg_idx)
            msg_idx = _resolved_idx

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
    try:
        settings = json.loads(row['settings'] or '{}')
    except (json.JSONDecodeError, TypeError) as _e_audit:
        logger.debug('[conversations] delete_branch caught %s: %s', type(_e_audit).__name__, _e_audit)
        settings = {}
    settings_json = json.dumps(settings, ensure_ascii=False)

    from lib.database.conversation_repository import replace_messages
    write_result = replace_messages(
        db, conv_id, messages, user_id=DEFAULT_USER_ID,
        expected_rev=row['rev'],
        metadata={'updated_at': now_ms, 'settings': settings_json},
        changed_seqs=[msg_idx], full=False)
    if not write_result.applied:
        return _json({'ok': False, 'error': 'blocked_rev_conflict'}, status=409)

    # Event-driven cross-device sync: a branch delete changes the conversation
    # body, so carry the post-write rev → a sibling tab with this conv open
    # refetches without a manual refresh. notify_conv_changed also invalidates
    # the sidebar meta cache, so it replaces the bare _invalidate_meta_cache().
    _notify_conv_changed(conv_id, rev=write_result.rev,
                         user_id=_request_user_id())
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
    # ── Stop the conversation's live work BEFORE wiping its rows ──
    # A conversation under autopilot is a self-spawning loop: each finished
    # turn's end-of-turn hook spawns a follow-up task. Deleting the conv row
    # without first aborting leaves that loop running against a conv that no
    # longer exists — it burns tokens and, worse, its terminal / checkpoint
    # write to ``task_results`` lands AFTER step-2's DELETE and re-inserts an
    # ORPHAN row (the "Conversation not found in DB — cannot sync result back"
    # signature). Abort + disarm the marker FIRST so no new follow-up can be
    # spawned after we wipe; the orphan-write RACE TAIL that cooperative abort
    # still leaves open is closed by the conv-existence guard in
    # ``_upsert_task_row``. Both are best-effort: an abort/disarm failure must
    # NEVER prevent the delete from completing (that would leave the user
    # unable to remove the conversation at all).
    try:
        from lib.tasks_pkg import abort_running_tasks_for_conv
        _n_aborted = abort_running_tasks_for_conv(conv_id)
        if _n_aborted:
            logger.info('[delete_conv] Aborted %d running task(s) for conv=%s '
                        'before delete', _n_aborted, conv_id[:12])
    except Exception as _ab_e:
        logger.warning('[delete_conv] pre-delete task abort failed for conv=%s '
                       '(continuing with delete): %s', conv_id[:12], _ab_e)
    try:
        from lib.message_queue import clear_autopilot_marker
        if clear_autopilot_marker(conv_id):
            logger.info('[delete_conv] Disarmed autopilot marker for conv=%s',
                        conv_id[:12])
    except Exception as _cm_e:
        logger.warning('[delete_conv] autopilot marker disarm failed for conv=%s '
                       '(continuing with delete): %s', conv_id[:12], _cm_e)
    # ── Cascade-cancel this conv's timer watchers BEFORE wiping its row ──
    # A timer_watchers row is keyed on conv_id, not FK-linked to conversations,
    # so deleting the conv leaves any active timer orphaned in the DB. On the
    # next restart resume_active_timers() would resurrect it against a conv that
    # no longer exists (an inline timer is now retired as orphaned, but a
    # background one would still inject into a ghost conv). Cancel them here so
    # a deleted conversation can never spawn a timer again. Best-effort: a
    # cancel failure must NEVER block the delete (mirrors the abort/disarm
    # blocks above).
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db as _get_sysdb
        _sysdb = _get_sysdb(DOMAIN_SYSTEM)
        _trows = _sysdb.execute(
            "SELECT id FROM timer_watchers WHERE conv_id=? AND status='active'",
            (conv_id,)).fetchall()
        if _trows:
            from lib.scheduler.timer import cancel_timer as _cancel_timer
            _n_timers = 0
            for _tr in _trows:
                _tid = _tr['id'] if isinstance(_tr, dict) else _tr[0]
                try:
                    if _cancel_timer(_tid):
                        _n_timers += 1
                except Exception as _te:
                    logger.warning('[delete_conv] cancel_timer(%s) failed for conv=%s '
                                   '(continuing): %s', _tid, conv_id[:12], _te)
            if _n_timers:
                logger.info('[delete_conv] Cancelled %d active timer(s) for conv=%s '
                            'before delete', _n_timers, conv_id[:12])
    except Exception as _tw_e:
        logger.warning('[delete_conv] timer cascade-cancel failed for conv=%s '
                       '(continuing with delete): %s', conv_id[:12], _tw_e)

    # Capture the conv's messages + timestamps BEFORE deleting so we can scope
    # the cost-cache invalidation to only the day(s) it contributed cost to.
    _conv_msgs, _conv_created, _conv_updated = [], 0, 0
    try:
        from lib.database.conversation_repository import load_conversation
        _row = load_conversation(
            db, conv_id, user_id=DEFAULT_USER_ID,
            metadata_columns=('created_at', 'updated_at'))
        if _row is not None:
            _conv_msgs = _row.messages
            _conv_created = _row['created_at'] or 0
            _conv_updated = _row['updated_at'] or 0
    except Exception as _e:
        logger.debug('[delete_conv] cost-day capture skipped: %s', _e)

    from lib.database.conversation_repository import delete_conversation
    deleted = delete_conversation(db, conv_id, user_id=DEFAULT_USER_ID)
    _deleted_committed = True
    # Event-driven cross-device sync: tell siblings this conv is gone so they
    # drop it from the sidebar (+ IDB cache) without a manual refresh.
    if _deleted_committed:
        _notify_conv_changed(conv_id, deleted=True, user_id=_request_user_id())
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
    logger.info('[delete_conv] Deleted conv %s '
                '(rows: conv=%d, messages=%d, tasks=%d, transcripts=%d)',
                conv_id[:12], deleted.conversation_rows, deleted.message_rows,
                deleted.task_rows, deleted.archive_rows)
    return _ok()


# ════════════════════════════════════════════════════════════════════════════
#  Endpoints moved to companion modules
# ════════════════════════════════════════════════════════════════════════════
#  /api/conversations/<id>/compactions[/<archive_id>] → routes/conversations_compaction.py
#  /api/conversations/search                          → routes/conversations_search.py
# Both register on the same conversations_bp via side-effect imports in routes/__init__.py.

"""Server-side auto-translate safety net (assistant + endpoint-critic messages).

Extracted from ``lib/tasks_pkg/manager.py`` (2026-06-24). This is the
server-side guarantee that an assistant reply (or endpoint-mode critic review)
gets translated even when the frontend is offline / switched away / the SSE
stream closed early. It honours the per-conversation ``autoTranslate`` setting,
dedups against an already-running frontend translate task, detects + re-does
stale partial translations, short-circuits already-Chinese content, and hands
off to the incremental per-round translator when one is active.

Called from ``manager._sync_result_to_conversation`` (single-turn safety net)
and ``endpoint._trigger_*_auto_translate`` (per-turn + final). ``manager`` and
``endpoint`` import these back, so call sites are unchanged. Dependency is
one-directional: this module imports DB helpers from ``lib.database`` and the
translate engine lazily from ``lib.translate``/``lib.text_lang`` — never
``manager``.
"""

import json
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
from lib.log import get_logger

logger = get_logger(__name__)


def _maybe_auto_translate_assistant(conv_id, content, msg_idx, db=None, task=None):
    """Automatically translate the assistant's response on the server side.

    Called from _sync_result_to_conversation after the assistant content is persisted.
    This is the server-side safety net — ensures translation happens even if the
    frontend is offline, switched away, or the SSE stream closed prematurely.

    Respects the per-conversation autoTranslate setting (frozen at send-time by
    the frontend — won't be overwritten while a task is active).

    ``db`` may be omitted; callers that don't hold a connection (e.g. the
    endpoint module, which is DB-decoupled) pass nothing and this acquires
    the thread-local chat connection itself.
    """
    pfx = '[AutoTranslate]'
    if db is None:
        db = get_thread_db(DOMAIN_CHAT)
    try:
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return

        # ── Check autoTranslate setting (default true, matching frontend behavior) ──
        settings = json.loads(row[1] or '{}') if row[1] else {}
        auto_translate = settings.get('autoTranslate', True)
        if not auto_translate:
            logger.info('%s conv=%s msg=%d autoTranslate=false in settings — '
                        'skipping (settings.autoTranslate=%r)',
                        pfx, conv_id[:8], msg_idx,
                        settings.get('autoTranslate'))
            return

        # Check if translation already exists (frontend may have triggered it first)
        messages = json.loads(row[0] or '[]')
        if msg_idx < len(messages):
            existing_tc = messages[msg_idx].get('translatedContent')
            if existing_tc and len(existing_tc) > 0:
                # ★ FIX: detect stale partial translations — if the existing translation
                # is less than 15% of the content length, it was translated from partial
                # content (e.g. mid-stream) and needs re-translation with the full content.
                content_len = len(content)
                tc_len = len(existing_tc)
                if content_len > 0 and tc_len < content_len * 0.15:
                    logger.info('%s conv=%s msg=%d stale translatedContent detected: '
                                'tc=%d chars vs content=%d chars (%.1f%%) — re-translating',
                                pfx, conv_id[:8], msg_idx, tc_len, content_len,
                                tc_len / content_len * 100)
                    # Clear the stale translation so we re-translate
                    messages[msg_idx].pop('translatedContent', None)
                    messages[msg_idx].pop('_translateDone', None)
                    messages[msg_idx].pop('_translateTaskId', None)
                    messages[msg_idx].pop('_translatedCache', None)
                    # Persist the cleared state (with CAS to avoid clobbering
                    # concurrent frontend writes)
                    try:
                        _ua_row = db.execute(
                            'SELECT updated_at FROM conversations WHERE id=? AND user_id=1',
                            (conv_id,)
                        ).fetchone()
                        if _ua_row:
                            _now_ms = int(time.time() * 1000)
                            db_execute_with_retry(
                                db,
                                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1 AND updated_at=?',
                                (json_dumps_pg(messages), _now_ms, conv_id, _ua_row[0])
                            )
                    except Exception as ce:
                        logger.warning('%s conv=%s Failed to clear stale translation: %s',
                                       pfx, conv_id[:8], ce)
                else:
                    logger.debug('%s conv=%s msg=%d already has translatedContent (%d chars) — skipping',
                                 pfx, conv_id[:8], msg_idx, len(existing_tc))
                    return

        # ── Skip already-Chinese content ──
        # The target language is hard-pinned to Chinese (see _run_translate
        # below). When the assistant already replied in Chinese (e.g. a Qwen/
        # Kimi model with a "default Chinese" system prompt), translating it to
        # Chinese is a no-op the engine's echo detector misreads as "model
        # echoed input" — it burns the full retry budget then FAILS the
        # translation outright. Short-circuit here, mirroring the frontend
        # _isAlreadyChinese guard (lib.text_lang.is_predominantly_chinese).
        from lib.text_lang import is_predominantly_chinese
        if is_predominantly_chinese(content):
            logger.info('%s conv=%s msg=%d content already predominantly Chinese '
                        '(target=Chinese) — skipping auto-translate (no-op)',
                        pfx, conv_id[:8], msg_idx)
            return

        logger.debug('%s conv=%s msg=%d autoTranslate is ON — starting translation',
                     pfx, conv_id[:8], msg_idx)

        # ── Check for already-running translate task from the frontend ──
        # Import lazily to avoid circular imports
        from lib.translate import _translate_tasks, _translate_tasks_lock
        with _translate_tasks_lock:
            for tid, tt in _translate_tasks.items():
                if (tt.get('convId') == conv_id and
                    tt.get('msgIdx') == msg_idx and
                    tt.get('field') == 'translatedContent' and
                    tt['status'] == 'running'):
                    logger.info('%s conv=%s msg=%d Frontend already started translate task %s — skipping',
                                pfx, conv_id[:8], msg_idx, tid)
                    return

        # ── Resolve the stable per-message id so the live push frame can be
        #    routed by id (the preferred path) instead of the fragile msgIdx
        #    fallback.  Without this the frontend's '*' translate subscriber
        #    can only match by index, which drifts for multi-turn agent /
        #    endpoint conversations and post-stream reconciliation — the
        #    surgical _renderMsgInPlace then targets the wrong (or no) DOM
        #    node and the translation never appears until a full re-render. ──
        _msg_id = ''
        if msg_idx is not None and 0 <= msg_idx < len(messages):
            _msg_id = messages[msg_idx].get('_msgId') or ''

        # ── Incremental per-round translation hand-off ──
        # When the task translated each round's prose segment as it closed,
        # the per-task worker already has the segments cached. Let it assemble
        # + commit the final translatedContent (no big end-of-task LLM call).
        # Only takes over when an accumulator is active for this task; else we
        # fall through to the whole-message thread below.
        if task is not None:
            try:
                from lib.translate import finalize_incremental
                if finalize_incremental(task, conv_id, msg_idx, content, msg_id=_msg_id or None):
                    logger.info('%s conv=%s msg=%d Incremental translator owns this '
                                'translation — skipping whole-message thread',
                                pfx, conv_id[:8], msg_idx)
                    return
            except Exception as ie:
                logger.warning('%s conv=%s Incremental finalize failed, falling back '
                               'to whole-message translate: %s', pfx, conv_id[:8], ie)

        # ── Start background translation thread ──
        logger.info('%s conv=%s msg=%d Starting server-side auto-translation (%d chars)',
                    pfx, conv_id[:8], msg_idx, len(content))

        def _run_translate():
            try:
                from lib.translate import _do_translate, _translate_tasks, _translate_tasks_lock
                task_id = str(uuid.uuid4())[:12]
                task = {
                    'id': task_id,
                    'status': 'running',
                    'result': None,
                    'error': None,
                    'model': None,
                    'progress': None,
                    'convId': conv_id,
                    'msgIdx': msg_idx,
                    'msgId': _msg_id,
                    'field': 'translatedContent',
                    'targetLang': 'Chinese',
                    'textLen': len(content),
                    'created_at': time.time(),
                    'completed_at': None,
                }
                with _translate_tasks_lock:
                    _translate_tasks[task_id] = task
                logger.info('%s task=%s conv=%s Translate thread started', pfx, task_id, conv_id[:8])
                _do_translate(task_id, content, 'Chinese', 'English', conv_id, msg_idx, 'translatedContent',
                              msg_id=_msg_id or None)
            except Exception as e:
                logger.error('%s conv=%s Translate thread failed: %s', pfx, conv_id[:8], e, exc_info=True)

        threading.Thread(target=_run_translate, daemon=True,
                         name=f'auto-translate-{conv_id[:8]}').start()

    except Exception as e:
        logger.warning('%s conv=%s Failed to check/start auto-translate: %s',
                       pfx, conv_id[:8], e)


def _maybe_auto_translate_critic(conv_id, content, msg_idx, db=None):
    """Server-side auto-translate for endpoint-mode critic review messages.

    Endpoint-mode critic output is authored by the Critic LLM (English by
    default, sometimes mixed) and is stored as ``role='user'`` with
    ``_isEndpointReview=true`` in the conversation's ``messages`` list.  The
    existing ``_maybe_auto_translate_assistant`` safety-net commits to
    ``messages[msg_idx]`` by index regardless of role, so we reuse it
    directly and only override the log prefix + source-lang hint for
    observability.

    This path is only invoked from
    ``endpoint._trigger_endpoint_auto_translate``.  The per-conv
    ``autoTranslate`` gate, dedup against running frontend translate tasks,
    and stale-partial re-translation logic are inherited verbatim.
    """
    pfx = '[AutoTranslate:Critic]'
    if not conv_id or not content:
        logger.debug('%s conv=%s msg=%s — empty conv/content; skipping',
                     pfx, conv_id[:8] if conv_id else '?', msg_idx)
        return
    # Delegate to the shared helper — it is role-agnostic at the commit
    # layer (writes to messages[msg_idx]).  We only log the role flavour
    # here so operators can distinguish critic translations in the log.
    logger.info('%s conv=%s msg=%d content=%dchars — delegating to '
                '_maybe_auto_translate_assistant safety net',
                pfx, conv_id[:8], msg_idx, len(content))
    _maybe_auto_translate_assistant(conv_id, content, msg_idx, db)


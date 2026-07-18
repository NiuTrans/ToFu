"""The core server-side auto-translate safety net for assistant replies.

Home of ``_maybe_auto_translate_assistant`` — the index-based, post-persist
translation trigger. It honours the per-conversation ``autoTranslate`` setting,
dedups against an already-running frontend translate task via the in-flight
guard, detects + re-does stale partial translations, short-circuits already-
Chinese content, and hands off to the incremental per-round translator when
one is active.

Dependencies are one-directional: DB helpers from ``lib.database``, the
translate engine lazily from ``lib.translate`` (facade path
``lib.translate.runtime._do_translate`` stays valid) and ``lib.text_lang`` —
never ``manager``.
"""

import json
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
from lib.log import get_logger

logger = get_logger(__name__)


def _spawn_narration_backfill(conv_id, msg_idx, msg_id, target_lang, messages):
    """Fire-and-forget the path-independent terminal narration backfill.

    Guards on a CHEAP pure predicate first (no LLM) — only spawns the daemon
    thread when the target message actually has a narration text segment whose
    ``translatedText`` is still empty. The backfill itself
    (``backfill_message_narration_sync``) is enrich-only + idempotent, so a
    spurious spawn would be a no-op anyway; the pre-check just avoids the thread
    churn on the common already-stamped case.

    Runs off-thread because it makes REAL LLM calls (one per still-missing
    narration segment) and must never block the sync / run_task caller.
    """
    try:
        from lib.translate.segment_backfill import has_untranslated_narration
        msg = None
        if isinstance(msg_idx, int) and 0 <= msg_idx < len(messages):
            msg = messages[msg_idx]
        if not (isinstance(msg, dict) and has_untranslated_narration(msg)):
            return

        def _run():
            try:
                from lib.translate.segment_backfill import backfill_message_narration_sync
                backfill_message_narration_sync(
                    conv_id, msg_idx, msg_id or '', target_lang, source='English',
                    log_tag=(conv_id or '?')[:8])
            except Exception as e:
                logger.warning('[AutoTranslate] conv=%s narration backfill thread '
                               'failed: %s', (conv_id or '?')[:8], e)

        threading.Thread(target=_run, daemon=True,
                         name=f'narration-backfill-{(conv_id or "x")[:8]}').start()
    except Exception as e:
        logger.debug('[AutoTranslate] conv=%s spawn narration backfill skipped: %s',
                     (conv_id or '?')[:8], e)


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
    # When an incremental accumulator is active for this task, it owns a
    # background worker thread that holds the pre-translated per-round
    # segments. Exactly ONE of two things must happen to that worker, or it
    # leaks (sits idle 300s then logs a misleading "finalize never called"
    # warning AND silently discards its segments — the reported "I have to
    # click Translate every time" bug): either finalize_incremental() takes
    # ownership (commits the assembled translation), or cancel_incremental()
    # tears it down. Every early-return below is a skip path that does NEITHER
    # unless we guarantee a cancel in the finally. ``_inc_handed_off`` flips
    # True only when finalize takes over, suppressing the cancel.
    _inc_handed_off = False
    # The in-flight guard is claimed (by stable msgId) just before we schedule
    # any work, and OWNED by whichever async path actually runs — so we must
    # NOT release it here when that path took over. ``_guard_owned_by_worker``
    # flips True the moment we hand the key to the spawned thread / incremental
    # finalize; the finally then leaves the release to that worker.
    _guard_owned_by_worker = False
    _guard_key_msg_id = ''
    _guard_key_idx = None
    try:
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return

        # ── Check autoTranslate setting (canonical default-OFF resolver) ──
        # Historically this defaulted TRUE while the input/config paths
        # defaulted FALSE — the three-way split that made auto-translate fire
        # unpredictably. Now every trigger path resolves through the single
        # lib.conv_config.resolve_auto_translate (default OFF).
        settings = json.loads(row[1] or '{}') if row[1] else {}
        from lib.conv_config import resolve_auto_translate, resolve_translate_target, target_lang_code
        auto_translate = resolve_auto_translate(settings)
        # ── OUTPUT-side target language (model reply → human) ──
        # Resolve from the UI language the frontend froze into settings.uiLang
        # (config wins over settings when the task carries a fresher one).
        # Falls back to Chinese when unresolved, so a headless / old-frontend
        # conv is byte-identical to the historical hard-pin.
        _task_cfg = (task or {}).get('config') or {}
        target_lang = resolve_translate_target(_task_cfg, settings)
        target_code = target_lang_code(target_lang)
        if not auto_translate:
            logger.info('%s conv=%s msg=%d autoTranslate=false (resolved) — '
                        'skipping (settings.autoTranslate=%r)',
                        pfx, conv_id[:8], msg_idx,
                        settings.get('autoTranslate'))
            return

        messages = json.loads(row[0] or '[]')

        # ── Resolve the target message by STABLE ID first (Phase 2) ──
        # The caller passes a positional ``msg_idx`` (e.g. len(messages)-1 from
        # the single-turn path, or a fresh-enumeration index from the endpoint
        # path). A concurrent frontend write can shift the row between the
        # caller's read and ours, so a position is NOT a safe anchor for the
        # stale-check / dedup / spawn below. Prefer the task-time assistant id
        # (the one the live preview used) → the persisted message's id at the
        # passed index, then LOOK THE MESSAGE UP BY THAT ID and use the
        # id-resolved index for everything downstream. Only fall back to the
        # raw positional index when no id is recoverable.
        _msg_id = (task or {}).get('_assistantMsgId') or ''
        if not _msg_id and isinstance(msg_idx, int) and 0 <= msg_idx < len(messages):
            _msg_id = (messages[msg_idx] or {}).get('_msgId') or ''
        _resolved_idx = None
        if _msg_id:
            for _i, _m in enumerate(messages):
                if isinstance(_m, dict) and _m.get('_msgId') == _msg_id:
                    _resolved_idx = _i
                    break
            if _resolved_idx is not None and _resolved_idx != msg_idx:
                logger.info('%s conv=%s id-anchored target: msgId=%s at idx=%d '
                            '(caller passed idx=%s) — following id',
                            pfx, conv_id[:8], _msg_id[:8], _resolved_idx, msg_idx)
        # The authoritative index for all index-based reads below.
        eff_idx = _resolved_idx if _resolved_idx is not None else msg_idx
        _guard_key_msg_id = _msg_id
        _guard_key_idx = eff_idx

        # Check if translation already exists (frontend may have triggered it first)
        if isinstance(eff_idx, int) and 0 <= eff_idx < len(messages):
            existing_tc = messages[eff_idx].get('translatedContent')
            if existing_tc and len(existing_tc) > 0:
                # ★ FIX: detect stale partial translations — if the existing translation
                # is less than 15% of the content length, it was translated from partial
                # content (e.g. mid-stream) and needs re-translation with the full content.
                content_len = len(content)
                tc_len = len(existing_tc)
                if content_len > 0 and tc_len < content_len * 0.15:
                    logger.info('%s conv=%s msg=%d stale translatedContent detected: '
                                'tc=%d chars vs content=%d chars (%.1f%%) — re-translating',
                                pfx, conv_id[:8], eff_idx, tc_len, content_len,
                                tc_len / content_len * 100)
                    # Clear the stale translation so we re-translate
                    messages[eff_idx].pop('translatedContent', None)
                    messages[eff_idx].pop('_translateDone', None)
                    messages[eff_idx].pop('_translateTaskId', None)
                    messages[eff_idx].pop('_translatedCache', None)
                    # Persist the cleared state (with CAS to avoid clobbering
                    # concurrent frontend writes)
                    try:
                        _ua_row = db.execute(
                            'SELECT rev FROM conversations WHERE id=? AND user_id=1',
                            (conv_id,)
                        ).fetchone()
                        if _ua_row:
                            _now_ms = int(time.time() * 1000)
                            # Phase 4 W4: CAS on rev (best-effort single-shot; a
                            # miss just skips the stale-clear, as before).
                            db_execute_with_retry(
                                db,
                                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1 AND rev=?',
                                (json_dumps_pg(messages), _now_ms, conv_id, _ua_row[0])
                            )
                    except Exception as ce:
                        logger.warning('%s conv=%s Failed to clear stale translation: %s',
                                       pfx, conv_id[:8], ce)
                else:
                    logger.debug('%s conv=%s msg=%d already has translatedContent (%d chars) — skipping',
                                 pfx, conv_id[:8], msg_idx, len(existing_tc))
                    # ★ PATH-INDEPENDENT narration backfill (Path B fix): the
                    #   DELIVERABLE is already translated, but that must NOT be
                    #   a reason to leave the interleaved tool narration in
                    #   English. Enrich any narration segment still missing
                    #   translatedText off-thread (enrich-only + idempotent →
                    #   a no-op / zero LLM calls when already stamped).
                    _spawn_narration_backfill(conv_id, eff_idx, _msg_id,
                                              target_lang, messages)
                    return

        # ── Skip content already in the target language ──
        # When the assistant already replied in the target language (e.g. a
        # Qwen/Kimi model that defaults to Chinese, or a genuinely-Japanese
        # reply into a Japanese UI), re-translating is a no-op the engine's
        # echo detector misreads as "model echoed input" — it burns the full
        # retry budget then FAILS outright. Detect with force_fasttext=True:
        # the script+heuristic tier CANNOT tell kanji-heavy Japanese from
        # Chinese (both use the CJK-ideograph block), so a plain
        # is_predominantly_chinese gate wrongly skipped Japanese as "already
        # Chinese" and never translated it — the reported bug. The statistical
        # model separates ja/zh. Accepted corner case: pure-kanji, no-kana
        # Japanese still resolves 'zh' (linguistically indistinguishable from
        # Chinese at the character level).
        from lib.text_lang import detect_language
        if detect_language(content, force_fasttext=True).code == target_code:
            logger.info('%s conv=%s msg=%d content already in target language '
                        '(target=%s/%s) — skipping auto-translate (no-op)',
                        pfx, conv_id[:8], msg_idx, target_lang, target_code)
            # ★ PATH-INDEPENDENT narration backfill (Path B fix): the
            #   DELIVERABLE is already in the target language (so it has no
            #   translatedContent), but the interleaved English narration still
            #   needs translating. Enrich off-thread (enrich-only + idempotent).
            _spawn_narration_backfill(conv_id, eff_idx, _msg_id,
                                      target_lang, messages)
            return

        logger.debug('%s conv=%s msg=%d autoTranslate is ON — starting translation',
                     pfx, conv_id[:8], msg_idx)

        # ── Pre-spawn dedup: claim the per-(conv,msgId) in-flight guard ──
        # (Phase 2) This is the AUTHORITATIVE double-fire guard. The per-turn
        # endpoint trigger, the end-of-task rescan, and a retried single-turn
        # safety net can all reach here for the SAME message; the first to
        # claim wins, the rest stand down. Keyed by the stable msgId (falls
        # back to the id-resolved index) so a concurrent insert can't defeat
        # it the way the old msgIdx-keyed _translate_tasks scan could. The
        # owning async path (incremental finalize / spawned thread) releases
        # the claim when it settles; every skip path releases in the finally.
        from lib.translate import claim_inflight
        if not claim_inflight(conv_id, _msg_id, eff_idx):
            logger.info('%s conv=%s msg=%s msgId=%s translation already in-flight — '
                        'standing down (pre-spawn dedup)',
                        pfx, conv_id[:8], eff_idx, (_msg_id or '-')[:8])
            return

        # ── Incremental per-round translation hand-off ──
        # When the task translated each round's prose segment as it closed,
        # the per-task worker already has the segments cached. Let it assemble
        # + commit the final translatedContent (no big end-of-task LLM call).
        # Only takes over when an accumulator is active for this task; else we
        # fall through to the whole-message thread below.
        if task is not None:
            try:
                from lib.translate import finalize_incremental
                if finalize_incremental(task, conv_id, eff_idx, content, msg_id=_msg_id or None, target=target_lang):
                    _inc_handed_off = True
                    # The incremental worker now owns BOTH the accumulator and
                    # the in-flight guard; it releases the guard when it commits
                    # (see incremental._do_finalize). Don't release in finally.
                    _guard_owned_by_worker = True
                    logger.info('%s conv=%s msg=%d Incremental translator owns this '
                                'translation — skipping whole-message thread',
                                pfx, conv_id[:8], eff_idx)
                    return
            except Exception as ie:
                logger.warning('%s conv=%s Incremental finalize failed, falling back '
                               'to whole-message translate: %s', pfx, conv_id[:8], ie)

        # ── Start background translation thread ──
        logger.info('%s conv=%s msg=%d Starting server-side auto-translation (%d chars)',
                    pfx, conv_id[:8], eff_idx, len(content))

        # The spawned thread now OWNS the in-flight guard (claimed above) and
        # releases it when the translate worker settles. Capture the key in
        # locals so the closure releases the SAME key regardless of later
        # mutations.
        _spawn_msg_id = _msg_id
        _spawn_idx = eff_idx
        _spawn_target = target_lang

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
                    'msgIdx': _spawn_idx,
                    'msgId': _spawn_msg_id,
                    'field': 'translatedContent',
                    'targetLang': target_lang,
                    'textLen': len(content),
                    'created_at': time.time(),
                    'completed_at': None,
                }
                with _translate_tasks_lock:
                    _translate_tasks[task_id] = task
                logger.info('%s task=%s conv=%s Translate thread started', pfx, task_id, conv_id[:8])
                _do_translate(task_id, content, _spawn_target, 'English', conv_id, _spawn_idx, 'translatedContent',
                              msg_id=_spawn_msg_id or None)
            except Exception as e:
                logger.error('%s conv=%s Translate thread failed: %s', pfx, conv_id[:8], e, exc_info=True)
            finally:
                # Release the in-flight guard so a legitimate later re-translate
                # (e.g. the message is edited) can claim it again.
                try:
                    from lib.translate import release_inflight
                    release_inflight(conv_id, _spawn_msg_id, _spawn_idx)
                except Exception as re:
                    logger.debug('%s conv=%s release_inflight (worker) failed: %s',
                                 pfx, conv_id[:8], re)

        # Mark the guard worker-owned ONLY after the thread is successfully
        # started — if Thread construction / start() raises, ownership never
        # transferred and the outer finally must release the claim (else the
        # message is wedged in-flight forever).
        threading.Thread(target=_run_translate, daemon=True,
                         name=f'auto-translate-{conv_id[:8]}').start()
        _guard_owned_by_worker = True

    except Exception as e:
        logger.warning('%s conv=%s Failed to check/start auto-translate: %s',
                       pfx, conv_id[:8], e)
    finally:
        # Tear down an incremental accumulator that nobody took ownership of.
        # finalize_incremental sets _inc_handed_off when it adopts the task;
        # every other exit (autoTranslate off, already-translated, already-
        # Chinese, frontend-task dedup, whole-message fallback that started a
        # fresh _do_translate thread, or any raised exception) leaves the
        # per-round worker dangling. cancel_incremental is a cheap no-op when
        # no accumulator exists (the common autoTranslate-off case).
        if task is not None and not _inc_handed_off:
            try:
                from lib.translate import cancel_incremental
                if cancel_incremental(task):
                    logger.info('%s conv=%s msg=%s cancelled orphaned incremental '
                                'accumulator (no finalize handoff)',
                                pfx, conv_id[:8] if conv_id else '?', msg_idx)
            except Exception as ce:
                logger.debug('%s conv=%s cancel_incremental failed: %s',
                             pfx, conv_id[:8] if conv_id else '?', ce)
        # Release the in-flight guard UNLESS an async worker took ownership of
        # it (incremental finalize / the spawned translate thread, both of
        # which release it themselves when they settle). Every skip / dedup /
        # already-translated / already-Chinese / error exit lands here and
        # must release so it can't wedge a future legitimate re-translate.
        if not _guard_owned_by_worker:
            try:
                from lib.translate import release_inflight
                release_inflight(conv_id, _guard_key_msg_id, _guard_key_idx)
            except Exception as re:
                logger.debug('%s conv=%s release_inflight (skip path) failed: %s',
                             pfx, conv_id[:8] if conv_id else '?', re)

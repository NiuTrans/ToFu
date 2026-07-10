"""Async translation TaskRuntime + worker.

The TaskRuntime owns the task registry, locking, push channel ('translate'),
TTL cleanup, and ``audit_log``-style error reporting. ``_do_translate`` is
the actual worker thread invoked via ``_translate_runtime.spawn(...)``.

Compatibility shims ``_translate_tasks`` / ``_translate_tasks_lock`` exist
because callers in lib.tasks_pkg.manager and tests import them by name.
New code should use the runtime directly.
"""

import json
import time

from lib.database import DOMAIN_CHAT
from lib.log import get_logger
from lib.task_runtime import TaskRuntime
from lib.text_lang import is_predominantly_chinese

from .commit import _commit_translation_to_db
from .constants import DEFAULT_USER_ID
from .engine import _translate_freetext
from .notranslate import _extract_notranslate_blocks, _reattach_notranslate_blocks
from .prompt import _build_translate_prompt, _strip_notranslate_tags
from .status import _format_status_message

logger = get_logger(__name__)


# ── Async translation tasks (survive page reload / tab switch) ──
_translate_runtime = TaskRuntime(
    'translate', ttl=1800,
    push_channel='translate',
    error_source='routes.translate',
)

# Compatibility shims for legacy code paths:
#   _translate_tasks      → registry-as-dict (read-only access for ID lookups)
#   _translate_tasks_lock → kept as a per-task multi-write lock (use task['events_lock']
#                            for new code; this name exists only for diff minimisation)
_translate_tasks_lock = _translate_runtime._lock      # type: ignore[attr-defined]
_translate_tasks = _translate_runtime._tasks          # type: ignore[attr-defined]


def _cleanup_translate_tasks():
    """Remove expired translation tasks (delegates to TaskRuntime)."""
    n = _translate_runtime.cleanup_stale()
    if n:
        logger.debug('[Translate] Cleaned up %d expired tasks', n)


def _read_message_segments(conv_id, msg_id, msg_idx):
    """Read the target assistant message's ``segments`` list from the DB.

    Resolves the message by stable id first (robust against concurrent
    inserts), then by position. Returns the segments list or ``None`` when the
    conversation / message / segments are absent (a pre-v36 row → the caller
    treats it as a no-op). Never raises — best-effort enrichment only.
    """
    try:
        from lib.database import get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()
        if not row:
            return None
        messages = json.loads(row['messages'] or '[]')
    except Exception as e:
        logger.warning('[Translate] segment read failed for conv=%s: %s',
                       (conv_id or '?')[:8], e)
        return None

    msg = None
    if msg_id:
        for candidate in messages:
            if isinstance(candidate, dict) and candidate.get('_msgId') == msg_id:
                msg = candidate
                break
    if msg is None and msg_idx is not None:
        try:
            idx = int(msg_idx)
        except (ValueError, TypeError) as e:
            logger.debug('[Translate] segment read: bad msg_idx %r: %s', msg_idx, e)
            idx = -1
        if 0 <= idx < len(messages):
            msg = messages[idx]
    if not isinstance(msg, dict):
        return None
    segs = msg.get('segments')
    return segs if isinstance(segs, list) and segs else None


def _build_segment_translation_map(conv_id, msg_id, msg_idx, system_prompt,
                                   source, target):
    """Translate each non-deliverable narration segment of the target message.

    Returns ``{llmRound: 中文}`` so ``_commit_translation_to_db`` can stamp
    ``translatedText`` onto the matching segments — making the retro / on-open /
    manual / toggle path interleave the settled timeline exactly like the live
    incremental worker does. Symmetric with
    :meth:`lib.translate.incremental._Acc._do_finalize_inner`'s ``seg_trans``
    build: same per-segment notranslate extraction + already-Chinese skip.

    A no-op returning ``None`` when the message has no segments (pre-v36 row).
    Per-segment failures are logged and skipped (the whole-message
    ``translatedContent`` commit is unaffected — this is pure enrichment).
    """
    segs = _read_message_segments(conv_id, msg_id, msg_idx)
    if not segs:
        return None
    seg_map = _translate_segments_to_map(segs, system_prompt, source, target,
                                         log_tag=(conv_id or '?')[:8])
    return seg_map or None


def _translate_segments_to_map(segs, system_prompt, source, target, *,
                               log_tag='?'):
    """Pure core: translate the non-deliverable narration segments → ``{llmRound: 中文}``.

    Shared by the live retro path (:func:`_build_segment_translation_map`, which
    reads ``segs`` from the DB first) and the one-shot backfill migration (which
    already holds ``segs``). Kept as a SINGLE source of truth so the two paths
    never diverge on which segments are translatable or how notranslate blocks /
    already-Chinese text are handled.

    ENRICH-ONLY: a segment that already carries a non-empty ``translatedText`` is
    skipped (not re-translated) — the map only contains rounds that gained a
    translation, so stamping is idempotent and cheap on re-run. ``tool_use`` and
    the deliverable/terminal ``text`` segment are excluded (the deliverable is
    rendered via ``translatedContent``). Per-segment failures are logged and
    skipped; returns ``{}`` when nothing was translatable.
    """
    seg_map = {}
    for seg in (segs or []):
        if not isinstance(seg, dict):
            continue
        if seg.get('type') != 'text' or seg.get('deliverable'):
            continue
        if (seg.get('translatedText') or '').strip():
            continue  # enrich-only: never re-translate / overwrite
        lr = seg.get('llmRound')
        if lr is None:
            continue
        original = (seg.get('text') or '').strip()
        if not original:
            continue
        try:
            if is_predominantly_chinese(original):
                seg_map[lr] = original
                continue
            body, nt_blocks = _extract_notranslate_blocks(original)
            if not body.strip():
                seg_map[lr] = original
                continue
            translated, _usage = _translate_freetext(
                body, system_prompt, source=source, target=target)
            translated = (translated or '').strip()
            if nt_blocks:
                translated = _reattach_notranslate_blocks(translated, nt_blocks)
            if translated:
                seg_map[lr] = translated
        except Exception as e:
            logger.warning('[Translate] segment round=%s translate failed for '
                           '%s: %s', lr, log_tag, e)
    if seg_map:
        logger.info('[Translate] built segment translation map for %s: '
                    '%d/%d narration segments', log_tag, len(seg_map), len(segs))
    return seg_map


def _do_translate(task_id, text, target, source, conv_id, msg_idx, field, *, msg_id=None):
    """Background thread: run translation and store result.

    msg_id (optional): stable per-message UUID. When supplied, the commit
    step looks the message up by id first and only falls back to msg_idx
    when the id no longer exists in the conversation. This is what makes
    translate robust against concurrent inserts (the
    "msg_idx N out of range" warning class).
    """
    with _translate_tasks_lock:
        task = _translate_tasks.get(task_id)
    if not task:
        return

    system_prompt = _build_translate_prompt(target, source)
    original_text = text
    input_len = len(text)

    def _push_running(*, partial=None, status_message=None, status_kind=None):
        """Emit a 'running' push frame so a client VIEWING this conversation
        renders the live translating indicator / streaming preview without
        having to switch away and back.

        Only fires when conv_id is known (auto-translate + manual click both
        carry it).  The frontend '*' subscriber ignores running frames for
        client-initiated tasks (its own poll loop is authoritative), so this
        is a no-op for the manual-click path and only drives the otherwise
        signal-less server-side auto-translate path.
        """
        if not conv_id:
            return
        try:
            from lib.push import push_event
            frame = {
                'type': 'running', 'status': 'running',
                'convId': conv_id, 'msgIdx': msg_idx,
                'msgId': msg_id or '', 'field': field,
            }
            if partial is not None:
                frame['partial'] = partial
            if status_message:
                frame['statusMessage'] = status_message
                frame['statusKind'] = status_kind or ''
            push_event('translate', task_id, frame)
        except Exception as e:
            logger.debug('[Translate] push running frame failed task=%s: %s',
                         task_id[:8], e)

    def _on_status(event):
        """Record the latest retry/status event onto the task dict."""
        msg = _format_status_message(event)
        with _translate_tasks_lock:
            t = _translate_tasks.get(task_id)
            if t:
                t['statusMessage'] = msg
                t['statusKind'] = event.get('kind', '')
                t['statusUpdatedAt'] = time.time()
        _push_running(status_message=msg, status_kind=event.get('kind', ''))

    # ── Streaming preview throttling ──
    # _translate_one_chunk fires progress_cb for every SSE delta (often
    # 1-3 chars at a time).  Server-side auto-translate drives the ACTIVE
    # view through the push channel, where each _push_running frame is
    # delivered in real time — so this throttle is the direct cap on how
    # fluid the live streaming preview looks.  250ms (4fps) made fast small
    # models look choppy/laggy; 100ms (10fps) streams smoothly while still
    # coalescing micro-deltas so large docs don't flood the socket.
    _last_partial_ts = [0.0]

    def _on_progress(text_so_far):
        now = time.time()
        if now - _last_partial_ts[0] < 0.10:
            return
        _last_partial_ts[0] = now
        with _translate_tasks_lock:
            t = _translate_tasks.get(task_id)
            if t and t.get('status') == 'running':
                t['partial'] = text_so_far
                t['partialUpdatedAt'] = now
        _push_running(partial=text_so_far)

    # Surface the indicator the instant the worker picks the task up, even
    # before the first SSE delta arrives — the active view otherwise shows
    # the bare finished English message with no hint translation is coming.
    _push_running(status_message='', status_kind='started')

    try:
        text, nt_blocks = _extract_notranslate_blocks(text)
        if nt_blocks:
            logger.info('[Translate] Task %s: extracted %d notranslate blocks',
                        task_id[:8], len(nt_blocks))
            if not text.strip():
                content = _strip_notranslate_tags(original_text)
                with _translate_tasks_lock:
                    task['status'] = 'done'
                    task['result'] = content
                    task['model'] = 'skipped'
                    task['completed_at'] = time.time()
                if conv_id and (msg_idx is not None or msg_id):
                    seg_trans = None
                    if field == 'translatedContent':
                        try:
                            seg_trans = _build_segment_translation_map(
                                conv_id, msg_id, msg_idx, system_prompt, source, target)
                        except Exception as se:
                            logger.warning('[Translate] segment map build failed for task %s: %s',
                                           task_id[:8], se, exc_info=True)
                    try:
                        _commit_translation_to_db(conv_id, msg_idx, field, content,
                                                  original_text=original_text,
                                                  model='skipped', msg_id=msg_id,
                                                  segment_translations=seg_trans)
                    except Exception as ce:
                        logger.warning('[Translate] Auto-commit failed for task %s: %s',
                                       task_id[:8], ce, exc_info=True)
                try:
                    from lib.push import push_event
                    push_event('translate', task_id, {
                        'type': 'done', 'status': 'done',
                        'translated': content, 'model': 'skipped',
                        'convId': conv_id or '', 'msgIdx': msg_idx,
                        'msgId': msg_id or '', 'field': field,
                    })
                except Exception as e:
                    logger.debug('[Translate] push_event skip-done failed task=%s: %s',
                                 task_id[:8], e)
                return

        content, _usage = _translate_freetext(text, system_prompt,
                                              source=source, target=target,
                                              status_cb=_on_status,
                                              progress_cb=_on_progress)
        _model = 'unknown'
        _trace = {}
        if isinstance(_usage, dict):
            _disp = _usage.get('_dispatch', {})
            _model = _disp.get('model', _usage.get('model', 'unknown'))
            _trace = _usage.get('_translate_trace', {}) or {}
        content = content.strip()

        if nt_blocks:
            content = _reattach_notranslate_blocks(content, nt_blocks)

        # 溯源: loudly flag a committed translation that the engine judged
        # incomplete (truncated/suspicious) — the dominant 漏译 signature.
        _verdict = _trace.get('verdict', 'ok')
        if _verdict != 'ok' or _trace.get('suspicious'):
            logger.warning('[Translate] Task %s committing INCOMPLETE translation: '
                           'verdict=%s suspicious=%s %d→%d chars (ratio=%.2f) '
                           'model=%s conv=%s msg=%s — original may be partially untranslated',
                           task_id[:8], _verdict, _trace.get('suspicious', False),
                           input_len, len(content),
                           (len(content) / input_len) if input_len else 0.0,
                           _model, conv_id[:8] if conv_id else '?', msg_idx)

        with _translate_tasks_lock:
            task['status'] = 'done'
            task['result'] = content
            task['model'] = _model
            task['completed_at'] = time.time()
            # Clear transient status so a late-poll doesn't show the last retry message
            task.pop('statusMessage', None)
            task.pop('statusKind', None)
            # Clear streaming preview — the final result supersedes it.
            task.pop('partial', None)
            task.pop('partialUpdatedAt', None)
        logger.info('[Translate] Task %s done: %d→%d chars, model=%s, target=%s, conv=%s msg=%s',
                    task_id[:8], input_len, len(content), _model, target,
                    conv_id[:8] if conv_id else '?', msg_idx)
        try:
            from lib.push import push_event
            push_event('translate', task_id, {
                'type': 'done', 'status': 'done',
                'translated': content, 'model': _model,
                'convId': conv_id or '', 'msgIdx': msg_idx,
                'msgId': msg_id or '', 'field': field,
            })
        except Exception as e:
            logger.debug('[Translate] push_event done failed task=%s: %s',
                         task_id[:8], e)

        if conv_id and (msg_idx is not None or msg_id):
            # ★ Per-round carry to the SETTLED render (whole-message / retro /
            #   on-open / manual / toggle path). The incremental live worker
            #   stamps translatedText onto each narration segment as its round
            #   closes; this path never had those cached segments, so it must
            #   BUILD the {llmRound: 中文} map itself by translating each
            #   non-deliverable text segment of the target message. Symmetric
            #   with incremental._Acc._do_finalize_inner. No-op (None) for a
            #   message without segments — the deliverable translatedContent
            #   commit below is unaffected either way.
            seg_trans = None
            if field == 'translatedContent':
                try:
                    seg_trans = _build_segment_translation_map(
                        conv_id, msg_id, msg_idx, system_prompt, source, target)
                except Exception as se:
                    logger.warning('[Translate] segment map build failed for task %s: %s',
                                   task_id[:8], se, exc_info=True)
            try:
                _commit_translation_to_db(conv_id, msg_idx, field, content,
                                         original_text=original_text, model=_model,
                                         msg_id=msg_id,
                                         segment_translations=seg_trans)
            except Exception as ce:
                logger.warning('[Translate] Auto-commit failed for task %s: %s', task_id[:8], ce, exc_info=True)

    except Exception as e:
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model=task.get('model', '') or '',
            context='translate', source='routes.translate',
        )
        with _translate_tasks_lock:
            task['status'] = 'error'
            task['error'] = envelope
            task['completed_at'] = time.time()
        logger.error('[Translate] Task %s failed: %s', task_id[:8], e, exc_info=True)
        try:
            from lib.push import push_event
            push_event('translate', task_id, {
                'type': 'error', 'status': 'error',
                'error': str(e)[:300],
                'convId': conv_id or '', 'msgIdx': msg_idx,
                'msgId': msg_id or '', 'field': field,
            })
        except Exception as pe:
            logger.debug('[Translate] push_event error failed task=%s: %s',
                         task_id[:8], pe)

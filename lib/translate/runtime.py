"""Async translation TaskRuntime + worker.

The TaskRuntime owns the task registry, locking, push channel ('translate'),
TTL cleanup, and ``audit_log``-style error reporting. ``_do_translate`` is
the actual worker thread invoked via ``_translate_runtime.spawn(...)``.

Compatibility shims ``_translate_tasks`` / ``_translate_tasks_lock`` exist
because callers in lib.tasks_pkg.manager and tests import them by name.
New code should use the runtime directly.
"""

import time

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

from .commit import _commit_translation_to_db
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
                    try:
                        _commit_translation_to_db(conv_id, msg_idx, field, content,
                                                  original_text=original_text,
                                                  model='skipped', msg_id=msg_id)
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
            try:
                _commit_translation_to_db(conv_id, msg_idx, field, content,
                                         original_text=original_text, model=_model,
                                         msg_id=msg_id)
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

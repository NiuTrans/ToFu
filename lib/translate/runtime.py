"""Async translation TaskRuntime + worker.

The TaskRuntime owns the task registry, locking, push channel ('translate'),
TTL cleanup, and ``audit_log``-style error reporting. ``_do_translate`` is
the actual worker thread invoked via ``_translate_runtime.spawn(...)``.

Compatibility shims ``_translate_tasks`` / ``_translate_tasks_lock`` exist
because callers in lib.tasks_pkg.manager and tests import them by name.
New code should use the runtime directly.
"""

import threading
import time

from lib.log import audit_log, get_logger
from lib.task_runtime import TaskRuntime

from .chunking import _split_text_for_translation
from .commit import _commit_translation_to_db
from .constants import _CHUNK_MAX_WORKERS, _CHUNK_THRESHOLD
from .engine import _translate_one_chunk
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


# Audit log on import so any change to chunked-translate parallelism is
# captured in the audit trail (matches the original module's behavior).
audit_log('config_change',
          param='translate_chunk_max_workers',
          old=4, new=_CHUNK_MAX_WORKERS,
          approved_by='user',
          rationale='speed up agent translation by raising chunked-translate parallelism')


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

    def _on_status(event):
        """Record the latest retry/status event onto the task dict."""
        msg = _format_status_message(event)
        with _translate_tasks_lock:
            t = _translate_tasks.get(task_id)
            if t:
                t['statusMessage'] = msg
                t['statusKind'] = event.get('kind', '')
                t['statusUpdatedAt'] = time.time()

    # ── Streaming preview throttling ──
    # _translate_one_chunk fires progress_cb for every SSE delta (often
    # 1-3 chars at a time).  Updating the task dict + serving polls for
    # every micro-delta is wasteful — the frontend polls at 2-4s anyway.
    # Throttle to one task-dict write per 250ms.
    _last_partial_ts = [0.0]

    def _on_progress(text_so_far):
        now = time.time()
        if now - _last_partial_ts[0] < 0.25:
            return
        _last_partial_ts[0] = now
        with _translate_tasks_lock:
            t = _translate_tasks.get(task_id)
            if t and t.get('status') == 'running':
                t['partial'] = text_so_far
                t['partialUpdatedAt'] = now

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

        if input_len > _CHUNK_THRESHOLD:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            chunks = _split_text_for_translation(text, max_chunk=8000)
            n_chunks = len(chunks)
            logger.info('[Translate] Task %s: splitting %d chars into %d chunks (parallel)',
                        task_id[:8], input_len, n_chunks)
            translated_chunks = [None] * n_chunks
            _model = 'unknown'
            _done_count = [0]
            _done_lock = threading.Lock()

            def _translate_indexed(idx, chunk):
                label = f':chunk{idx+1}/{n_chunks}'
                c, u = _translate_one_chunk(chunk, system_prompt, label,
                                            source=source, target=target,
                                            status_cb=_on_status)
                return idx, c, u

            max_workers = min(n_chunks, _CHUNK_MAX_WORKERS)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_translate_indexed, i, ch): i for i, ch in enumerate(chunks)}
                for future in as_completed(futures):
                    idx, c, u = future.result()
                    translated_chunks[idx] = c
                    if isinstance(u, dict):
                        _disp = u.get('_dispatch', {})
                        _model = _disp.get('model', u.get('model', _model))
                    with _done_lock:
                        _done_count[0] += 1
                    with _translate_tasks_lock:
                        task['progress'] = f'{_done_count[0]}/{n_chunks}'
            content = '\n\n'.join(translated_chunks)
        else:
            content, _usage = _translate_one_chunk(text, system_prompt,
                                                   source=source, target=target,
                                                   status_cb=_on_status,
                                                   progress_cb=_on_progress)
            _model = 'unknown'
            if isinstance(_usage, dict):
                _disp = _usage.get('_dispatch', {})
                _model = _disp.get('model', _usage.get('model', 'unknown'))
            content = content.strip()

        if nt_blocks:
            content = _reattach_notranslate_blocks(content, nt_blocks)

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

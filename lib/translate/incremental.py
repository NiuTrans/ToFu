"""Incremental per-round translation of agent assistant replies.

Problem
-------
The server-side auto-translate safety net
(``lib.tasks_pkg.manager._maybe_auto_translate_assistant``) waits until the
whole task has finished and then translates the entire assistant reply in one
LLM call. For a long agentic task with many tool rounds this means the user
stares at the untranslated reply (or a spinner) while one big translation
runs at the very end.

Approach
--------
The assistant's prose is produced incrementally: each tool round emits a
self-contained text segment (the model's commentary for that round) before it
calls its tools. We translate each segment AS SOON AS its round closes, in a
background worker, overlapping translation with the (slow) tool execution of
later rounds. By the time the task finishes the earlier segments are already
translated, so assembling the final ``translatedContent`` only needs the last
segment — no big end-of-task translation stall.

Lifecycle
---------
1. ``submit_round_segment(task, round_num, text)`` — called by the
   orchestrator at every round close. Lazily creates a per-task accumulator
   (gated on the per-conv ``autoTranslate`` setting + the
   ``TOFU_INCREMENTAL_TRANSLATE`` kill switch + a non-endpoint / non-autopilot
   task) and enqueues the segment to a single ordered worker thread.
2. The worker translates queued segments sequentially (preserving order),
   caching ``{round_num: translated}`` in memory. No DB write yet — the
   assistant message is still streaming and its index / ``_msgId`` aren't
   settled.
3. ``finalize_incremental(task, conv_id, msg_idx, content, msg_id)`` — called
   from the auto-translate decision point AFTER the result is persisted.
   Signals the worker to drain, assemble ``translatedContent`` from the cached
   segments (falling back to a single whole-content translation when the
   cached segments don't faithfully cover the final content), commit it
   race-safely via :func:`lib.translate.commit._commit_translation_to_db`, and
   push a ``done`` frame on the ``translate`` channel. Non-blocking: the worker
   thread does the final assemble / commit / push.

Ownership
---------
``finalize_incremental`` returns True when the incremental path has taken
ownership of the translation (the caller must NOT also run the whole-message
path), and False when it declined (no usable accumulator) so the caller falls
back to the whole-message translation.
"""

import os
import queue
import threading

from lib.log import get_logger
from lib.text_lang import is_predominantly_chinese

logger = get_logger(__name__)

# Kill switch: set TOFU_INCREMENTAL_TRANSLATE=0 to disable and fall back to the
# whole-message safety net everywhere.
_KILL_ENV = 'TOFU_INCREMENTAL_TRANSLATE'

# Worker self-destructs if no item arrives for this long (a task that errored
# or was superseded never calls finalize — this prevents accumulator leaks).
_WORKER_IDLE_TIMEOUT = 300.0

# The joined per-round originals must cover at least this fraction of the final
# content (whitespace-insensitive) for the assembly to be trusted; otherwise we
# fall back to a single whole-content translation.
_ASSEMBLY_COVERAGE_MIN = 0.85

# Assistant replies are authored in English when autoTranslate is on (the user
# message was translated to English before the turn ran). Mirrors
# _maybe_auto_translate_assistant's _do_translate(..., 'Chinese', 'English').
_TARGET = 'Chinese'
_SOURCE = 'English'

_accumulators: dict[str, '_Acc'] = {}
_acc_lock = threading.Lock()


def _enabled() -> bool:
    return os.environ.get(_KILL_ENV, '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _gate(task) -> bool:
    """Decide whether incremental translation applies to *task*."""
    if not _enabled():
        return False
    if not task or not task.get('convId'):
        return False
    # Endpoint mode + autopilot have their own translation paths.
    if task.get('_endpoint_managed') or task.get('endpoint_mode'):
        return False
    if task.get('_autopilot_kick') or task.get('_inline_messages'):
        return False
    cfg = task.get('config') or {}
    from lib.conv_config import resolve_auto_translate
    if not resolve_auto_translate(cfg):
        return False
    return True


class _Acc:
    """Per-task ordered accumulator + worker thread."""

    def __init__(self, task):
        self.task_id = task['id']
        self.conv_id = task.get('convId') or ''
        # Stable assistant message id, minted client-side and threaded through
        # task['_assistantMsgId'] at task start (see create_task). Required to
        # route LIVE progressive-partial push frames to the still-streaming
        # bubble — the message has no DB index yet mid-task. Empty when the
        # caller didn't supply one (old frontend / non-UI start path): we then
        # simply skip the live preview and still finalize at task end.
        self.msg_id = task.get('_assistantMsgId') or ''
        self.q: queue.Queue = queue.Queue()
        self.segments: dict[int, str] = {}   # round_num -> translated text
        self.originals: dict[int, str] = {}  # round_num -> original text
        self.model = 'unknown'
        self.lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'inc-translate-{self.task_id[:8]}')
        self.thread.start()

    # ── worker ──────────────────────────────────────────────
    def _run(self):
        from lib.translate.engine import _translate_freetext
        from lib.translate.notranslate import (_extract_notranslate_blocks,
                                                _reattach_notranslate_blocks)
        from lib.translate.prompt import _build_translate_prompt
        system_prompt = _build_translate_prompt(_TARGET, _SOURCE)
        helpers = (system_prompt, _translate_freetext,
                   _extract_notranslate_blocks, _reattach_notranslate_blocks)
        while True:
            try:
                item = self.q.get(timeout=_WORKER_IDLE_TIMEOUT)
            except queue.Empty:
                logger.warning('[IncTranslate] task=%s conv=%s worker idle-timeout '
                               'after %.0fs — abandoning (%d segments translated; '
                               'neither finalize nor cancel arrived — task likely '
                               'errored, aborted, or superseded)',
                               self.task_id[:8], (self.conv_id or '?')[:8],
                               _WORKER_IDLE_TIMEOUT, len(self.segments))
                self._cleanup()
                return
            try:
                kind = item[0]
                if kind == 'seg':
                    _, round_num, text = item
                    self._translate_segment(round_num, text, *helpers)
                elif kind == 'fin':
                    _, conv_id, msg_idx, content, msg_id = item
                    self._do_finalize(conv_id, msg_idx, content, msg_id, *helpers)
                    self._cleanup()
                    return
                elif kind == 'cancel':
                    logger.info('[IncTranslate] task=%s cancelled before finalize '
                                '(%d segments translated, discarded) — caller skipped '
                                'translation or task ended without content',
                                self.task_id[:8], len(self.segments))
                    self._cleanup()
                    return
            except Exception as e:
                logger.error('[IncTranslate] task=%s worker item %r failed: %s',
                             self.task_id[:8], item[0] if item else '?', e,
                             exc_info=True)

    def _translate_segment(self, round_num, text, system_prompt, translate_fn,
                           extract_nt, reattach_nt):
        with self.lock:
            if round_num in self.segments:
                return
        original = text or ''
        if not original.strip():
            return
        try:
            if is_predominantly_chinese(original):
                # Already in the target language — keep the segment verbatim.
                with self.lock:
                    self.originals[round_num] = original
                    self.segments[round_num] = original
                self._push_progressive()
                return
            body, nt_blocks = extract_nt(original)
            if not body.strip():
                translated = original
            else:
                translated, usage = translate_fn(
                    body, system_prompt, source=_SOURCE, target=_TARGET)
                translated = (translated or '').strip()
                if nt_blocks:
                    translated = reattach_nt(translated, nt_blocks)
                if isinstance(usage, dict):
                    disp = usage.get('_dispatch', {})
                    self.model = disp.get('model', usage.get('model', self.model))
            with self.lock:
                self.originals[round_num] = original
                self.segments[round_num] = translated
            logger.debug('[IncTranslate] task=%s round=%d segment translated '
                         '(%d→%d chars)', self.task_id[:8], round_num,
                         len(original), len(translated))
            # ★ Live progressive display: the moment this round's segment is
            #   translated, push the translated-so-far as a partial frame so the
            #   user watches the Chinese fill in round-by-round during the task,
            #   instead of waiting for the whole reply to finish.
            self._push_progressive()
        except Exception as e:
            logger.warning('[IncTranslate] task=%s round=%d segment translate '
                           'failed: %s', self.task_id[:8], round_num, e)
            # Record the original but NOT a translated segment, so the assembly
            # coverage check detects the gap and falls back to a whole-content
            # translation rather than silently dropping this round's prose.
            with self.lock:
                self.originals[round_num] = original

    def _do_finalize(self, conv_id, msg_idx, content, msg_id, system_prompt,
                     translate_fn, extract_nt, reattach_nt):
        content = content or ''
        # The safety net handed us ownership of the per-(conv,msgId) in-flight
        # guard (it set _guard_owned_by_worker before finalize). Release it when
        # this finalize returns — by any path — so a legitimate later
        # re-translate (e.g. message edited) can claim it again.
        try:
            self._do_finalize_inner(conv_id, msg_idx, content, msg_id,
                                    system_prompt, translate_fn, extract_nt,
                                    reattach_nt)
        finally:
            try:
                from lib.translate.inflight import release_inflight
                release_inflight(conv_id, msg_id, msg_idx)
            except Exception as e:
                logger.debug('[IncTranslate] task=%s release_inflight failed: %s',
                             self.task_id[:8], e)

    def _do_finalize_inner(self, conv_id, msg_idx, content, msg_id, system_prompt,
                           translate_fn, extract_nt, reattach_nt):
        from lib.translate.commit import _commit_translation_to_db
        # Surface the live spinner while we assemble / translate any tail.
        self._push({'type': 'running', 'status': 'running',
                    'statusKind': 'started', 'statusMessage': ''},
                   conv_id, msg_idx, msg_id)

        translated = self._assemble(content)
        if translated is None:
            logger.info('[IncTranslate] task=%s assembly unusable — falling back '
                        'to whole-content translation (%d chars)',
                        self.task_id[:8], len(content))
            translated = self._translate_whole(content, system_prompt,
                                                translate_fn, extract_nt,
                                                reattach_nt, conv_id, msg_idx,
                                                msg_id)
            if translated is None:
                return  # error already pushed

        if not translated or not translated.strip():
            logger.debug('[IncTranslate] task=%s nothing to commit', self.task_id[:8])
            return

        try:
            _commit_translation_to_db(conv_id, msg_idx, 'translatedContent',
                                      translated, original_text=content,
                                      model=self.model, msg_id=msg_id or None)
        except Exception as e:
            logger.warning('[IncTranslate] task=%s commit failed: %s',
                           self.task_id[:8], e, exc_info=True)
        self._push({'type': 'done', 'status': 'done',
                    'translated': translated, 'model': self.model},
                   conv_id, msg_idx, msg_id)
        logger.info('[IncTranslate] task=%s ✓ finalized translatedContent '
                    '(%d chars, %d segments, model=%s)',
                    self.task_id[:8], len(translated), len(self.segments),
                    self.model)

    def _translate_whole(self, content, system_prompt, translate_fn,
                         extract_nt, reattach_nt, conv_id, msg_idx, msg_id):
        """Single whole-content translation fallback. Returns text or None."""
        try:
            if is_predominantly_chinese(content):
                return content
            body, nt_blocks = extract_nt(content)
            if not body.strip():
                return content
            translated, usage = translate_fn(body, system_prompt,
                                             source=_SOURCE, target=_TARGET)
            translated = (translated or '').strip()
            if nt_blocks:
                translated = reattach_nt(translated, nt_blocks)
            if isinstance(usage, dict):
                disp = usage.get('_dispatch', {})
                self.model = disp.get('model', usage.get('model', self.model))
            return translated
        except Exception as e:
            logger.error('[IncTranslate] task=%s whole-content fallback failed: %s',
                         self.task_id[:8], e, exc_info=True)
            self._push({'type': 'error', 'status': 'error', 'error': str(e)[:300]},
                       conv_id, msg_idx, msg_id)
            return None

    def _assemble(self, content):
        """Join cached translated segments in round order.

        Returns the assembled translation, or None when the cached segments
        don't faithfully cover the final *content* (a gap, or low coverage) so
        the caller falls back to a single whole-content translation.
        """
        with self.lock:
            if not self.segments:
                return None
            rounds = sorted(self.originals.keys())
            # Every round we captured an original for must also have a
            # translated segment, else there's a gap (a failed segment).
            for rn in rounds:
                if rn not in self.segments:
                    return None
            joined_orig = '\n\n'.join(self.originals[rn] for rn in rounds
                                      if self.originals[rn].strip())
            joined_trans = '\n\n'.join(self.segments[rn] for rn in rounds
                                       if self.segments[rn].strip())
        co = ''.join(content.split())
        if not co:
            return joined_trans or None
        jo = ''.join(joined_orig.split())
        coverage = len(jo) / len(co)
        if coverage < _ASSEMBLY_COVERAGE_MIN:
            logger.info('[IncTranslate] task=%s assembly coverage %.2f < %.2f '
                        '(joined_orig=%d content=%d) — fallback',
                        self.task_id[:8], coverage, _ASSEMBLY_COVERAGE_MIN,
                        len(jo), len(co))
            return None
        return joined_trans

    def _assemble_progressive(self):
        """Join currently-translated segments in round order for a LIVE preview.

        Unlike :meth:`_assemble`, there is NO coverage gate — this is a
        best-effort partial shown WHILE the task is still streaming (covers only
        the rounds closed so far), not the committed final translation.
        """
        with self.lock:
            if not self.segments:
                return ''
            rounds = sorted(self.segments.keys())
            return '\n\n'.join(self.segments[rn] for rn in rounds
                               if self.segments[rn].strip())

    def _push_progressive(self):
        """Emit a live ``running``/``partial`` frame with the translated-so-far.

        Routed by the task-time assistant ``msg_id`` (the only stable handle to
        the still-streaming bubble — it has no DB index yet). No-op when that id
        is unknown, so a non-UI / old-frontend start path silently degrades to
        the existing end-of-task display with zero regression.
        """
        if not self.conv_id or not self.msg_id:
            return
        partial = self._assemble_progressive()
        if not partial:
            return
        self._push({'type': 'running', 'status': 'running',
                    'statusKind': 'in_progress', 'partial': partial},
                   self.conv_id, None, self.msg_id)

    def _push(self, payload, conv_id, msg_idx, msg_id):
        if not conv_id:
            return
        try:
            from lib.push import push_event
            frame = {'convId': conv_id, 'msgIdx': msg_idx,
                     'msgId': msg_id or '', 'field': 'translatedContent'}
            frame.update(payload)
            push_event('translate', self.task_id, frame)
        except Exception as e:
            logger.debug('[IncTranslate] task=%s push failed: %s',
                         self.task_id[:8], e)

    def _cleanup(self):
        with _acc_lock:
            if _accumulators.get(self.task_id) is self:
                del _accumulators[self.task_id]


def submit_round_segment(task, round_num, text):
    """Translate one round's assistant text segment in the background.

    Safe to call unconditionally from the orchestrator — the gate decides
    whether incremental translation applies. Exceptions are swallowed (logged)
    so translation can never break the agent loop.
    """
    try:
        if not text or not text.strip():
            return
        if not _gate(task):
            return
        tid = task['id']
        with _acc_lock:
            acc = _accumulators.get(tid)
            if acc is None:
                acc = _Acc(task)
                _accumulators[tid] = acc
                task['_incremental_translate_active'] = True
        acc.q.put(('seg', int(round_num), text))
    except Exception as e:
        logger.warning('[IncTranslate] submit_round_segment failed task=%s: %s',
                       (task or {}).get('id', '?')[:8], e)


def finalize_incremental(task, conv_id, msg_idx, content, msg_id=None) -> bool:
    """Signal the per-task worker to assemble + commit the final translation.

    Returns True if the incremental path owns this translation (caller must
    skip the whole-message fallback), False if no incremental accumulator was
    active (caller falls back to the whole-message path).
    """
    try:
        tid = task.get('id') if task else None
        if not tid:
            return False
        with _acc_lock:
            acc = _accumulators.get(tid)
        if acc is None:
            return False
        acc.q.put(('fin', conv_id, msg_idx, content or '', msg_id))
        return True
    except Exception as e:
        logger.warning('[IncTranslate] finalize_incremental failed task=%s: %s',
                       (task or {}).get('id', '?')[:8], e)
        return False


def cancel_incremental(task) -> bool:
    """Stop a per-task accumulator's worker WITHOUT finalizing.

    Call this whenever the task ends without the incremental translation being
    committed — the caller decided to skip translation (autoTranslate off,
    content already translated / already in the target language, a frontend
    task owns it) OR the task errored / produced no content so
    :func:`finalize_incremental` will never run. Without this the worker thread
    sits idle until ``_WORKER_IDLE_TIMEOUT`` (300s) and then logs a misleading
    "finalize never called" warning, and the pre-translated segments are
    silently discarded.

    Returns True if an accumulator was found and signalled to cancel, False if
    there was none (the common case — most tasks have autoTranslate off).
    """
    try:
        tid = task.get('id') if task else None
        if not tid:
            return False
        with _acc_lock:
            acc = _accumulators.get(tid)
        if acc is None:
            return False
        acc.q.put(('cancel',))
        return True
    except Exception as e:
        logger.warning('[IncTranslate] cancel_incremental failed task=%s: %s',
                       (task or {}).get('id', '?')[:8], e)
        return False

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
   Signals the worker to drain and commit the DELIVERABLE-only
   ``translatedContent`` (reusing the terminal deliverable's already-cached
   per-round translation; a fresh whole-content translation only when it isn't
   cached). The inter-round narration is NOT part of ``translatedContent`` — it
   is carried per-segment via ``segment_translations`` (stamped onto
   ``msg['segments'][].translatedText``) and rendered inline by the settled
   timeline. Commits race-safely via
   :func:`lib.translate.commit._commit_translation_to_db` and pushes a ``done``
   frame (carrying ``segmentsByRound`` so the live view stamps the narration
   without a reload). Non-blocking: the worker thread does the commit / push.

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
        # Authoritative THIN segments (segments_to_json(task['segments'])),
        # captured at finalize time. Handed to the commit so the per-round
        # stamp can SELF-HEAL: if the finalize commit resolves a DB message
        # that has no `segments` yet (it raced ahead of / lost the row-write
        # to _sync_result_to_conversation), the commit splices these on in the
        # SAME CAS write, then stamps — so the 19 translations can never be
        # silently dropped onto a segment-less row.
        self._fallback_segments = None
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
                    _, conv_id, msg_idx, content, msg_id, fallback_segments = item
                    self._fallback_segments = fallback_segments
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
            # Record the original but NOT a translated segment. For the terminal
            # deliverable this means _deliverable_translation finds no cached
            # match and re-translates the whole content; for a narration round
            # the segment simply carries no translatedText (renders in English).
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

        # ★ translatedContent is the DELIVERABLE answer ONLY — never the joined
        #   per-round narration. The narration is carried per-segment via
        #   `segment_translations` (stamped onto msg['segments'][].translatedText)
        #   and rendered INLINE by the settled segment timeline. Assembling the
        #   whole reply here painted that narration a SECOND time as one block
        #   below the turn — the reported "all content clumps at the tail after
        #   finalize" bug — and made the bilingual 原文/译文 pair asymmetric
        #   (原文 = msg.content = deliverable-only). The terminal deliverable was
        #   submitted as its own round segment (orchestrator: submit_round_segment
        #   with the final content), so reuse that cached translation; translate
        #   afresh only when it isn't cached (e.g. a Sources-footer /
        #   content-filter override rewrote task['content'] after capture).
        translated = self._deliverable_translation(
            content, system_prompt, translate_fn, extract_nt, reattach_nt,
            conv_id, msg_idx, msg_id)
        if translated is None:
            return  # error already pushed by _translate_whole

        if not translated or not translated.strip():
            logger.debug('[IncTranslate] task=%s nothing to commit', self.task_id[:8])
            return

        # ★ Per-round carry to the SETTLED render: hand the commit the
        #   {round_num: 中文} map so it stamps `translatedText` onto each
        #   matching text segment of msg['segments'] (keyed by llmRound ≡
        #   round_num). renderSegmentTimelineHTML then shows the translated
        #   narration in its .seg-narration slot — the settled view stays
        #   interleaved exactly like the streaming view (no finalize snap-back).
        with self.lock:
            seg_trans = {rn: txt for rn, txt in self.segments.items()
                         if txt and txt.strip()}
        try:
            _commit_translation_to_db(conv_id, msg_idx, 'translatedContent',
                                      translated, original_text=content,
                                      model=self.model, msg_id=msg_id or None,
                                      segment_translations=seg_trans or None,
                                      fallback_segments=self._fallback_segments)
        except Exception as e:
            logger.warning('[IncTranslate] task=%s commit failed: %s',
                           self.task_id[:8], e, exc_info=True)
        # ★ Carry the per-round narration Chinese on the done frame (str keys —
        #   JSON object) so the LIVE view can stamp msg['segments'][].translatedText
        #   WITHOUT a reload — the settled timeline then keeps its narration in
        #   Chinese immediately at finalize (a reconnect reads the same already-
        #   stamped segments from the DB commit above).
        segments_by_round = {str(rn): txt for rn, txt in seg_trans.items()}
        self._push({'type': 'done', 'status': 'done',
                    'translated': translated, 'model': self.model,
                    'segmentsByRound': segments_by_round},
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

    def _deliverable_translation(self, content, system_prompt, translate_fn,
                                 extract_nt, reattach_nt, conv_id, msg_idx, msg_id):
        """Translate ONLY the deliverable answer for the ``translatedContent`` blob.

        ``content`` is ``task['content']`` — the terminal-round deliverable
        (inter-round narration was already zeroed by ``_discard_pretool_prose``).
        The deliverable was submitted as its OWN round segment, so reuse that
        cached translation (whitespace-insensitive original match) to avoid a
        redundant LLM call; fall back to a fresh whole-content translation when
        there is no cached match (e.g. a Sources-footer / content-filter
        override rewrote ``task['content']`` after the segment was captured).

        Returns the translation, ``''`` when content is empty, or ``None`` on a
        translate error (which ``_translate_whole`` has already pushed).
        """
        content = content or ''
        if not content.strip():
            return ''
        key = ''.join(content.split())
        with self.lock:
            for rn, orig in self.originals.items():
                if orig and ''.join(orig.split()) == key:
                    cached = self.segments.get(rn)
                    if cached and cached.strip():
                        return cached
        return self._translate_whole(content, system_prompt, translate_fn,
                                     extract_nt, reattach_nt, conv_id, msg_idx,
                                     msg_id)

    def _assemble_progressive(self):
        """Join currently-translated segments in round order for a LIVE preview.

        This is the best-effort joined blob shown WHILE the task is still
        streaming (covers the rounds closed so far); the settled render routes
        each round's Chinese into its tool group via ``partialByRound`` and the
        committed ``translatedContent`` is the deliverable only (see
        :meth:`_deliverable_translation`).
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
        # ★ Per-round interleave: also carry each closed round's translated
        #   Chinese keyed by round_num (≡ frontend llmRound — tool_dispatch.py
        #   stamps round_entry['llmRound'] = round_num). The frontend routes
        #   each round's text into its matching .ptool-turn narration slot so
        #   the translation interleaves with the tools it describes instead of
        #   piling up as one blob below the whole panel. `partial` (the joined
        #   blob) is kept for graceful degrade when a round's .ptool-turn isn't
        #   in the DOM yet.
        with self.lock:
            by_round = {str(rn): txt for rn, txt in self.segments.items()
                        if txt and txt.strip()}
        self._push({'type': 'running', 'status': 'running',
                    'statusKind': 'in_progress', 'partial': partial,
                    'partialByRound': by_round},
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
        # ★ Capture the AUTHORITATIVE thin segments now (task['segments'] is
        #   assembled by persist_task_result before this handoff fires) and
        #   hand them to the finalize commit as a self-heal fallback. This is
        #   the SSOT ordering guarantee: the stamp no longer depends on the
        #   conversation row-write having already landed the segments — if the
        #   DB row lacks them at commit time (finalize/sync race, or a frontend
        #   PUT won the row-write CAS), the commit writes these in the same CAS
        #   write. Best-effort: a serialize failure just skips the fallback
        #   (degrades to the pre-fix behaviour, never breaks finalize).
        fallback_segments = None
        try:
            _segs = task.get('segments')
            if _segs:
                from lib.tasks_pkg.segments import segments_to_json
                fallback_segments = segments_to_json(_segs)
        except Exception as _fe:
            logger.debug('[IncTranslate] finalize fallback-segments capture failed '
                         'task=%s: %s', tid[:8], _fe)
        acc.q.put(('fin', conv_id, msg_idx, content or '', msg_id, fallback_segments))
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

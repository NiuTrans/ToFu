"""Incrementally translate narration while an authoritative turn runs.

One accumulator belongs to one executor task. During generation it translates
every closed narration/reasoning segment without count/character admission
gates and emits live previews; pending narration is translated before pending
reasoning.
Task-local preview policy and completed segment cache survive idle thread
retirement without retaining a full task or an idle worker. Writes still use
settled Turn authority; running Turns defer their cache to terminal handoff,
and ``preview_translations_for_conversation`` lets the owner's browser re-read
the completed previews of a running Turn after a reload or a missed frame.
Provider failures pause one preview kind (narration or reasoning) for a
bounded window; missed blocks retry behind later submissions.

The whole-turn terminal DELIVERABLE does NOT commit through this accumulator.
Once the turn settles, ``lib.translate.terminal`` spawns a durable
``_translate_runtime`` TaskRuntime task for it (the same authority the boot
sweep and goal-mode per-turn boundary use), so a provider failure or process
restart leaves a persisted ``_translateFailed`` verdict the boot sweep can
re-spawn. ``finalize_incremental`` is therefore a STAMP-ONLY handoff: it pins
the run's cached preview translations onto the projection's segments via
``commit_translation_to_turn(field=None, ...)``. No conversation transcript or
positional message identity participates.
"""

from __future__ import annotations

from contextlib import nullcontext
import os
import queue
import re as re
import threading
import time
import weakref
from typing import Any

from lib.log import get_logger
from lib.translate._operation_buffer import IncrementalOperationBuffer, segment_kind
from lib.translate._completed_cache import CompletedTranslationCache
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)
_SOURCE = "English"
_STOP = object()
# The knob is the floor of the preview deadline. A flat 30s cut previews in
# proportion to their size (docs/modules/ingest_media.md, "Preview deadline"),
# so longer sources earn the generation time they need: one response-header
# bound plus 1s per 180 source characters, the slowest decile's ~100 output
# chars/s at the p90 0.55 output/source ratio.
# The cap bounds what one optional preview may hold its turn's worker for.
_PREVIEW_DEADLINE_SECONDS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS',
    minimum=5,
    maximum=300,
)
_PREVIEW_SOURCE_CHARS_PER_SECOND = 180.0
_PREVIEW_DEADLINE_CAP_SECONDS = 120.0
_PREVIEW_MAX_429_ATTEMPTS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS',
    minimum=1,
    maximum=8,
)
_TASK_PREVIEW_STATE = '_incremental_translate_preview_state'
_PREVIEW_STATE_STARTED = 'started'
# One circuit per preview kind (narration, reasoning) holds the three fields
# below, so a failing kind never pauses or defers the other.
_PREVIEW_STATE_CIRCUITS = 'circuits'
_PREVIEW_STATE_FAILURES = 'failures'
_PREVIEW_STATE_PAUSED_UNTIL = 'pausedUntil'
_PREVIEW_STATE_PAUSE_SECONDS = 'pauseSeconds'
_PREVIEW_STATE_ATTEMPTS = 'attempts'
_PREVIEW_STATE_DEFERRED = 'deferred'
# Consecutive failures of one preview kind that pause that kind's previews.
# One transient 429 or read timeout used to disable them permanently, so a long
# turn kept its opening translation and then silently reverted to the source
# language for every later round. Retry across rounds instead; three misses in
# a row leave the provider's capacity to the terminal deliverable translation
# (a separate path) for one pause window.
_PREVIEW_FAILURE_LIMIT = 3
# A pause, not retirement for the rest of the turn: in the 2026-10-27/28 logs
# another task's preview succeeded within 28 s after 5 of 7 trips, while each
# tripped turn stayed in the source language until it settled. The window
# doubles per failed probe, so a provider that stays down costs one probe per
# window (docs/modules/ingest_media.md, "Preview circuit recovery").
_PREVIEW_PAUSE_BASE_SECONDS = 60.0
_PREVIEW_PAUSE_CAP_SECONDS = 600.0
# Missed blocks retry behind later submissions; after this many provider
# attempts a block waits for the settled-turn enrichment pass instead.
_PREVIEW_MAX_ATTEMPTS_PER_BLOCK = 3


_accumulators: dict[str, "_Accumulator"] = {}
_accumulators_lock = threading.RLock()
_worker_threads: weakref.WeakSet[threading.Thread] = weakref.WeakSet()
_shutdown_started = False
_retired_stamps: dict[int, int] = {}
_shutdown_condition = threading.Condition(_accumulators_lock)


def _preview_deadline_seconds(source: str) -> float:
    """Return the wall budget for previewing ``source``; reads no hardware."""
    from lib.translate.engine import translation_first_byte_timeout_seconds

    chars = len(source)
    needed = (translation_first_byte_timeout_seconds(chars)
              + chars / _PREVIEW_SOURCE_CHARS_PER_SECOND)
    return max(float(_PREVIEW_DEADLINE_SECONDS),
               min(_PREVIEW_DEADLINE_CAP_SECONDS, needed))


def _enabled_for(task: dict[str, Any] | None) -> bool:
    if os.environ.get("TOFU_INCREMENTAL_TRANSLATE", "1").lower() in {
        "0", "false", "no", "off",
    }:
        return False
    if not task or not task.get("id") or not task.get("convId"):
        return False
    if not task.get("_turnId"):
        return False
    config = task.get("config") or {}
    from lib.conv_config import resolve_auto_translate

    return bool(resolve_auto_translate(config))


def _owner_id(task: dict[str, Any]):
    from lib.tasks_pkg.manager._registry import task_user_id

    return task_user_id(task)


def _task_preview_state(
    task: dict[str, Any], *, create: bool = False,
) -> dict[str, Any]:
    state = task.get(_TASK_PREVIEW_STATE)
    if isinstance(state, dict):
        return state
    if not create:
        return {}
    state = {}
    task[_TASK_PREVIEW_STATE] = state
    return state


def _task_preview_started(task: dict[str, Any]) -> int:
    try:
        return max(0, int(
            _task_preview_state(task).get(_PREVIEW_STATE_STARTED) or 0))
    except (TypeError, ValueError):
        return 0


def _preview_circuit(state: dict[str, Any], kind: str) -> dict[str, Any]:
    return state.setdefault(_PREVIEW_STATE_CIRCUITS, {}).setdefault(kind, {})


def _preview_paused(state: dict[str, Any], kind: str) -> bool:
    circuit = (state.get(_PREVIEW_STATE_CIRCUITS) or {}).get(kind) or {}
    paused_until = circuit.get(_PREVIEW_STATE_PAUSED_UNTIL)
    return paused_until is not None and time.monotonic() < paused_until


def _pause_preview_circuit(circuit: dict[str, Any]) -> float:
    previous = circuit.get(_PREVIEW_STATE_PAUSE_SECONDS)
    pause = (_PREVIEW_PAUSE_BASE_SECONDS if previous is None
             else min(_PREVIEW_PAUSE_CAP_SECONDS, previous * 2))
    circuit[_PREVIEW_STATE_PAUSE_SECONDS] = pause
    circuit[_PREVIEW_STATE_PAUSED_UNTIL] = time.monotonic() + pause
    return pause


def _defer_preview(
    state: dict[str, Any], key: int | str, text: str, *, failed: bool = False,
) -> bool:
    """Keep a missed preview's source for a later attempt.

    Sources live in their own lazy anonymous file, like the completed cache,
    so a long pause never moves backlog text onto the heap. A block whose
    attempts are spent is left to the settled-turn enrichment pass.
    """
    with state.setdefault('cacheLock', threading.Lock()):
        if state.get('cancelled'):
            return False
        attempts = state.setdefault(_PREVIEW_STATE_ATTEMPTS, {})
        spent = attempts.get(key, 0) + int(failed)
        if failed:
            attempts[key] = spent
        deferred = state.get(_PREVIEW_STATE_DEFERRED)
        if spent >= _PREVIEW_MAX_ATTEMPTS_PER_BLOCK:
            if deferred is not None:
                deferred.pop(key, None)
            return False
        if deferred is None:
            deferred = CompletedTranslationCache().view('originals')
            state[_PREVIEW_STATE_DEFERRED] = deferred
        deferred[key] = str(text)
        return True


def _clear_task_preview_state(
    task: dict[str, Any] | None, *, cancelled: bool = False,
) -> None:
    if not isinstance(task, dict):
        return
    state = task.pop(_TASK_PREVIEW_STATE, None)
    if not isinstance(state, dict):
        return
    lock = state.get('cacheLock')
    with lock if lock is not None else nullcontext():
        # Missed previews never outlive their turn's handoff: the settled-turn
        # enrichment pass owns every block still lacking a translation.
        deferred = state.get(_PREVIEW_STATE_DEFERRED)
        if deferred is not None:
            deferred.clear()
        if not cancelled:
            return
        # A retiring worker holds the small state, never the full task. Stop
        # invalidates that detached reference even after queue admission closed.
        state['cancelled'] = True
        cache = state.get('cache')
        if cache is not None:
            cache.clear()
        else:
            state.get('translations', {}).clear()
            state.get('originals', {}).clear()


def _model_batch_segment_key(
    task: dict[str, Any], segment_type: str, round_num: int,
) -> str:
    """Match the exact block id final segment assembly will persist."""
    from lib.tool_results.tool_round_identity import model_batch_segment_block_id

    return model_batch_segment_block_id(
        segment_type,
        round_num,
        attempt_id=task.get('_attemptId') or task.get('attemptId') or '',
        task_id=task.get('id') or task.get('taskId') or '',
    )


class _Accumulator:
    def __init__(self, task: dict[str, Any]) -> None:
        from lib.conv_config import resolve_translate_target, target_lang_code

        self._preview_state = _task_preview_state(task, create=True)
        self.task_id = str(task["id"])
        self.conversation_id = str(task["convId"])
        self.turn_id = str(task["_turnId"])
        self.message_id = str(task.get("_assistantMsgId") or "")
        self.user_id = _owner_id(task)
        self.target = resolve_translate_target(task.get("config") or {})
        self.target_code = target_lang_code(self.target)
        # Keys are stable segment blockIds. They carry attempt identity, so a
        # Continue task's local R0 cannot overwrite an earlier attempt's R0.
        # Cache views share one anonymous file and survive worker retirement.
        self._lock = self._preview_state.setdefault('cacheLock', threading.Lock())
        with self._lock:
            cache = self._preview_state.get('cache')
            if cache is None:
                cache = CompletedTranslationCache()
                originals, translations = cache.view('originals'), cache.view('translations')
                for key, value in self._preview_state.get('originals', {}).items():
                    originals[key] = value
                for key, value in self._preview_state.get('translations', {}).items():
                    translations[key] = value
                self._preview_state.update(cache=cache, originals=originals, translations=translations)
            self._cache = cache
            self.originals = self._preview_state['originals']
            self.translations = self._preview_state['translations']
        self.model = "unknown"
        self._preview_segments_started = _task_preview_started(task)
        self._cancel_event = threading.Event()
        self._startup_gate = threading.Event()
        self._startup_requested = False
        self._queue = IncrementalOperationBuffer()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"turn-translate-{self.turn_id[:8]}",
        )

    def start(self) -> None:
        with _accumulators_lock:
            if _shutdown_started or self._cancel_event.is_set():
                raise RuntimeError('incremental translation owner is closing')
            if self._thread.ident is not None:
                raise RuntimeError('incremental translation worker already started')
            self._startup_requested = True
            _worker_threads.add(self._thread)
            try:
                self._thread.start()
            except BaseException:
                if not self._thread.is_alive():
                    _worker_threads.discard(self._thread)
                self._cancel_event.set()
                self._queue.close()
                self._startup_gate.set()
                raise
            self._startup_gate.set()

    def submit(self, key: int | str, text: str) -> bool:
        if self._queue.put_segment(key, str(text or '')) < 0:
            return False
        if key != 'thinking:terminal':
            self._requeue_deferred(key)
        return True

    def _requeue_deferred(self, submitted: int | str) -> None:
        """Queue missed previews of unpaused kinds behind an accepted block."""
        with self._lock:
            deferred = self._preview_state.get(_PREVIEW_STATE_DEFERRED)
            if not deferred:
                return
            # A newer submission of the same block supersedes its old source.
            deferred.pop(submitted, None)
            for key in list(deferred):
                if _preview_paused(self._preview_state, segment_kind(key)):
                    continue
                if self._queue.put_segment(key, deferred[key]) < 0:
                    return
                del deferred[key]

    def finalize(self, content: str) -> bool:
        # Migration: the whole-turn terminal DELIVERABLE no longer commits
        # through this in-memory accumulator. It runs on the durable
        # ``_translate_runtime`` TaskRuntime (terminal.py routes the root turn
        # to ``_spawn_whole_turn_translation``), so a provider failure or
        # process restart leaves a persisted ``_translateFailed`` verdict the
        # boot sweep can re-spawn — instead of vanishing with the daemon
        # thread. ``content`` is therefore no longer used here. This method
        # remains only to pin the run's cached preview translations onto the
        # projection's segments (enrich-only), identical to ``stamp_only``.
        return self.stamp_only()

    def stamp_only(self) -> bool:
        if self._cancel_event.is_set():
            return False
        dropped = self._queue.put_terminal(
            ("stamp",),
            replace=True,
            preserve_segment_keys=frozenset({'thinking:terminal'}),
        )
        return dropped >= 0

    def cancel(self) -> bool:
        self._cancel_event.set()
        with self._lock:
            self._preview_state['cancelled'] = True
            self._cache.clear()
            deferred = self._preview_state.get(_PREVIEW_STATE_DEFERRED)
            if deferred is not None:
                deferred.clear()
        dropped = self._queue.put_terminal(_STOP, replace=True, discard_segments=True)
        if self._thread.ident is None:
            self._queue.close()
            with _accumulators_lock:
                if _accumulators.get(self.task_id) is self:
                    _accumulators.pop(self.task_id, None)
        return dropped >= 0

    def _run(self) -> None:
        try:
            if self._startup_requested:
                self._startup_gate.wait()
                if self._cancel_event.is_set():
                    return
            while True:
                try:
                    item = self._queue.get(timeout=0)
                except queue.Empty:
                    # Empty ownership retires immediately. Paid cache stays
                    # task-local until terminal handoff; no full-cache idle
                    # copy/commit is needed for each closed segment.
                    logger.debug(
                        "[IncTranslate] task=%s idle worker retired; "
                        "completed segment cache retained for terminal handoff",
                        self.task_id[:8],
                    )
                    return
                if item is _STOP:
                    return
                operation = item[0]
                if operation == "segment":
                    self._process_preview_segment(item[1], item[2])
                    continue
                if operation == "stamp":
                    self._commit_segments_only()
                    return
                raise RuntimeError(f"Unknown incremental translation operation: {operation}")
        except Exception as exc:
            logger.error(
                "[IncTranslate] task=%s failed: %s",
                self.task_id[:8], exc, exc_info=True,
            )
            self._push({
                "type": "error", "status": "error", "error": str(exc)[:300],
            })
        finally:
            try:
                self._queue.close()
            finally:
                with _accumulators_lock:
                    if _accumulators.get(self.task_id) is self:
                        _accumulators.pop(self.task_id, None)

    def _process_preview_segment(self, key: int | str, original: str) -> None:
        """Translate one optional preview without risking terminal delivery."""
        terminal_reasoning = key == 'thinking:terminal'
        kind = segment_kind(key)
        if not terminal_reasoning and _preview_paused(self._preview_state, kind):
            _defer_preview(self._preview_state, key, original)
            return
        if not terminal_reasoning:
            self._preview_segments_started += 1
            self._preview_state[_PREVIEW_STATE_STARTED] = (
                self._preview_segments_started)
        try:
            self._translate_segment(
                key,
                original,
                max_429_attempts=(
                    None if terminal_reasoning
                    else _PREVIEW_MAX_429_ATTEMPTS),
                defer_on_shared_contention=not terminal_reasoning,
            )
        except Exception as exc:
            # A preview is reconstructible enrichment. Do not tear down the
            # accumulator and lose its final translation because a saturated
            # provider or malformed intermediate segment failed.
            pause = None
            failures = 0
            if not terminal_reasoning:
                circuit = _preview_circuit(self._preview_state, kind)
                failures = circuit.get(_PREVIEW_STATE_FAILURES, 0) + 1
                circuit[_PREVIEW_STATE_FAILURES] = failures
                if failures >= _PREVIEW_FAILURE_LIMIT:
                    pause = _pause_preview_circuit(circuit)
                _defer_preview(self._preview_state, key, original, failed=True)
            logger.warning(
                '[IncTranslate] task=%s %s segment %s failed '
                '(%s consecutive=%d/%d); terminal translation remains enabled: %s',
                self.task_id[:8],
                ('terminal reasoning' if terminal_reasoning
                 else 'preview retrying after'),
                key, kind, failures, _PREVIEW_FAILURE_LIMIT,
                str(exc)[:300],
            )
            if pause is not None:
                logger.warning(
                    '[IncTranslate] task=%s %s previews paused for %.0fs; '
                    'missed blocks retry after the pause',
                    self.task_id[:8], kind, pause,
                )
        else:
            if terminal_reasoning:
                return
            self._preview_state.get(_PREVIEW_STATE_ATTEMPTS, {}).pop(key, None)
            circuit = _preview_circuit(self._preview_state, kind)
            circuit.pop(_PREVIEW_STATE_FAILURES, None)
            if circuit.pop(_PREVIEW_STATE_PAUSE_SECONDS, None):
                circuit.pop(_PREVIEW_STATE_PAUSED_UNTIL, None)
                logger.info(
                    '[IncTranslate] task=%s %s previews resumed after a pause',
                    self.task_id[:8], kind)

    def _translate_segment(
        self,
        key: int | str,
        original: str,
        *,
        max_429_attempts: int | None,
        defer_on_shared_contention: bool,
    ) -> None:
        original = str(original or "")
        if not original.strip():
            return
        with self._lock:
            if key in self.originals:
                return
        translated, model = self._translate(
            original,
            progress_cb=lambda partial: self._push_segment_progress(
                key, partial,
            ),
            overall_deadline=_preview_deadline_seconds(original),
            max_429_attempts=max_429_attempts,
            defer_on_shared_contention=defer_on_shared_contention,
        )
        with self._lock:
            if self._cancel_event.is_set() or self._preview_state.get('cancelled'):
                return
            self._cache.put_pair(key, original, translated)
            if model:
                self.model = model
        self._push_segment_progress(key, translated)

    def _translate(
        self,
        original: str,
        progress_cb=None,
        overall_deadline: float | None = None,
        max_429_attempts: int | None = None,
        defer_on_shared_contention: bool = False,
    ) -> tuple[str, str]:
        from lib.translate.notranslate import (
            _extract_notranslate_blocks,
            _has_translatable_text,
            _reattach_notranslate_blocks,
            _reattach_notranslate_blocks_partial,
        )

        body, protected = _extract_notranslate_blocks(original)
        if protected and not _has_translatable_text(body):
            # A protected-only preview needs neither fastText nor a provider
            # call. Restore the visible payload locally so markup never leaks
            # into the live preview or terminal segment stamp.
            return _reattach_notranslate_blocks(body, protected).strip(), "skipped"

        from lib.translate.skip_policy import (
            should_skip_automatic_translation,
        )

        if should_skip_automatic_translation(
                original, self.target, self.target_code):
            return original, "skipped"

        from lib.translate.engine import _translate_freetext
        from lib.translate.prompt import _build_translate_prompt

        def visible_progress(partial):
            if progress_cb is not None:
                progress_cb(_reattach_notranslate_blocks_partial(
                    partial, protected,
                ))

        translated, usage = _translate_freetext(
            body,
            _build_translate_prompt(self.target, _SOURCE),
            source=_SOURCE,
            target=self.target,
            progress_cb=visible_progress if progress_cb is not None else None,
            overall_deadline=overall_deadline,
            abort_check=self._cancel_event.is_set,
            max_429_attempts=max_429_attempts,
            defer_on_shared_contention=defer_on_shared_contention,
        )
        translated = (translated or "").strip()
        if protected:
            translated = _reattach_notranslate_blocks(translated, protected)
        dispatch = usage.get("_dispatch", {}) if isinstance(usage, dict) else {}
        model = dispatch.get("model") or (
            usage.get("model") if isinstance(usage, dict) else None
        ) or "unknown"
        if not translated:
            raise RuntimeError("Incremental translation produced empty content")
        return translated, model

    def _commit_segments_only(self, *, terminal: bool = True) -> bool:
        with self._lock:
            if self._cancel_event.is_set() or self._preview_state.get('cancelled'):
                return False
            snapshot = self._cache.freeze(read_lock=self._lock)
        try:
            return self._commit_cache_snapshot(snapshot, terminal=terminal)
        finally:
            snapshot.close()

    def _commit_cache_snapshot(self, snapshot, *, terminal: bool) -> bool:
        # Commit reads only keys present in the authoritative Turn. These
        # borrowed views remain pinned across storage/CAS retries and cancel.
        by_round, sources = snapshot.translations, snapshot.originals
        if not by_round:
            return False
        from lib.translate.commit import commit_translation_to_turn
        from lib.turn_lifecycle import LifecycleConflict

        try:
            commit_translation_to_turn(
                self.conversation_id,
                self.turn_id,
                None,
                "",
                user_id=self.user_id,
                segment_translations=by_round,
                segment_sources=sources,
            )
        except Exception as exc:
            if terminal:
                raise
            if isinstance(exc, LifecycleConflict) and exc.code == 'turn_in_progress':
                logger.debug('[IncTranslate] task=%s cache awaits Turn settlement',
                             self.task_id[:8])
            else:
                logger.warning('[IncTranslate] task=%s idle stamp deferred: %s',
                               self.task_id[:8], exc)
            return False
        if terminal:
            with self._lock:
                self._cache.clear()
            self._push({
                "type": "done",
                "status": "done",
            })
        return True

    def _push_segment_progress(self, key: int | str, partial: str) -> None:
        partial = str(partial or '')
        if not partial.strip():
            return
        # The receiver owns a per-block ephemeral index. Do not clone/re-send
        # completed history for every provider chunk or closed segment.
        # Each partial is cumulative, so a socket that is behind skips to the
        # block's newest value; the final translation is the last value.
        self._push({
            'type': 'running', 'status': 'running', 'statusKind': 'in_progress',
            'partialByRound': {str(key): partial},
        }, latest_value_key=str(key))

    def _push(self, payload: dict[str, Any], *,
              latest_value_key: str | None = None) -> None:
        if self._cancel_event.is_set():
            return
        try:
            from lib.agent_core.push import push_event

            push_event("translate", self.task_id, {
                **payload,
                "convId": self.conversation_id,
                "turnId": self.turn_id,
                "msgId": self.message_id,
                "field": "translatedContent",
            }, user_id=self.user_id, latest_value_key=latest_value_key)
        except Exception as exc:
            logger.debug(
                "[IncTranslate] task=%s push failed: %s",
                self.task_id[:8], exc,
            )


def _submit_keyed(task, key: int | str, text) -> bool:
    """Shared registry/queue path for narration rounds and reasoning blocks."""
    try:
        if not text or not str(text).strip() or not _enabled_for(task):
            return False
        terminal_reasoning = key == 'thinking:terminal'
        preview_state = _task_preview_state(task)
        if not terminal_reasoning and _preview_paused(
                preview_state, segment_kind(key)):
            # The block keeps its claim on a preview: it re-enters the queue
            # behind the first accepted submission after its kind's pause.
            _defer_preview(preview_state, key, str(text))
            return False
        task_id = str(task["id"])
        while True:
            created = False
            with _accumulators_lock:
                if _shutdown_started or task.get('_incremental_translate_terminal_requested'):
                    return False
                accumulator = _accumulators.get(task_id)
                if accumulator is None:
                    accumulator = _Accumulator(task)
                    _accumulators[task_id] = accumulator
                    task['_incremental_translate_active'] = True
                    created = True
            try:
                accepted = accumulator.submit(key, str(text))
                if not accepted:
                    if getattr(accumulator, '_queue', None) is not None and accumulator._queue.retired_for_idle():
                        with _accumulators_lock:
                            if _accumulators.get(task_id) is accumulator:
                                _accumulators.pop(task_id, None)
                        continue
                    if created:
                        accumulator._queue.close()
                        with _accumulators_lock:
                            if _accumulators.get(task_id) is accumulator:
                                _accumulators.pop(task_id, None)
                    return False
                with _accumulators_lock:
                    thread = getattr(accumulator, '_thread', None)
                    if (created and thread is None) or (thread is not None and thread.ident is None):
                        accumulator.start()
                return True
            except Exception:
                queue_owner = getattr(accumulator, '_queue', None)
                with _accumulators_lock:
                    thread = getattr(accumulator, '_thread', None)
                    cancelled = getattr(accumulator, '_cancel_event', None)
                    keep_owner = thread is not None and not (cancelled and cancelled.is_set())
                    if keep_owner and thread.ident is None:
                        if queue_owner is not None and queue_owner.snapshot()['depth'] and not _shutdown_started:
                            try:
                                accumulator.start()
                            except Exception as diagnostic_error:
                                logger.debug('_submit_keyed failure handled: %s', type(diagnostic_error).__name__)
                                keep_owner = False
                        else:
                            keep_owner = False
                    if not keep_owner:
                        if queue_owner is not None:
                            queue_owner.close()
                        if _accumulators.get(task_id) is accumulator:
                            _accumulators.pop(task_id, None)
                        task.pop('_incremental_translate_active', None)
                raise
    except Exception as exc:
        logger.warning("[IncTranslate] submit failed: %s", exc)
        return False


def submit_round_segment(task, round_num, text) -> bool:
    """Queue one closed narration round; return whether translation owns it."""
    try:
        normalized_round = int(round_num)
    except (TypeError, ValueError):
        return False
    key = _model_batch_segment_key(task, 'text', normalized_round)
    return _submit_keyed(task, key, text)


def submit_thinking_segment(task, block_id, text) -> bool:
    """Queue one closed reasoning block, keyed by its segment blockId.

    Reasoning shares its llmRound with the round's narration prose, so it is
    keyed by the collision-free segment blockId — the same key
    ``commit._stamp_segment_translations`` resolves when pinning. Oversize
    reasoning defers to the retro/on-open path: stamping is enrich-only, so
    pinning a truncated translation would freeze it permanently.
    """
    key = str(block_id or '').strip()
    if not key:
        return False
    # Compatibility call sites historically passed ``thinking:llm-N``. Scope
    # that key at admission so it matches modern final segment assembly.
    if key.startswith('thinking:llm-'):
        try:
            round_number = int(key.removeprefix('thinking:llm-'))
        except ValueError:
            pass
        else:
            key = _model_batch_segment_key(task, 'thinking', round_number)
    return _submit_keyed(task, key, text)


def _stamp_retired_cache(task) -> bool:
    """Own synchronous cache persistence until shutdown has joined it."""
    thread_id = threading.get_ident()
    with _accumulators_lock:
        if _shutdown_started:
            return False
        _retired_stamps[thread_id] = _retired_stamps.get(thread_id, 0) + 1
    try:
        return _stamp_retired_cache_owned(task)
    finally:
        with _shutdown_condition:
            remaining = _retired_stamps[thread_id] - 1
            if remaining:
                _retired_stamps[thread_id] = remaining
            else:
                del _retired_stamps[thread_id]
            _shutdown_condition.notify_all()


def _stamp_retired_cache_owned(task) -> bool:
    """Consume completed cache after a worker retired, without provider work."""
    if not _task_preview_state(task).get('translations'):
        _clear_task_preview_state(task)
        return False
    try:
        committed = _Accumulator(task)._commit_segments_only()
    except Exception as exc:
        logger.warning('[IncTranslate] retired cache handoff failed: %s', exc)
        return False
    _clear_task_preview_state(task)
    return committed


def finalize_incremental(task, content, **_ignored) -> bool:
    """Stamp completed segment overlays after the Turn projection settles."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        if isinstance(task, dict):
            task['_incremental_translate_terminal_requested'] = True
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        return _stamp_retired_cache(task)
    accepted = accumulator.finalize(str(content or ""))
    if accepted:
        _clear_task_preview_state(task)
        return True
    return _stamp_retired_cache(task)


def finalize_incremental_stamp_only(task, **_ignored) -> bool:
    """Persist cached narration/reasoning independently of the deliverable."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        if isinstance(task, dict):
            task['_incremental_translate_terminal_requested'] = True
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        return _stamp_retired_cache(task)
    accepted = accumulator.stamp_only()
    if accepted:
        _clear_task_preview_state(task)
        return True
    return _stamp_retired_cache(task)


def cancel_incremental(task) -> bool:
    """Release an accumulator when its turn cannot reach translation finalize."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        if isinstance(task, dict):
            task['_incremental_translate_terminal_requested'] = True
        accumulator = _accumulators.get(task_id)
    accepted = accumulator.cancel() if accumulator is not None else False
    _clear_task_preview_state(task, cancelled=True)
    return accepted


def shutdown_incremental_translations() -> None:
    """Withdraw new work, cancel live owners and physically join every worker.

    Weak thread handles cover the gap between accumulator deregistration and
    actual thread exit without retaining task/cache payloads after retirement.
    Synchronous retired-cache handoffs carry per-thread invocation counts and
    drain alongside workers. Dependencies remain available until this returns.
    A callback cannot close its own owner; repeated external close joins the same work.
    """
    global _shutdown_started
    with _accumulators_lock:
        threads = tuple(_worker_threads)
        if threading.current_thread() in threads or threading.get_ident() in _retired_stamps:
            raise RuntimeError('incremental translation callback cannot join its own owner')
        _shutdown_started = True
        accumulators = tuple(_accumulators.values())
    for accumulator in accumulators:
        accumulator.cancel()
    for thread in threads:
        thread.join()
    with _shutdown_condition:
        _shutdown_condition.wait_for(lambda: not _retired_stamps)
        for thread in threads:
            _worker_threads.discard(thread)


def preview_translations_for_conversation(
    conversation_id: str, *, user_id: int,
) -> list[dict[str, Any]]:
    """Return completed previews of the owner's live turns in one conversation.

    Previews are push-only while their Turn runs and each block's final
    preview is published once, so a browser that reloads, reconnects or
    missed frames re-reads this task-local cache. The result is presentation
    data: it reads no Turn and stamps nothing.
    """
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    previews = []
    for task in chat_task_runtime.snapshot_owned(user_id=user_id):
        if (str(task.get('convId') or '') != conversation_id
                or task.get('status') not in ('pending', 'running')
                or task.get('aborted') or not task.get('_turnId')):
            continue
        translations = _completed_preview_translations(task)
        if translations:
            previews.append({
                'turnId': str(task['_turnId']),
                'partialByRound': translations,
            })
    return previews


def _completed_preview_translations(task: dict[str, Any]) -> dict[str, str]:
    state = _task_preview_state(task)
    lock = state.get('cacheLock')
    if lock is None:
        return {}
    with lock:
        translations = state.get('translations')
        if state.get('cancelled') or not translations:
            return {}
        return {str(key): translations[key] for key in list(translations)}


__all__ = [
    "preview_translations_for_conversation",
    "submit_round_segment",
    "submit_thinking_segment",
    "finalize_incremental",
    "finalize_incremental_stamp_only",
    "cancel_incremental",
    "shutdown_incremental_translations",
]

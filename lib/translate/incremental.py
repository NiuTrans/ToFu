"""Incrementally translate narration while an authoritative turn runs.

One accumulator belongs to one executor task. During generation it translates
closed narration/reasoning rounds without count/character admission gates and
emits live previews.
Task-local preview policy and completed segment cache survive idle thread
retirement without retaining a full task or an idle worker. Writes still use
settled Turn authority; running Turns defer their cache to terminal handoff.

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

import os
import queue
import re as re
import threading
import weakref
from typing import Any

from lib.log import get_logger
from lib.translate._operation_buffer import IncrementalOperationBuffer
from lib.translate._completed_cache import CompletedTranslationCache
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)
_SOURCE = "English"
_STOP = object()
_PREVIEW_DEADLINE_SECONDS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS',
    minimum=5,
    maximum=300,
)
_PREVIEW_MIN_NARRATION_CHARS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS',
    minimum=1,
    maximum=4096,
)
_PREVIEW_MAX_429_ATTEMPTS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS',
    minimum=1,
    maximum=8,
)
_TASK_PREVIEW_STATE = '_incremental_translate_preview_state'
_PREVIEW_STATE_DISABLED = 'disabled'
_PREVIEW_STATE_STARTED = 'started'


_accumulators: dict[str, "_Accumulator"] = {}
_accumulators_lock = threading.RLock()
_worker_threads: weakref.WeakSet[threading.Thread] = weakref.WeakSet()
_shutdown_started = False
_retired_stamps: dict[int, int] = {}
_shutdown_condition = threading.Condition(_accumulators_lock)


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


def _clear_task_preview_state(
    task: dict[str, Any] | None, *, cancelled: bool = False,
) -> None:
    if not isinstance(task, dict):
        return
    state = task.pop(_TASK_PREVIEW_STATE, None)
    if cancelled and isinstance(state, dict):
        # A retiring worker holds the small state, never the full task. Stop
        # invalidates that detached reference even after queue admission closed.
        lock = state.get('cacheLock')
        if lock is not None:
            with lock:
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
        self._preview_disabled = bool(
            self._preview_state.get(_PREVIEW_STATE_DISABLED))
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
        dropped = self._queue.put_segment(key, str(text or ''))
        return dropped >= 0

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
        if self._preview_disabled and not terminal_reasoning:
            return
        narration = isinstance(key, int) or str(key).startswith('text:')
        if (not terminal_reasoning and narration
                and len(str(original or '').strip())
                < _PREVIEW_MIN_NARRATION_CHARS):
            logger.debug(
                '[IncTranslate] task=%s skipped short narration preview '
                'chars=%d floor=%d; terminal translation remains enabled',
                self.task_id[:8], len(str(original or '').strip()),
                _PREVIEW_MIN_NARRATION_CHARS,
            )
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
            # provider or malformed intermediate segment failed once.
            if not terminal_reasoning:
                self._preview_disabled = True
                self._preview_state[_PREVIEW_STATE_DISABLED] = True
            logger.warning(
                '[IncTranslate] task=%s %s segment %s failed; terminal '
                'translation remains enabled: %s',
                self.task_id[:8],
                ('terminal reasoning' if terminal_reasoning
                 else 'preview disabled after'),
                key, str(exc)[:300],
            )

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
            overall_deadline=_PREVIEW_DEADLINE_SECONDS,
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
        self._push({
            'type': 'running', 'status': 'running', 'statusKind': 'in_progress',
            'partialByRound': {str(key): partial},
        })

    def _push(self, payload: dict[str, Any]) -> None:
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
            }, user_id=self.user_id)
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
        if not terminal_reasoning and (
            preview_state.get(_PREVIEW_STATE_DISABLED)
        ):
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


__all__ = [
    "submit_round_segment",
    "submit_thinking_segment",
    "finalize_incremental",
    "finalize_incremental_stamp_only",
    "cancel_incremental",
    "shutdown_incremental_translations",
]

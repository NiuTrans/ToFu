"""Incrementally translate narration while an authoritative turn runs.

One accumulator belongs to one executor task. During generation it translates
a bounded number of closed narration/reasoning rounds and emits live previews.
Task-local admission state preserves that budget across idle thread retirement.
After the terminal turn event has committed, ``finalize_incremental``
prioritizes exactly one projection merge. No conversation transcript or
positional message identity participates.
"""

from __future__ import annotations

import os
import queue
import re
import threading
from typing import Any

from lib.log import get_logger
from lib.translate._operation_buffer import IncrementalOperationBuffer
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)
_SOURCE = "English"
_IDLE_SECONDS = 300.0
_STOP = object()
_SEGMENT_MAX_CHARS = 32_000
_MAX_ACTIVE_ACCUMULATORS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_ACTIVE', maximum=256)
_OPERATION_QUEUE_CAPACITY = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY', minimum=2, maximum=256)
_MAX_PREVIEW_SEGMENTS = resolve_resource_budget(
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS', minimum=1, maximum=1024)
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
_PREVIEW_STATE_LIMIT_REPORTED = 'limitReported'

_accumulators: dict[str, "_Accumulator"] = {}
_accumulators_lock = threading.Lock()


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


def _clear_task_preview_state(task: dict[str, Any] | None) -> None:
    if not isinstance(task, dict):
        return
    task.pop(_TASK_PREVIEW_STATE, None)


def _mixed_key_order(key: int | str) -> tuple[Any, ...]:
    """Deterministic order for legacy rounds and scoped segment block ids."""
    text = str(key)
    if text.isdigit():
        return (0, int(text), text)
    round_match = re.search(r':llm-(\d+)(?::|$)', text)
    if round_match:
        lane = 0 if text.startswith('text:') else 1
        return (lane, int(round_match.group(1)), text)
    return (2, text)


def _model_batch_segment_key(
    task: dict[str, Any], segment_type: str, round_num: int,
) -> str:
    """Match the exact block id final segment assembly will persist."""
    from lib.tool_round_identity import model_batch_segment_block_id

    return model_batch_segment_block_id(
        segment_type,
        round_num,
        attempt_id=task.get('_attemptId') or task.get('attemptId') or '',
        task_id=task.get('id') or task.get('taskId') or '',
    )


def _frame_payloads(translations: dict[int | str, str]) -> tuple[str, dict[str, str]]:
    """Joined narration preview + full per-round map for one push frame.

    The joined ``partial`` mirrors the narration lane only — reasoning
    translations (``thinking:``-keyed) ride ``partialByRound`` for per-block
    preview and terminal stamping but never dump into the bubble text
    (mirrors the retro worker's ``segment_progress`` in runtime/_worker.py).
    """
    by_round = {
        str(key): value
        for key, value in sorted(
            translations.items(), key=lambda item: _mixed_key_order(item[0]))
        if value.strip()
    }
    narration = [
        value for key, value in by_round.items()
        if not key.startswith('thinking:')
    ]
    return "\n\n".join(narration), by_round

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
        self.translations: dict[int | str, str] = {}
        self.originals: dict[int | str, str] = {}
        self.model = "unknown"
        self._preview_segments_started = _task_preview_started(task)
        self._preview_disabled = bool(
            self._preview_state.get(_PREVIEW_STATE_DISABLED))
        self._preview_limit_reported = bool(
            self._preview_state.get(_PREVIEW_STATE_LIMIT_REPORTED))
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._queue = IncrementalOperationBuffer(_OPERATION_QUEUE_CAPACITY)
        self._last_drop_reported = 0
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"turn-translate-{self.turn_id[:8]}",
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, key: int | str, text: str) -> bool:
        bounded_text = str(text or '')
        if len(bounded_text) > _SEGMENT_MAX_CHARS:
            bounded_text = bounded_text[:_SEGMENT_MAX_CHARS].rstrip() + '\n…'
        dropped = self._queue.put_segment(key, bounded_text)
        self._report_preview_pressure(dropped)
        return dropped >= 0

    def finalize(self, content: str) -> bool:
        # Pending previews are reconstructible enrichment. Once authority has
        # settled, the user-visible terminal translation owns the worker next.
        dropped = self._queue.put_terminal(
            ("finalize", content or ""),
            replace=True,
            preserve_segment_keys=frozenset({'thinking:terminal'}),
        )
        if dropped > 0:
            logger.debug(
                '[IncTranslate] task=%s terminal prioritized over %d '
                'pending preview segment(s)', self.task_id[:8], dropped)
        return dropped >= 0

    def stamp_only(self) -> bool:
        dropped = self._queue.put_terminal(
            ("stamp",),
            replace=True,
            preserve_segment_keys=frozenset({'thinking:terminal'}),
        )
        return dropped >= 0

    def cancel(self) -> bool:
        self._cancel_event.set()
        dropped = self._queue.put_terminal(_STOP, replace=True)
        return dropped >= 0

    def _report_preview_pressure(self, dropped: int) -> None:
        if dropped <= 0:
            return
        snapshot = self._queue.snapshot()
        total = int(snapshot['droppedSegments'])
        if total <= self._last_drop_reported:
            return
        self._last_drop_reported = total
        if total & (total - 1) == 0:
            logger.warning(
                '[IncTranslate] task=%s preview backlog saturated '
                'capacity=%d dropped_segments=%d; terminal handoff preserved',
                self.task_id[:8], snapshot['capacity'], total,
            )

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=_IDLE_SECONDS)
                except queue.Empty:
                    logger.warning(
                        "[IncTranslate] task=%s expired without terminal handoff",
                        self.task_id[:8],
                    )
                    return
                if item is _STOP:
                    return
                operation = item[0]
                if operation == "segment":
                    self._process_preview_segment(item[1], item[2])
                    continue
                if operation == "finalize":
                    self._commit_deliverable(item[1])
                    return
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
            if self._preview_segments_started >= _MAX_PREVIEW_SEGMENTS:
                if not self._preview_limit_reported:
                    self._preview_limit_reported = True
                    self._preview_state[
                        _PREVIEW_STATE_LIMIT_REPORTED] = True
                    logger.info(
                        '[IncTranslate] task=%s preview budget reached '
                        'segments=%d; terminal translation remains enabled',
                        self.task_id[:8], _MAX_PREVIEW_SEGMENTS,
                    )
                return
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
            self.originals[key] = original
            self.translations[key] = translated
            if model:
                self.model = model
        self._push_progress()

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

    def _translated_deliverable(self, content: str) -> str:
        normalized = "".join(content.split())
        with self._lock:
            for round_number, original in self.originals.items():
                if "".join(original.split()) == normalized:
                    cached = self.translations.get(round_number, "")
                    if cached.strip():
                        return cached
        translated, model = self._translate(
            content,
            progress_cb=lambda partial: self._push({
                "type": "running",
                "status": "running",
                "statusKind": "in_progress",
                "partial": partial,
            }),
        )
        if model:
            self.model = model
        return translated

    def _commit_deliverable(self, content: str) -> None:
        if not content.strip():
            return
        translated = self._translated_deliverable(content)
        with self._lock:
            by_round = dict(self.translations)
        from lib.translate.commit import commit_translation_to_turn

        commit_translation_to_turn(
            self.conversation_id,
            self.turn_id,
            "translatedContent",
            translated,
            user_id=self.user_id,
            model=self.model,
            segment_translations=by_round,
        )
        self._push({
            "type": "done",
            "status": "done",
            "translated": translated,
            "model": self.model,
            "segmentsByRound": {
                str(number): value for number, value in by_round.items()
            },
        })

    def _commit_segments_only(self) -> None:
        with self._lock:
            by_round = dict(self.translations)
        if not by_round:
            return
        from lib.translate.commit import commit_translation_to_turn

        commit_translation_to_turn(
            self.conversation_id,
            self.turn_id,
            None,
            "",
            user_id=self.user_id,
            segment_translations=by_round,
        )
        self._push({
            "type": "done",
            "status": "done",
            "segmentsByRound": {
                str(number): value for number, value in by_round.items()
            },
        })

    def _push_progress(self) -> None:
        with self._lock:
            partial, by_round = _frame_payloads(self.translations)
        if not by_round:
            return
        payload: dict[str, Any] = {
            "type": "running",
            "status": "running",
            "statusKind": "in_progress",
            "partialByRound": by_round,
        }
        if partial:
            payload["partial"] = partial
        self._push(payload)

    def _push_segment_progress(self, key: int | str, partial: str) -> None:
        partial = str(partial or '')
        if not partial.strip():
            return
        with self._lock:
            combined = dict(self.translations)
        combined[key] = partial
        narration_partial, by_round = _frame_payloads(combined)
        payload: dict[str, Any] = {
            "type": "running",
            "status": "running",
            "statusKind": "in_progress",
            "partialByRound": by_round,
        }
        if narration_partial:
            payload["partial"] = narration_partial
        self._push(payload)

    def _push(self, payload: dict[str, Any]) -> None:
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
            or _task_preview_started(task) >= _MAX_PREVIEW_SEGMENTS
        ):
            return False
        task_id = str(task["id"])
        with _accumulators_lock:
            accumulator = _accumulators.get(task_id)
            if accumulator is None:
                if len(_accumulators) >= _MAX_ACTIVE_ACCUMULATORS:
                    logger.warning(
                        '[IncTranslate] active accumulator capacity reached '
                        'capacity=%d task=%s',
                        _MAX_ACTIVE_ACCUMULATORS, task_id[:8],
                    )
                    return False
                accumulator = _Accumulator(task)
                _accumulators[task_id] = accumulator
                task["_incremental_translate_active"] = True
                try:
                    accumulator.start()
                except Exception:
                    _accumulators.pop(task_id, None)
                    task.pop("_incremental_translate_active", None)
                    raise
        return accumulator.submit(key, str(text))
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
    if len(str(text or '')) > _SEGMENT_MAX_CHARS:
        logger.debug(
            "[IncTranslate] thinking %s exceeds %d chars; left to retro path",
            key, _SEGMENT_MAX_CHARS,
        )
        return False
    return _submit_keyed(task, key, text)


def finalize_incremental(task, content, **_ignored) -> bool:
    """Queue the terminal deliverable after its turn projection has settled."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        _clear_task_preview_state(task)
        return False
    accepted = accumulator.finalize(str(content or ""))
    if accepted:
        _clear_task_preview_state(task)
    return accepted


def finalize_incremental_stamp_only(task, **_ignored) -> bool:
    """Persist cached narration when the deliverable needs no translation."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        _clear_task_preview_state(task)
        return False
    accepted = accumulator.stamp_only()
    if accepted:
        _clear_task_preview_state(task)
    return accepted


def cancel_incremental(task) -> bool:
    """Release an accumulator when its turn cannot reach translation finalize."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        _clear_task_preview_state(task)
        return False
    accepted = accumulator.cancel()
    if accepted:
        _clear_task_preview_state(task)
    return accepted


__all__ = [
    "submit_round_segment",
    "submit_thinking_segment",
    "finalize_incremental",
    "finalize_incremental_stamp_only",
    "cancel_incremental",
]

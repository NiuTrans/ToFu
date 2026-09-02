"""Incrementally translate narration while an authoritative turn runs.

One accumulator belongs to one executor task. During generation it translates
closed narration rounds and emits live previews; after the terminal turn event
has committed, ``finalize_incremental`` queues exactly one projection merge.
No conversation transcript or positional message identity participates.
"""

from __future__ import annotations

import os
import queue
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


def _mixed_key_order(key: int | str) -> tuple[int, Any]:
    """Deterministic order for the mixed int-round / thinking-blockId map:
    narration rounds numerically first, ``thinking:`` blockIds after."""
    text = str(key)
    return (0, int(text)) if text.isdigit() else (1, text)


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

        self.task_id = str(task["id"])
        self.conversation_id = str(task["convId"])
        self.turn_id = str(task["_turnId"])
        self.message_id = str(task.get("_assistantMsgId") or "")
        self.user_id = _owner_id(task)
        self.target = resolve_translate_target(task.get("config") or {})
        self.target_code = target_lang_code(self.target)
        # Keys: int llmRound for narration prose; the segment blockId
        # (``thinking:llm-N`` / ``thinking:terminal``) for reasoning, which
        # shares its round number with the round's narration.
        self.translations: dict[int | str, str] = {}
        self.originals: dict[int | str, str] = {}
        self.model = "unknown"
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
        dropped = self._queue.put_terminal(("finalize", content or ""))
        self._report_preview_pressure(dropped)
        return dropped >= 0

    def stamp_only(self) -> bool:
        dropped = self._queue.put_terminal(("stamp",))
        self._report_preview_pressure(dropped)
        return dropped >= 0

    def cancel(self) -> bool:
        dropped = self._queue.put_terminal(_STOP, replace=True)
        self._report_preview_pressure(dropped)
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
                    self._translate_segment(item[1], item[2])
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

    def _translate_segment(self, key: int | str, original: str) -> None:
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
    ) -> tuple[str, str]:
        from lib.text_lang import detect_language

        if detect_language(original, force_fasttext=True).code == self.target_code:
            return original, "skipped"

        from lib.translate.engine import _translate_freetext
        from lib.translate.notranslate import (
            _extract_notranslate_blocks,
            _reattach_notranslate_blocks,
            _reattach_notranslate_blocks_partial,
        )
        from lib.translate.prompt import _build_translate_prompt

        body, protected = _extract_notranslate_blocks(original)
        if not body.strip():
            return original, "skipped"
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
        key = int(round_num)
    except (TypeError, ValueError):
        return False
    return _submit_keyed(task, key, text)


def submit_thinking_segment(task, block_id, text) -> bool:
    """Queue one closed reasoning block, keyed by its segment blockId.

    Reasoning shares its llmRound with the round's narration prose, so it is
    keyed by the collision-free segment blockId (``thinking:llm-N`` /
    ``thinking:terminal``) — the same key
    ``commit._stamp_segment_translations`` resolves when pinning. Oversize
    reasoning defers to the retro/on-open path: stamping is enrich-only, so
    pinning a truncated translation would freeze it permanently.
    """
    key = str(block_id or '').strip()
    if not key:
        return False
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
        return False
    return accumulator.finalize(str(content or ""))


def finalize_incremental_stamp_only(task, **_ignored) -> bool:
    """Persist cached narration when the deliverable needs no translation."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        return False
    return accumulator.stamp_only()


def cancel_incremental(task) -> bool:
    """Release an accumulator when its turn cannot reach translation finalize."""
    task_id = str((task or {}).get("id") or "")
    with _accumulators_lock:
        accumulator = _accumulators.get(task_id)
    if accumulator is None:
        return False
    return accumulator.cancel()


__all__ = [
    "submit_round_segment",
    "submit_thinking_segment",
    "finalize_incremental",
    "finalize_incremental_stamp_only",
    "cancel_incremental",
]

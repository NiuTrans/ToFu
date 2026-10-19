"""Commit translation overlays to the authoritative turn projection.

Responsibility: merge a completed translation into one owner-scoped turn and
publish the resulting conversation revision.  Callers address a turn by its
stable ``turn_id``; message-array positions, content matching, transcript
blobs, and process-local serialization are deliberately outside this module.

The turn projection revision is the concurrency boundary.  A stale writer
re-reads the latest projection and reapplies only its translation-owned fields,
so unrelated terminal metadata and sibling enrichment cannot be clobbered.
"""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any, Mapping

from lib.log import get_logger

from lib.llm.diagnostic_timing import timed_call, timed_stage
from lib.storage.errors import StorageError
from lib.turn_lifecycle import (
    LifecycleConflict,
    get_turn,
    update_turn_projection,
)


logger = get_logger(__name__)
_MAX_PROJECTION_ATTEMPTS = 6
_WRITABLE_FIELDS = frozenset({"content", "translatedContent"})

_RETRYABLE_STORAGE_CODES = frozenset({
    "database_timeout", "database_busy", "database_unavailable",
})
# Writer acquisition is deadline-bounded (~2s); bursts of turn.event.record
# commits can starve a translation commit for several seconds, so retries
# back off far past the burst window before surfacing the failure.
_MAX_STORAGE_ATTEMPTS = 6
_STORAGE_RETRY_INITIAL_DELAY_S = 0.5
_STORAGE_RETRY_MAX_DELAY_S = 8.0


def _is_retryable_storage_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, StorageError)
        and exc.retryable
        and exc.code in _RETRYABLE_STORAGE_CODES
    )


def _with_storage_retry(operation, *, description: str):
    delay = _STORAGE_RETRY_INITIAL_DELAY_S
    for attempt in range(_MAX_STORAGE_ATTEMPTS):
        try:
            return timed_call('storage_attempt', operation)
        except StorageError as exc:
            if (not _is_retryable_storage_error(exc)
                    or attempt + 1 >= _MAX_STORAGE_ATTEMPTS):
                raise
            logger.warning(
                "[Translate] %s storage attempt %d/%d failed (%s: %s); "
                "retrying in %.1fs",
                description, attempt + 1, _MAX_STORAGE_ATTEMPTS,
                exc.code, exc, delay,
            )
            timed_call('storage_backoff', time.sleep, delay)
            delay = min(delay * 2, _STORAGE_RETRY_MAX_DELAY_S)
    raise AssertionError("storage retry loop must return or raise")


def _stamp_segment_translations(
    projection: dict[str, Any],
    translations_by_round: Mapping[Any, Any] | None,
    *,
    segment_sources: Mapping[Any, Any] | None = None,
) -> int:
    """Stamp translated narration / reasoning on matching segments.

    Modern producers address every segment by stable ``blockId``. Integer
    ``llmRound`` keys remain a read-only compatibility fallback for persisted
    translation work created before attempt-scoped block identities shipped.
    """
    if not translations_by_round:
        return 0
    segments = projection.get("segments")
    if not isinstance(segments, list):
        return 0

    stamped = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        translated: Any = None
        source_key: Any = segment.get("blockId")
        if segment_type == "thinking":
            block_id = segment.get("blockId")
            if block_id:
                translated = translations_by_round.get(block_id)
        elif segment_type == "text" and not segment.get("deliverable"):
            block_id = segment.get("blockId")
            if block_id:
                translated = translations_by_round.get(block_id)
            if translated is None:
                round_number = segment.get("llmRound")
                if round_number is not None:
                    source_key = round_number
                    translated = translations_by_round.get(round_number)
                    if translated is None:
                        source_key = str(round_number)
                        translated = translations_by_round.get(source_key)
        else:
            continue
        if not isinstance(translated, str) or not translated.strip():
            continue
        if segment_sources is not None:
            source = segment_sources.get(source_key)
            if (not isinstance(source, str)
                    or str(segment.get("text") or "").strip() != source.strip()):
                # Stable IDs fence Continue rounds; source equality also fences
                # edited/replaced terminal blocks which reuse a terminal ID.
                continue
        if segment.get("translatedText") == translated:
            continue
        segment["translatedText"] = translated
        stamped += 1
    return stamped


def _merge_translation(
    projection: Mapping[str, Any],
    *,
    field: str | None,
    translated_text: str,
    model: str | None,
    segment_translations: Mapping[Any, Any] | None,
    segment_sources: Mapping[Any, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return a copied projection with only translation-owned fields changed."""
    if field not in _WRITABLE_FIELDS and field is not None:
        raise ValueError(f"Unsupported translation field: {field}")

    before = dict(projection)
    updated = deepcopy(before)

    if field == "translatedContent":
        updated["translatedContent"] = translated_text
        updated["_showingTranslation"] = True
        updated["_translateDone"] = True
        if model:
            updated["_translateModel"] = model
    elif field == "content":
        updated.setdefault("originalContent", updated.get("content", ""))
        updated["content"] = translated_text
        updated["_translateDone"] = True
        if model:
            updated["_translateModel"] = model
    if field in ("translatedContent", "content"):
        # A successful whole-turn commit supersedes any earlier failure mark.
        updated.pop("_translateFailed", None)

    stamped = _stamp_segment_translations(
        updated, segment_translations, segment_sources=segment_sources)
    if stamped:
        logger.debug("[Translate] stamped %d narration segment(s)", stamped)
    return updated, updated != before


@timed_stage('storage_commit')
def commit_translation_to_turn(
    conversation_id: str,
    turn_id: str,
    field: str | None,
    translated_text: str,
    *,
    user_id: Any,
    model: str | None = None,
    segment_translations: Mapping[Any, Any] | None = None,
    segment_sources: Mapping[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """CAS-merge a translation into one settled authoritative turn.

    ``field=None`` enriches narration/reasoning without a whole-turn verdict.
    ``segment_sources`` fences cached overlays against edited source text.
    Missing identity is a programming error: silently falling back to an
    array index would recreate the dual-authority race this boundary removes.
    """
    if not conversation_id:
        raise ValueError("conversation_id is required")
    if not turn_id:
        raise ValueError("turn_id is required")
    if user_id in (None, ""):
        raise ValueError("user_id is required")
    if field is None and not segment_translations:
        return None

    def _attempt_commit() -> dict[str, Any]:
        latest_conflict: LifecycleConflict | None = None
        for attempt in range(_MAX_PROJECTION_ATTEMPTS):
            turn = get_turn(conversation_id, turn_id, user_id=user_id)
            if turn.get('status') in {'pending', 'running', 'waiting_for_user'}:
                raise LifecycleConflict('turn_in_progress',
                                        'Translation overlays require a settled Turn')
            projection, changed = _merge_translation(
                turn.get("projection") or {},
                field=field,
                translated_text=translated_text,
                model=model,
                segment_translations=segment_translations,
                segment_sources=segment_sources,
            )
            if not changed:
                return {"turn": turn, "conversationRevision": None}
            try:
                result = update_turn_projection(
                    conversation_id,
                    turn_id,
                    projection=projection,
                    expected_projection_revision=turn["projectionRevision"],
                    user_id=user_id,
                    preserve_activity=True,
                )
            except LifecycleConflict as exc:
                if exc.code != "stale_projection":
                    raise
                latest_conflict = exc
                if attempt + 1 < _MAX_PROJECTION_ATTEMPTS:
                    timed_call('storage_backoff', time.sleep, 0.02 * (attempt + 1))
                continue

            revision = result.get("conversationRevision")
            try:
                from lib.conversations import notify_conv_changed

                notify_conv_changed(
                    conversation_id,
                    rev=revision,
                    user_id=user_id,
                )
            except Exception as exc:  # persistence succeeded; notification is repairable
                logger.debug(
                    "[Translate] invalidation publish failed conv=%s turn=%s: %s",
                    conversation_id[:8],
                    turn_id[:8],
                    exc,
                )
            logger.info(
                "[Translate] committed field=%s conv=%s turn=%s revision=%s",
                field or "segments",
                conversation_id[:8],
                turn_id[:8],
                revision,
            )
            return result

        assert latest_conflict is not None
        raise latest_conflict

    return _with_storage_retry(_attempt_commit, description="translation commit")


def mark_turn_translation_failed(
    conversation_id: str,
    turn_id: str,
    *,
    user_id: Any,
    reason: str,
    error: str,
) -> dict[str, Any] | None:
    """Persist the terminal failure verdict so the turn is re-sweepable.

    A terminal auto-translate that dies (provider stream premature close,
    engine exhausted, restart) previously vanished without a trace: the turn
    kept its content, carried no ``_translateDone`` and no failure marker, so
    no later pass could distinguish 'never attempted' from 'attempted and
    failed' — the reported 'translations were never triggered' bug. Writing
    ``_translateFailed`` converts the in-memory, thread-local failure into
    durable, inspectable state. The marker is cleared by the next successful
    commit (``_merge_translation`` pops it) and never blocks a retry: the
    terminal coordinator and the boot sweep treat it as eligible work.
    """
    def _attempt_mark() -> dict[str, Any]:
        latest_conflict: LifecycleConflict | None = None
        for attempt in range(_MAX_PROJECTION_ATTEMPTS):
            turn = get_turn(conversation_id, turn_id, user_id=user_id)
            projection = deepcopy(dict(turn.get("projection") or {}))
            # A committed/skip verdict always wins over a late failure mark
            # from a superseded attempt — never regress a finished turn.
            if projection.get("_translateDone") is True:
                return {"turn": turn, "conversationRevision": None}
            projection["_translateFailed"] = {
                "reason": str(reason or "unknown")[:120],
                "error": str(error or "")[:300],
                "at": int(time.time()),
            }
            try:
                result = update_turn_projection(
                    conversation_id,
                    turn_id,
                    projection=projection,
                    expected_projection_revision=turn["projectionRevision"],
                    user_id=user_id,
                    preserve_activity=True,
                )
            except LifecycleConflict as exc:
                if exc.code != "stale_projection":
                    raise
                latest_conflict = exc
                if attempt + 1 < _MAX_PROJECTION_ATTEMPTS:
                    timed_call('storage_backoff', time.sleep, 0.02 * (attempt + 1))
                continue
            try:
                from lib.conversations import notify_conv_changed

                notify_conv_changed(
                    conversation_id,
                    rev=result.get("conversationRevision"),
                    user_id=user_id,
                )
            except Exception as exc:
                logger.debug(
                    "[Translate] failure-mark invalidation failed conv=%s turn=%s: %s",
                    conversation_id[:8], turn_id[:8], exc,
                )
            logger.warning(
                "[Translate] recorded failure verdict conv=%s turn=%s reason=%s",
                conversation_id[:8], turn_id[:8], reason,
            )
            return result
        assert latest_conflict is not None
        raise latest_conflict

    return _with_storage_retry(_attempt_mark, description="translation failure mark")

def mark_turn_translation_complete(
    conversation_id: str,
    turn_id: str,
    *,
    user_id: Any,
) -> dict[str, Any] | None:
    """Persist the terminal no-output verdict for already-target content."""
    def _attempt_mark() -> dict[str, Any]:
        latest_conflict: LifecycleConflict | None = None
        for attempt in range(_MAX_PROJECTION_ATTEMPTS):
            turn = get_turn(conversation_id, turn_id, user_id=user_id)
            projection = deepcopy(dict(turn.get("projection") or {}))
            if (projection.get("_translateDone") is True
                    and projection.get("_translateSkippedReason")
                    == "already_target_language"):
                return {"turn": turn, "conversationRevision": None}
            projection["_translateDone"] = True
            projection["_translateSkippedReason"] = "already_target_language"
            projection.pop("translatedContent", None)
            projection.pop("_showingTranslation", None)
            try:
                result = update_turn_projection(
                    conversation_id,
                    turn_id,
                    projection=projection,
                    expected_projection_revision=turn["projectionRevision"],
                    user_id=user_id,
                    preserve_activity=True,
                )
            except LifecycleConflict as exc:
                if exc.code != "stale_projection":
                    raise
                latest_conflict = exc
                if attempt + 1 < _MAX_PROJECTION_ATTEMPTS:
                    timed_call('storage_backoff', time.sleep, 0.02 * (attempt + 1))
                continue
            try:
                from lib.conversations import notify_conv_changed

                notify_conv_changed(
                    conversation_id,
                    rev=result.get("conversationRevision"),
                    user_id=user_id,
                )
            except Exception as exc:
                logger.debug(
                    "[Translate] no-op invalidation failed conv=%s turn=%s: %s",
                    conversation_id[:8], turn_id[:8], exc,
                )
            return result
        assert latest_conflict is not None
        raise latest_conflict

    return _with_storage_retry(_attempt_mark, description="translation mark")


__all__ = [
    "commit_translation_to_turn",
    "mark_turn_translation_complete",
    "mark_turn_translation_failed",
    "_is_retryable_storage_error",
    "_merge_translation",
    "_stamp_segment_translations",
]

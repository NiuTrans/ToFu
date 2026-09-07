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
from lib.turn_lifecycle import (
    LifecycleConflict,
    get_turn,
    update_turn_projection,
)


logger = get_logger(__name__)
_MAX_PROJECTION_ATTEMPTS = 6
_WRITABLE_FIELDS = frozenset({"content", "translatedContent"})


def _stamp_segment_translations(
    projection: dict[str, Any],
    translations_by_round: Mapping[Any, Any] | None,
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
                    translated = translations_by_round.get(round_number)
                    if translated is None:
                        translated = translations_by_round.get(str(round_number))
        else:
            continue
        if not isinstance(translated, str) or not translated.strip():
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
) -> tuple[dict[str, Any], bool]:
    """Return a copied projection with only translation-owned fields changed."""
    if field not in _WRITABLE_FIELDS and field is not None:
        raise ValueError(f"Unsupported translation field: {field}")

    updated = deepcopy(dict(projection))
    before = deepcopy(updated)

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

    stamped = _stamp_segment_translations(updated, segment_translations)
    if stamped:
        logger.debug("[Translate] stamped %d narration segment(s)", stamped)
    return updated, updated != before


def commit_translation_to_turn(
    conversation_id: str,
    turn_id: str,
    field: str | None,
    translated_text: str,
    *,
    user_id: Any,
    model: str | None = None,
    segment_translations: Mapping[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """CAS-merge a translation into one settled authoritative turn.

    ``field=None`` is a narration-only enrichment.  Missing identity is a
    programming error: silently falling back to an array index would recreate
    the dual-authority race this boundary exists to remove.
    """
    if not conversation_id:
        raise ValueError("conversation_id is required")
    if not turn_id:
        raise ValueError("turn_id is required")
    if user_id in (None, ""):
        raise ValueError("user_id is required")
    if field is None and not segment_translations:
        return None

    latest_conflict: LifecycleConflict | None = None
    for attempt in range(_MAX_PROJECTION_ATTEMPTS):
        turn = get_turn(conversation_id, turn_id, user_id=user_id)
        projection, changed = _merge_translation(
            turn.get("projection") or {},
            field=field,
            translated_text=translated_text,
            model=model,
            segment_translations=segment_translations,
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
            )
        except LifecycleConflict as exc:
            if exc.code != "stale_projection":
                raise
            latest_conflict = exc
            if attempt + 1 < _MAX_PROJECTION_ATTEMPTS:
                time.sleep(0.02 * (attempt + 1))
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


def mark_turn_translation_complete(
    conversation_id: str,
    turn_id: str,
    *,
    user_id: Any,
) -> dict[str, Any] | None:
    """Persist the terminal no-output verdict for already-target content."""
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
            )
        except LifecycleConflict as exc:
            if exc.code != "stale_projection":
                raise
            latest_conflict = exc
            if attempt + 1 < _MAX_PROJECTION_ATTEMPTS:
                time.sleep(0.02 * (attempt + 1))
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


__all__ = [
    "commit_translation_to_turn",
    "mark_turn_translation_complete",
    "_merge_translation",
    "_stamp_segment_translations",
]

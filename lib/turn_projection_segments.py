"""Canonical stable content blocks for every public Turn projection.

Responsibility: derive or repair the ordered ``segments`` document from the
legacy content/thinking/tool projections at backend authority boundaries.
This module is pure: storage adapters decide when a normalized document is
persisted, while HTTP/application adapters use the same function for archived
rows that have not yet been rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.tasks_pkg.segments import (
    assemble_segments,
    is_synthetic_inbox_round,
    segments_to_json,
    tool_use_segment_from_round,
)
from lib.turn_projection_patch import normalize_projection_document


_SEGMENT_TYPES = frozenset({"text", "thinking", "tool_use"})
_RESUMABLE_TURN_STATUSES = frozenset({"interrupted", "truncated"})


def _valid_segments(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    segments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") not in _SEGMENT_TYPES:
            return None
        segment = dict(item)
        segment_type = segment["type"]
        if segment_type in {"text", "thinking"}:
            if not isinstance(segment.get("text"), str):
                return None
        elif not isinstance(segment.get("result"), Mapping):
            return None
        segments.append(segment)
    return segments


def _fallback_block_id(segment: Mapping[str, Any], position: int) -> str:
    segment_type = str(segment.get("type") or "text")
    if segment_type == "tool_use":
        call_id = str(segment.get("id") or "").strip()
        if call_id:
            return f"tool:{call_id}"
    if segment.get("terminal"):
        return f"{segment_type}:terminal"
    llm_round = segment.get("llmRound")
    if isinstance(llm_round, int) and not isinstance(llm_round, bool):
        return f"{segment_type}:llm-{llm_round}"
    return f"{segment_type}:legacy-{position}"


def _stable_block_ids(
    segments: list[dict[str, Any]],
    *,
    reserved_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    claimed: dict[str, int] = {
        block_id: 1 for block_id in (reserved_ids or set())
    }
    normalized: list[dict[str, Any]] = []
    for position, source in enumerate(segments):
        segment = dict(source)
        declared = segment.get("blockId")
        preferred = (
            declared.strip()
            if isinstance(declared, str) and declared.strip()
            else _fallback_block_id(segment, position)
        )
        occurrence = claimed.get(preferred, 0) + 1
        claimed[preferred] = occurrence
        segment["blockId"] = (
            preferred if occurrence == 1 else f"{preferred}~{occurrence}"
        )
        segment.pop("_round", None)
        normalized.append(segment)
    return normalized


def _segments_missing_tool_round(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> bool:
    """True when a live tool round has no ``tool_use`` segment yet.

    ``task['segments']`` is a CHECKPOINT-era assembly: rounds appended after
    the last checkpoint (≥5s cadence) are absent from it even though they are
    already in ``toolRounds``.  Ordinary rounds heal at the next checkpoint,
    but human-wait rounds (ask_human / write-approval / stdin) BLOCK the
    executor — no later checkpoint ever runs — so their interactive card
    (question + answer UI, rendered only from a segment's tool block) would
    never appear.  The authority boundary repairs the gap. Running turns
    append only the missing tool blocks after their current live prefix;
    settled turns can safely use finished-turn re-assembly.

    One-directional by design: segments may legitimately be a SUPERSET of
    ``toolRounds`` (manual compaction folds cold rounds out of the round list
    while their render blocks stay), so only a round MISSING from the segment
    ids triggers repair — never a count mismatch.  Synthetic inbox rows
    and rounds without a toolCallId never become segments, so they must not
    count as gaps (that would repair on every read, forever).
    """
    if not isinstance(tool_rounds, list) or not tool_rounds:
        return False
    covered = {
        str(segment.get("id") or "")
        for segment in segments
        if segment.get("type") == "tool_use"
    }
    for round_record in tool_rounds:
        if not isinstance(round_record, Mapping):
            continue
        if is_synthetic_inbox_round(round_record):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if call_id and call_id not in covered:
            return True
    return False


def _append_missing_live_tool_segments(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> list[dict[str, Any]]:
    """Append absent live tool blocks without reordering streamed segments."""
    if not isinstance(tool_rounds, list):
        return segments
    covered = {
        str(segment.get("id") or "")
        for segment in segments
        if segment.get("type") == "tool_use"
    }
    appended: list[dict[str, Any]] = []
    for position, source in enumerate(tool_rounds):
        if not isinstance(source, Mapping):
            continue
        round_record = dict(source)
        if is_synthetic_inbox_round(round_record):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if not call_id or call_id in covered:
            continue
        appended.append(tool_use_segment_from_round(round_record, position))
        covered.add(call_id)
    return [*segments, *segments_to_json(appended)]


def _normalize_injection_lane(records: Any, channel: str) -> Any:
    if not isinstance(records, list):
        return records
    claimed: dict[str, int] = {}
    normalized: list[Any] = []
    for item in records:
        if not isinstance(item, Mapping):
            normalized.append(item)
            continue
        record = dict(item)
        declared = record.get("blockId")
        round_value = record.get("round")
        round_token = (
            str(round_value)
            if isinstance(round_value, int) and not isinstance(round_value, bool)
            else "unknown"
        )
        preferred = (
            declared.strip()
            if isinstance(declared, str) and declared.strip()
            else f"injection:{channel}:round-{round_token}"
        )
        occurrence = claimed.get(preferred, 0) + 1
        claimed[preferred] = occurrence
        record["blockId"] = (
            preferred if occurrence == 1 else f"{preferred}~{occurrence}"
        )
        normalized.append(record)
    return normalized


def _normalize_sidecar_blocks(projection: dict[str, Any]) -> set[str]:
    reserved: set[str] = set()
    for field, fallback in (
        ("origin", "origin"),
        ("contextSnapshot", "turn-context"),
        ("compaction", "compaction"),
        ("imageGeneration", "image-generation"),
        ("proposedPlan", "proposed-plan"),
        ("planExecution", "plan-execution"),
        ("activityTimeline", "activity-timeline"),
    ):
        sidecar = projection.get(field)
        if not isinstance(sidecar, Mapping):
            continue
        normalized_sidecar = dict(sidecar)
        normalized_sidecar["blockId"] = str(
            normalized_sidecar.get("blockId") or fallback
        )
        projection[field] = normalized_sidecar
        reserved.add(normalized_sidecar["blockId"])
    if any(projection.get(field) for field in (
        "images", "videos", "pdfTexts", "convRefs", "replyQuotes"
    )):
        reserved.add("attachments")
    provenance = projection.get("provenance")
    if isinstance(provenance, Mapping):
        provenance_block = dict(provenance)
        provenance_block["blockId"] = str(
            provenance_block.get("blockId") or "provenance"
        )
        projection["provenance"] = provenance_block
        reserved.add(provenance_block["blockId"])
    for field, channel in (
        ("_inboxInjects", "inbox"),
        ("_peerInjects", "peer"),
        ("_userSteerInjects", "user-steer"),
        ("_stallNudges", "stall-nudge"),
    ):
        if projection.get(field) is None:
            continue
        records = _normalize_injection_lane(projection[field], channel)
        projection[field] = records
        reserved.update(
            str(item["blockId"])
            for item in records
            if isinstance(item, Mapping) and item.get("blockId")
        )
    raw_file_changes = projection.get("fileChanges")
    legacy_files = projection.get("modifiedFileList")
    if isinstance(raw_file_changes, Mapping):
        file_changes = dict(raw_file_changes)
    elif isinstance(legacy_files, list) and legacy_files:
        raw_count = projection.get("modifiedFiles")
        count = raw_count if isinstance(raw_count, int) else 0
        file_changes = {
            "blockId": "file-changes",
            "count": max(count, len(legacy_files)),
            "state": "applied",
            "files": [dict(item) if isinstance(item, Mapping) else item
                      for item in legacy_files],
        }
    else:
        file_changes = None
    if file_changes is not None:
        file_changes["blockId"] = str(
            file_changes.get("blockId") or "file-changes"
        )
        file_changes.setdefault("state", "applied")
        projection["fileChanges"] = file_changes
        reserved.add(file_changes["blockId"])
    return reserved


def _synchronize_terminal_blocks(
    segments: list[dict[str, Any]],
    projection: Mapping[str, Any],
    *,
    actor: str,
    status: str,
    ensure_assistant_text_placeholder: bool = True,
) -> None:
    content = str(projection.get("content") or "")
    thinking = str(projection.get("thinking") or "")
    terminal_text_index = next((
        index for index in range(len(segments) - 1, -1, -1)
        if segments[index].get("type") == "text"
        and (segments[index].get("terminal")
             or segments[index].get("deliverable"))
    ), None)
    if terminal_text_index is None and (
        content or (ensure_assistant_text_placeholder and actor != "human")
    ):
        segments.append({
            "type": "text",
            "blockId": "text:terminal",
            "text": content,
            "deliverable": True,
            "terminal": True,
        })
        terminal_text_index = len(segments) - 1
    if terminal_text_index is not None:
        terminal_text = segments[terminal_text_index]
        terminal_text["text"] = content
        terminal_text["deliverable"] = True
        terminal_text["terminal"] = True
        if status in _RESUMABLE_TURN_STATUSES:
            terminal_text["resumable"] = True
        else:
            terminal_text.pop("resumable", None)

    terminal_thinking_index = next((
        index for index in range(len(segments) - 1, -1, -1)
        if segments[index].get("type") == "thinking"
        and segments[index].get("terminal")
    ), None)
    if terminal_thinking_index is not None:
        segments[terminal_thinking_index]["text"] = thinking
    elif thinking:
        thinking_segment = {
            "type": "thinking",
            "blockId": "thinking:terminal",
            "text": thinking,
            "deliverable": False,
            "terminal": True,
        }
        if terminal_text_index is None:
            segments.append(thinking_segment)
        else:
            segments.insert(terminal_text_index, thinking_segment)


def projection_with_stable_segments(
    raw_projection: Any,
    *,
    actor: str = "assistant",
    status: str = "completed",
) -> dict[str, Any]:
    """Return one projection whose render blocks all have durable identity."""
    projection = normalize_projection_document(raw_projection)
    reserved_ids = _normalize_sidecar_blocks(projection)
    normalized_actor = str(actor or "assistant")
    normalized_status = str(status or "")
    segments = _valid_segments(projection.get("segments"))
    if segments is not None and _segments_missing_tool_round(
        segments, projection.get("toolRounds"),
    ):
        if normalized_status == "running":
            # A complete tool call can be announced before parsing attaches
            # the current thinking/text to its round. Materialize that live
            # prefix first, then append only the missing tools; finished-turn
            # assembly would incorrectly put those tools before the prefix.
            _synchronize_terminal_blocks(
                segments,
                projection,
                actor=normalized_actor,
                status=normalized_status,
                ensure_assistant_text_placeholder=False,
            )
            segments = _append_missing_live_tool_segments(
                segments, projection.get("toolRounds"),
            )
        else:
            segments = None
    if segments is None:
        task = {
            "content": str(projection.get("content") or ""),
            "thinking": str(projection.get("thinking") or ""),
        }
        assembled = assemble_segments(
            task,
            merged=(list(projection.get("toolRounds") or [])
                    if isinstance(projection.get("toolRounds"), list) else []),
        )
        segments = segments_to_json(assembled)
    _synchronize_terminal_blocks(
        segments,
        projection,
        actor=normalized_actor,
        status=normalized_status,
    )
    projection["segments"] = _stable_block_ids(
        segments, reserved_ids=reserved_ids,
    )
    return projection


def public_turn_with_stable_segments(raw_turn: Any) -> Any:
    """Normalize one TurnRecord-shaped mapping without mutating its source."""
    if not isinstance(raw_turn, Mapping) or not isinstance(
        raw_turn.get("projection"), Mapping
    ):
        return raw_turn
    turn = dict(raw_turn)
    turn["projection"] = projection_with_stable_segments(
        turn["projection"],
        actor=str(turn.get("actor") or "assistant"),
        status=str(turn.get("status") or "completed"),
    )
    return turn


def public_value_with_stable_segments(value: Any) -> Any:
    """Normalize every TurnRecord nested in a command/sync public document."""
    if isinstance(value, list):
        return [public_value_with_stable_segments(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    if ("turnId" in value and "projectionRevision" in value
            and isinstance(value.get("projection"), Mapping)):
        return public_turn_with_stable_segments(value)
    return {
        key: public_value_with_stable_segments(item)
        for key, item in value.items()
    }


__all__ = [
    "projection_with_stable_segments",
    "public_turn_with_stable_segments",
    "public_value_with_stable_segments",
]

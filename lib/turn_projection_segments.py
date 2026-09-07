"""Canonical stable content blocks for every public Turn projection.

Responsibility: derive or repair the ordered ``segments`` document from the
legacy content/thinking/tool projections at backend authority boundaries.
It also derives the request-local completed-turn browser view from the same
authority without mutating it. This module is pure: storage adapters decide
when a normalized document is persisted, while HTTP/application adapters use
the same functions for archived rows and snapshot representations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
from typing import Any
from urllib.parse import quote

import orjson

from lib.attachments import canonical_image_ref
from lib.tasks_pkg.segments import (
    assemble_segments,
    is_synthetic_inbox_round,
    segments_to_json,
    tool_use_segment_from_round,
)
from lib.swarm.presentation_budget import swarm_snapshot_for_browser
from lib.storage_projection import trim_tool_round_for_persist
from lib.tool_round_identity import execution_identity, execution_llm_round
from lib.turn_image_transport import (
    MAX_TURN_IMAGES,
    MIN_LAZY_TURN_IMAGE_ENCODED_CHARS,
    legacy_turn_image_payload,
)
from lib.turn_projection_patch import normalize_projection_document


_SEGMENT_TYPES = frozenset({"text", "thinking", "tool_use", "system_note"})
_RESUMABLE_TURN_STATUSES = frozenset({"interrupted", "truncated"})
_REFERENCEABLE_TURN_STATUS = "completed"
_BROWSER_TERMINAL_TURN_STATUSES = frozenset({
    "completed",
    "failed",
    "interrupted",
    "truncated",
})
_BROWSER_PRIVATE_TOOL_ROUND_FIELDS = frozenset({
    "_responsesItems",
    "_anthropicContentBlocks",
})
_BROWSER_API_ROUND_USAGE_FIELDS = frozenset({
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_creation_input_tokens",
    "cache_read_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
    "thinking_tokens",
    "total_tokens",
    "_dispatch",
    "_subscription_quota",
    "trace_id",
})
_BROWSER_API_ROUND_DISPATCH_FIELDS = frozenset({
    "key",
    "key_tail",
    "model",
    "provider_id",
})
_BROWSER_API_ROUND_COST_FIELDS = frozenset({"costCny"})
_SHARED_TOOL_DOCUMENT_FIELDS = ("toolContent", "results")
_SHARED_TOOL_DOCUMENT_MIN_BYTES = 1024
_SHARED_TOOL_DOCUMENT_MAX_DOCUMENTS = 256
_SHARED_TOOL_DOCUMENT_MAX_REFERENCES = 4096
_SNAPSHOT_DOCUMENT_REFS_FIELD = "_snapshotDocumentRefs"
_SNAPSHOT_PROJECTION_REFS_FIELD = "snapshotProjectionRefs"
_SNAPSHOT_CONTENT_MIN_BYTES = 128
_SNAPSHOT_REFERENCE_MIN_SAVINGS = 64
_SNAPSHOT_PROJECTION_MAX_REFERENCES = 4096


def _valid_segments(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    segments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") not in _SEGMENT_TYPES:
            return None
        segment = dict(item)
        segment_type = segment["type"]
        if segment_type in {"text", "thinking", "system_note"}:
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
        attempt_id, task_id = execution_identity(segment)
        scope = attempt_id or task_id
        prefix = f"attempt-{scope}:" if scope else ""
        return f"{segment_type}:{prefix}llm-{llm_round}"
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
    covered: dict[str, int] = {}
    for segment in segments:
        if segment.get("type") != "tool_use":
            continue
        call_id = str(segment.get("id") or "").strip()
        if call_id:
            covered[call_id] = covered.get(call_id, 0) + 1
    seen_rounds: dict[str, int] = {}
    for round_record in tool_rounds:
        if not isinstance(round_record, Mapping):
            continue
        if is_synthetic_inbox_round(round_record):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if not call_id:
            continue
        occurrence = seen_rounds.get(call_id, 0) + 1
        seen_rounds[call_id] = occurrence
        if occurrence > covered.get(call_id, 0):
            return True
    return False


def _append_missing_live_tool_segments(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> list[dict[str, Any]]:
    """Append absent live tool blocks without reordering streamed segments."""
    if not isinstance(tool_rounds, list):
        return segments
    covered: dict[str, int] = {}
    for segment in segments:
        if segment.get("type") != "tool_use":
            continue
        call_id = str(segment.get("id") or "").strip()
        if call_id:
            covered[call_id] = covered.get(call_id, 0) + 1
    seen_rounds: dict[str, int] = {}
    appended: list[dict[str, Any]] = []
    for position, source in enumerate(tool_rounds):
        if not isinstance(source, Mapping):
            continue
        round_record = dict(source)
        if is_synthetic_inbox_round(round_record):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if not call_id:
            continue
        occurrence = seen_rounds.get(call_id, 0) + 1
        seen_rounds[call_id] = occurrence
        if occurrence <= covered.get(call_id, 0):
            continue
        appended.append(tool_use_segment_from_round(round_record, position))
        covered[call_id] = covered.get(call_id, 0) + 1
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
        "images", "attachments", "videos", "pdfTexts", "convRefs",
        "replyQuotes"
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
        ("_bgCommandInjects", "background-command"),
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
    rolled_back = projection.get("rolledBack")
    if isinstance(rolled_back, list):
        claimed_rolled: dict[str, int] = {}
        normalized_rolled: list[dict[str, Any]] = []
        for item in rolled_back:
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            declared = record.get("blockId")
            preferred = (
                declared.strip()
                if isinstance(declared, str) and declared.strip()
                else "rolled-back"
            )
            occurrence = claimed_rolled.get(preferred, 0) + 1
            claimed_rolled[preferred] = occurrence
            record["blockId"] = (
                preferred if occurrence == 1 else f"{preferred}~{occurrence}"
            )
            normalized_rolled.append(record)
            reserved.add(record["blockId"])
        if normalized_rolled:
            projection["rolledBack"] = normalized_rolled
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


def _unique_rounds_by_call_id(tool_rounds: Any) -> dict[str, Mapping[str, Any]]:
    """Index only unambiguous real rounds for projection reconciliation."""
    if not isinstance(tool_rounds, list):
        return {}
    indexed: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for round_record in tool_rounds:
        if not isinstance(round_record, Mapping):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if not call_id:
            continue
        if call_id in indexed:
            duplicates.add(call_id)
            continue
        indexed[call_id] = round_record
    for call_id in duplicates:
        indexed.pop(call_id, None)
    return indexed


def _tool_segment_id_counts(segments: Any) -> Counter[str]:
    if not isinstance(segments, list):
        return Counter()
    return Counter(
        str(segment.get("id") or "").strip()
        for segment in segments
        if isinstance(segment, Mapping)
        and segment.get("type") == "tool_use"
        and str(segment.get("id") or "").strip()
    )


def _tool_segment_matches_round(
    segment: Mapping[str, Any], round_record: Mapping[str, Any],
) -> bool:
    """Fail closed unless two carriers describe one tool occurrence."""
    segment_id = str(segment.get("id") or "").strip()
    round_id = str(round_record.get("toolCallId") or "").strip()
    if not segment_id or segment_id != round_id:
        return False
    segment_name = str(segment.get("name") or "").strip()
    round_name = str(round_record.get("toolName") or "").strip()
    if segment_name and round_name and segment_name != round_name:
        return False
    if (
        ("input" in segment) != ("toolArgs" in round_record)
        or (
            "input" in segment
            and segment.get("input") != round_record.get("toolArgs")
        )
    ):
        return False
    segment_attempt, segment_task = execution_identity(segment)
    round_attempt, round_task = execution_identity(round_record)
    if segment_attempt and round_attempt and segment_attempt != round_attempt:
        return False
    if segment_task and round_task and segment_task != round_task:
        return False
    segment_scope = segment_attempt or segment_task
    round_scope = round_attempt or round_task
    if segment_scope and round_scope and segment_scope != round_scope:
        return False
    segment_llm_round = execution_llm_round(segment)
    round_llm_round = execution_llm_round(round_record)
    return not (
        segment_llm_round is not None
        and round_llm_round is not None
        and segment_llm_round != round_llm_round
    )


def _round_has_explicit_payload_compaction(
    round_record: Mapping[str, Any],
) -> bool:
    return (
        round_record.get("compactionLayer") == "L1"
        or round_record.get("_persistCompacted") is True
    )


def _segment_result_from_round(round_record: Mapping[str, Any]) -> dict[str, Any]:
    """Mirror the generated browser's ``resultFromRound`` reconstruction."""
    source = round_record.get("result")
    result = dict(source) if isinstance(source, Mapping) else {}
    explicitly_compacted = _round_has_explicit_payload_compaction(round_record)
    has_content = isinstance(source, Mapping) and "content" in source
    has_status = isinstance(source, Mapping) and "status" in source
    if (explicitly_compacted or not has_content) and "toolContent" in round_record:
        result["content"] = round_record["toolContent"]
    if (explicitly_compacted or not has_status) and "status" in round_record:
        result["status"] = round_record["status"]
    return result


def _synchronize_compacted_tool_segment_results(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> list[dict[str, Any]]:
    """Prevent a durable compacted round's segment mirror restoring old bytes."""
    if not isinstance(tool_rounds, list) or not any(
        isinstance(round_record, Mapping)
        and _round_has_explicit_payload_compaction(round_record)
        for round_record in tool_rounds
    ):
        return segments
    rounds_by_id = _unique_rounds_by_call_id(tool_rounds)
    segment_id_counts = _tool_segment_id_counts(segments)
    if not rounds_by_id or not segment_id_counts:
        return segments
    changed = False
    synchronized: list[dict[str, Any]] = []
    for source in segments:
        call_id = (
            str(source.get("id") or "").strip()
            if source.get("type") == "tool_use"
            else ""
        )
        round_record = rounds_by_id.get(call_id)
        if (
            not call_id
            or segment_id_counts.get(call_id) != 1
            or round_record is None
            or not _round_has_explicit_payload_compaction(round_record)
            or not _tool_segment_matches_round(source, round_record)
        ):
            synchronized.append(source)
            continue
        round_result = _segment_result_from_round(round_record)
        if "content" not in round_result:
            synchronized.append(source)
            continue
        result = dict(source["result"])
        result["content"] = round_result["content"]
        if "status" in round_result:
            result["status"] = round_result["status"]
        if result == source["result"]:
            synchronized.append(source)
            continue
        segment = dict(source)
        segment["result"] = result
        synchronized.append(segment)
        changed = True
    return synchronized if changed else segments


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
    segments = _synchronize_compacted_tool_segment_results(
        segments, projection.get("toolRounds"),
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


def _tool_rounds_for_browser(
    tool_rounds: Any,
    *,
    omit_provider_replay: bool,
) -> Any:
    """Project closed-world tool-round presentation without changing storage.

    Completed views drop opaque provider replay bodies. Every terminal view
    also normalizes reconstructible historical Swarm tool timelines to their
    current resource budget. Nested values are shallow-shared unless changed.
    """
    if not isinstance(tool_rounds, list):
        return tool_rounds
    changed = False
    projected: list[Any] = []
    for source in tool_rounds:
        if not isinstance(source, Mapping):
            projected.append(source)
            continue
        browser_source = trim_tool_round_for_persist(source)
        has_private_replay = omit_provider_replay and any(
            field in browser_source
            for field in _BROWSER_PRIVATE_TOOL_ROUND_FIELDS
        )
        source_swarm_snapshot = browser_source.get("_swarmSnapshot")
        browser_swarm_snapshot = swarm_snapshot_for_browser(
            source_swarm_snapshot
        )
        if (
            browser_source is source
            and not has_private_replay
            and browser_swarm_snapshot is source_swarm_snapshot
        ):
            projected.append(source)
            continue
        round_record = {
            key: value for key, value in browser_source.items()
            if not (
                omit_provider_replay
                and key in _BROWSER_PRIVATE_TOOL_ROUND_FIELDS
            )
        }
        if browser_swarm_snapshot is not source_swarm_snapshot:
            round_record["_swarmSnapshot"] = browser_swarm_snapshot
        projected.append(round_record)
        changed = True
    return projected if changed else tool_rounds


def _api_round_usage_for_browser(usage: Any) -> Any:
    """Keep the closed-world cost/quota/context usage read model."""
    if not isinstance(usage, Mapping):
        return usage
    projected = {
        key: value for key, value in usage.items()
        if key in _BROWSER_API_ROUND_USAGE_FIELDS
    }
    dispatch = projected.get("_dispatch")
    if isinstance(dispatch, Mapping):
        browser_dispatch = {
            key: value for key, value in dispatch.items()
            if key in _BROWSER_API_ROUND_DISPATCH_FIELDS
        }
        if len(browser_dispatch) == len(dispatch) and all(
            browser_dispatch.get(key) is value
            for key, value in dispatch.items()
        ):
            browser_dispatch = dispatch
        projected["_dispatch"] = browser_dispatch
    if len(projected) == len(usage) and all(
        projected.get(key) is value for key, value in usage.items()
    ):
        return usage
    return projected


def _api_rounds_for_browser(api_rounds: Any) -> Any:
    """Keep only fields consumed by the completed-round browser read model."""
    if not isinstance(api_rounds, list):
        return api_rounds
    changed = False
    projected_rounds: list[Any] = []
    for source in api_rounds:
        if not isinstance(source, Mapping):
            projected_rounds.append(source)
            continue
        usage = source.get("usage")
        browser_usage = _api_round_usage_for_browser(usage)
        cost = source.get("cost")
        browser_cost = cost
        if isinstance(cost, Mapping):
            projected_cost = {
                key: value
                for key, value in cost.items()
                if key in _BROWSER_API_ROUND_COST_FIELDS
            }
            if len(projected_cost) == len(cost) and all(
                projected_cost.get(key) is value
                for key, value in cost.items()
            ):
                browser_cost = cost
            else:
                browser_cost = projected_cost or None
        if browser_usage is usage and browser_cost is cost:
            projected_rounds.append(source)
            continue
        round_record = dict(source)
        if browser_usage is not usage:
            round_record["usage"] = browser_usage
        if browser_cost is not cost:
            if browser_cost is None:
                round_record.pop("cost", None)
            else:
                round_record["cost"] = browser_cost
        projected_rounds.append(round_record)
        changed = True
    return projected_rounds if changed else api_rounds


def projection_with_reference_tool_segments(
    stable_projection: Any,
    *,
    status: str,
) -> Any:
    """Deduplicate settled tool blocks against their sibling round authority.

    The input must already have passed ``projection_with_stable_segments``.
    All terminal Turns may receive a bounded historical Swarm presentation.
    Only completed turns are immutable enough for segment references and
    provider-evidence omission. Running turns remain byte-identical, while
    resumable/failed turns retain complete segments and provider evidence.

    ``TurnToolUseSegment.result`` remains an object for backward-compatible
    generated types. ``roundRef`` makes the empty object explicit; the browser
    materializes its local full shape from the uniquely matching tool round.
    Opaque Responses/Anthropic provider-replay bodies and per-request stream /
    pricing evidence with no browser consumer are also server-only and omitted
    from this completed browser view. Per-round token, cost, cache-break,
    dispatch, quota, and trace facts remain. This function copies only changed
    containers and never mutates the shared snapshot returned by
    ``ConversationSnapshotQuery``.
    """
    turn_status = str(status or "")
    if (
        turn_status not in _BROWSER_TERMINAL_TURN_STATUSES
        or not isinstance(stable_projection, Mapping)
    ):
        return stable_projection
    completed = turn_status == _REFERENCEABLE_TURN_STATUS
    source_rounds = stable_projection.get("toolRounds")
    browser_rounds = _tool_rounds_for_browser(
        source_rounds,
        omit_provider_replay=completed,
    )
    source_api_rounds = stable_projection.get("apiRounds")
    browser_api_rounds = (
        _api_rounds_for_browser(source_api_rounds)
        if completed
        else source_api_rounds
    )
    rounds_by_id = _unique_rounds_by_call_id(browser_rounds)
    segments = stable_projection.get("segments")

    segment_id_counts: dict[str, int] = {}
    if isinstance(segments, list):
        for segment in segments:
            if (
                not isinstance(segment, Mapping)
                or segment.get("type") != "tool_use"
            ):
                continue
            call_id = str(segment.get("id") or "").strip()
            if call_id:
                segment_id_counts[call_id] = (
                    segment_id_counts.get(call_id, 0) + 1
                )

    segments_changed = False
    referenced_segments: list[Any] = []
    iterable_segments = segments if isinstance(segments, list) else []
    for source in iterable_segments:
        if not isinstance(source, Mapping) or source.get("type") != "tool_use":
            referenced_segments.append(source)
            continue
        call_id = str(source.get("id") or "").strip()
        round_record = rounds_by_id.get(call_id)
        if (
            round_record is None
            or segment_id_counts.get(call_id) != 1
            or (
                not completed
                and str(round_record.get("status") or "") != "done"
            )
        ):
            referenced_segments.append(source)
            continue
        if (
            not _tool_segment_matches_round(source, round_record)
            or source.get("result") != _segment_result_from_round(round_record)
        ):
            referenced_segments.append(source)
            continue
        referenced = dict(source)
        referenced.pop("input", None)
        referenced["result"] = {}
        referenced["roundRef"] = call_id
        referenced_segments.append(referenced)
        segments_changed = True

    if (
        not segments_changed
        and browser_rounds is source_rounds
        and browser_api_rounds is source_api_rounds
    ):
        return stable_projection
    projection = dict(stable_projection)
    if segments_changed:
        projection["segments"] = referenced_segments
    if browser_rounds is not source_rounds:
        projection["toolRounds"] = browser_rounds
    if browser_api_rounds is not source_api_rounds:
        projection["apiRounds"] = browser_api_rounds
    return projection


def turn_with_reference_tool_segments(stable_turn: Any) -> Any:
    """Return one stable TurnRecord in the request-local reference view."""
    if not isinstance(stable_turn, Mapping) or not isinstance(
        stable_turn.get("projection"), Mapping
    ):
        return stable_turn
    projection = projection_with_reference_tool_segments(
        stable_turn["projection"],
        status=str(stable_turn.get("status") or ""),
    )
    if projection is stable_turn["projection"]:
        return stable_turn
    turn = dict(stable_turn)
    turn["projection"] = projection
    return turn


def turns_with_reference_tool_segments(stable_turns: Any) -> Any:
    """Project a Turn list without recursively copying conversation payloads."""
    if not isinstance(stable_turns, list):
        return stable_turns
    return [turn_with_reference_tool_segments(turn) for turn in stable_turns]


def _unique_text_segment_references(
    segments: list[Any],
) -> dict[tuple[str, str], str]:
    """Index exact text authorities with globally unique block identities.

    A snapshot can reference many content/thinking values from one Turn. Build
    the index once so each lookup does not rescan the same segment list. Any
    duplicate text authority, invalid block identity, or block identity reused
    by another segment remains absent and therefore fails closed.
    """
    block_id_counts: Counter[str] = Counter()
    candidates: dict[tuple[str, str], str | None] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        block_id = segment.get("blockId")
        if isinstance(block_id, str):
            block_id_counts[block_id] += 1
        segment_type = segment.get("type")
        text = segment.get("text")
        if segment_type not in {"text", "thinking"} or not isinstance(text, str):
            continue
        key = (segment_type, text)
        candidate = block_id if isinstance(block_id, str) and block_id else None
        candidates[key] = None if key in candidates else candidate

    return {
        key: block_id
        for key, block_id in candidates.items()
        if block_id is not None and block_id_counts[block_id] == 1
    }


def _snapshot_reference_saves_bytes(inline: Any, reference: Any) -> bool:
    """Require a conservative net win before introducing wire indirection."""
    try:
        inline_size = len(orjson.dumps(inline))
        reference_size = len(orjson.dumps(reference))
    except TypeError:
        return False
    return inline_size - reference_size >= _SNAPSHOT_REFERENCE_MIN_SAVINGS


def _turns_with_referenced_projection_fields(
    stable_turns: Any,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Reference exact terminal projection fields from stable segments."""
    if not isinstance(stable_turns, list):
        return stable_turns, {}

    turn_id_counts: Counter[str] = Counter()
    for turn in stable_turns:
        if not isinstance(turn, Mapping):
            continue
        turn_id = turn.get("turnId")
        if isinstance(turn_id, str) and turn_id:
            turn_id_counts[turn_id] += 1
    replacements: dict[int, dict[str, Any]] = {}
    projection_references: dict[str, dict[str, Any]] = {}
    reference_count = 0
    for turn_index, turn in enumerate(stable_turns):
        if reference_count >= _SNAPSHOT_PROJECTION_MAX_REFERENCES:
            break
        if (
            not isinstance(turn, Mapping)
            or str(turn.get("status") or "")
            not in _BROWSER_TERMINAL_TURN_STATUSES
        ):
            continue
        turn_id = turn.get("turnId")
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or turn_id_counts[turn_id] != 1
        ):
            continue
        projection = turn.get("projection")
        if not isinstance(projection, Mapping):
            continue
        segments = projection.get("segments")
        if not isinstance(segments, list):
            continue
        segment_references = _unique_text_segment_references(segments)
        turn_references: dict[str, Any] = {}
        projected_projection: dict[str, Any] | None = None

        content = projection.get("content")
        if isinstance(content, str):
            try:
                encoded_content_size = len(orjson.dumps(content))
            except TypeError:
                encoded_content_size = 0
            content_block_id = segment_references.get(("text", content))
            if (
                encoded_content_size >= _SNAPSHOT_CONTENT_MIN_BYTES
                and content_block_id is not None
                and _snapshot_reference_saves_bytes(
                    {"content": content},
                    {turn_id: {"content": content_block_id}},
                )
            ):
                projected_projection = dict(projection)
                projected_projection.pop("content", None)
                turn_references["content"] = content_block_id
                reference_count += 1

        source_rounds = projection.get("toolRounds")
        projected_rounds: list[Any] | None = None
        round_thinking_references: dict[str, str] = {}
        if (
            isinstance(source_rounds, list)
            and reference_count < _SNAPSHOT_PROJECTION_MAX_REFERENCES
        ):
            call_id_counts: Counter[str] = Counter(
                round_record.get("toolCallId")
                for round_record in source_rounds
                if isinstance(round_record, Mapping)
                and isinstance(round_record.get("toolCallId"), str)
                and round_record.get("toolCallId")
            )
            for round_index, round_record in enumerate(source_rounds):
                if reference_count >= _SNAPSHOT_PROJECTION_MAX_REFERENCES:
                    break
                if not isinstance(round_record, Mapping):
                    continue
                call_id = round_record.get("toolCallId")
                thinking = round_record.get("thinking")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or call_id != call_id.strip()
                    or call_id_counts[call_id] != 1
                    or not isinstance(thinking, str)
                ):
                    continue
                thinking_block_id = segment_references.get(
                    ("thinking", thinking)
                )
                if thinking_block_id is None or not _snapshot_reference_saves_bytes(
                    {"thinking": thinking},
                    {call_id: thinking_block_id},
                ):
                    continue
                if projected_rounds is None:
                    projected_rounds = list(source_rounds)
                projected_round = dict(round_record)
                projected_round.pop("thinking", None)
                projected_rounds[round_index] = projected_round
                round_thinking_references[call_id] = thinking_block_id
                reference_count += 1

        if round_thinking_references:
            turn_references["roundThinking"] = round_thinking_references
        if not turn_references:
            continue
        if projected_projection is None:
            projected_projection = dict(projection)
        if projected_rounds is not None:
            projected_projection["toolRounds"] = projected_rounds
        replacements[turn_index] = {
            **turn,
            "projection": projected_projection,
        }
        projection_references[turn_id] = turn_references

    if not replacements:
        return stable_turns, {}
    projected_turns = list(stable_turns)
    for turn_index, turn in replacements.items():
        projected_turns[turn_index] = turn
    return projected_turns, projection_references


def _turns_with_shared_tool_documents(
    stable_turns: Any,
) -> tuple[Any, dict[str, Any]]:
    """Intern repeated large browser-only round fields within one snapshot.

    Storage and the default/full API remain unchanged.  The reference view
    keeps a bounded request-local dictionary and shallow-copies only affected
    Turn/projection/round containers.  Full SHA-256 keys plus an equality
    check make a collision fall back to inline values rather than aliasing two
    documents.  The browser restores these exact objects before TurnStore.
    """
    if not isinstance(stable_turns, list):
        return stable_turns, {}

    candidates: list[tuple[int, int, str, str]] = []
    counts: Counter[str] = Counter()
    samples: dict[str, Any] = {}
    collisions: set[str] = set()
    for turn_index, turn in enumerate(stable_turns):
        if (
            not isinstance(turn, Mapping)
            or str(turn.get("status") or "")
            not in _BROWSER_TERMINAL_TURN_STATUSES
        ):
            continue
        projection = turn.get("projection")
        rounds = (
            projection.get("toolRounds")
            if isinstance(projection, Mapping)
            else None
        )
        if not isinstance(rounds, list):
            continue
        for round_index, round_record in enumerate(rounds):
            if not isinstance(round_record, Mapping):
                continue
            # A stored field with the representation-only name is ambiguous;
            # leave the whole round inline so the browser never guesses which
            # authority minted it.
            if _SNAPSHOT_DOCUMENT_REFS_FIELD in round_record:
                continue
            for field in _SHARED_TOOL_DOCUMENT_FIELDS:
                if field not in round_record:
                    continue
                try:
                    encoded = orjson.dumps(
                        round_record[field], option=orjson.OPT_SORT_KEYS
                    )
                except TypeError:
                    continue
                if len(encoded) < _SHARED_TOOL_DOCUMENT_MIN_BYTES:
                    continue
                document_key = "sha256:" + hashlib.sha256(encoded).hexdigest()
                if (
                    document_key in samples
                    and samples[document_key] != round_record[field]
                ):
                    collisions.add(document_key)
                else:
                    samples.setdefault(document_key, round_record[field])
                candidates.append(
                    (turn_index, round_index, field, document_key)
                )
                counts[document_key] += 1
                if len(candidates) >= _SHARED_TOOL_DOCUMENT_MAX_REFERENCES:
                    break
            if len(candidates) >= _SHARED_TOOL_DOCUMENT_MAX_REFERENCES:
                break
        if len(candidates) >= _SHARED_TOOL_DOCUMENT_MAX_REFERENCES:
            break

    eligible: set[str] = set()
    for _turn_index, _round_index, _field, document_key in candidates:
        if (
            document_key in eligible
            or document_key in collisions
            or counts[document_key] < 2
        ):
            continue
        if len(eligible) >= _SHARED_TOOL_DOCUMENT_MAX_DOCUMENTS:
            break
        eligible.add(document_key)
    if not eligible:
        return stable_turns, {}

    refs_by_round: dict[tuple[int, int], dict[str, str]] = {}
    shared_documents: dict[str, Any] = {}
    for turn_index, round_index, field, document_key in candidates:
        if document_key not in eligible:
            continue
        refs_by_round.setdefault((turn_index, round_index), {})[
            field
        ] = document_key
        shared_documents.setdefault(document_key, samples[document_key])

    projected_turns: list[Any] = []
    for turn_index, turn in enumerate(stable_turns):
        if not isinstance(turn, Mapping):
            projected_turns.append(turn)
            continue
        projection = turn.get("projection")
        rounds = (
            projection.get("toolRounds")
            if isinstance(projection, Mapping)
            else None
        )
        if not isinstance(rounds, list):
            projected_turns.append(turn)
            continue
        projected_rounds: list[Any] = []
        changed = False
        for round_index, round_record in enumerate(rounds):
            field_refs = refs_by_round.get((turn_index, round_index))
            if not field_refs or not isinstance(round_record, Mapping):
                projected_rounds.append(round_record)
                continue
            projected_round = {
                key: value
                for key, value in round_record.items()
                if key not in field_refs
            }
            projected_round[_SNAPSHOT_DOCUMENT_REFS_FIELD] = field_refs
            projected_rounds.append(projected_round)
            changed = True
        if not changed:
            projected_turns.append(turn)
            continue
        projected_turns.append({
            **turn,
            "projection": {**projection, "toolRounds": projected_rounds},
        })
    return projected_turns, shared_documents


def _turns_with_lazy_legacy_images(
    turns: Any,
    *,
    conversation_id: Any,
    owner_cache_scope: str,
) -> Any:
    """Replace duplicate historical image bytes with immutable fetch URLs."""
    if (
        not isinstance(turns, list)
        or not isinstance(conversation_id, str)
        or not conversation_id
        or len(conversation_id) > 256
        or not isinstance(owner_cache_scope, str)
        or not owner_cache_scope
        or len(owner_cache_scope) > 64
    ):
        return turns
    turn_id_counts = Counter(
        turn.get("turnId")
        for turn in turns
        if isinstance(turn, Mapping)
        and isinstance(turn.get("turnId"), str)
        and turn.get("turnId")
    )
    projected_turns: list[Any] = []
    encoded_conversation_id = quote(conversation_id, safe="")
    for turn in turns:
        if not isinstance(turn, Mapping) or turn.get("status") != "completed":
            projected_turns.append(turn)
            continue
        turn_id = turn.get("turnId")
        revision = turn.get("projectionRevision")
        projection = turn.get("projection")
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or len(turn_id) > 128
            or turn_id_counts[turn_id] != 1
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or not isinstance(projection, Mapping)
        ):
            projected_turns.append(turn)
            continue
        images = projection.get("images")
        if not isinstance(images, list):
            projected_turns.append(turn)
            continue
        projected_images: list[Any] = []
        changed = False
        encoded_turn_id = quote(turn_id, safe="")
        for image_index, image in enumerate(images):
            uploaded_image_ref = (
                canonical_image_ref(image.get("url"))
                if isinstance(image, Mapping)
                else ""
            )
            if uploaded_image_ref:
                projected_image = {
                    key: value
                    for key, value in image.items()
                    if key != "base64"
                }
                projected_image["url"] = uploaded_image_ref
                projected_image["preview"] = uploaded_image_ref
                projected_images.append(projected_image)
                changed = changed or projected_image != image
                continue
            payload = (
                legacy_turn_image_payload(image)
                if image_index < MAX_TURN_IMAGES
                else None
            )
            if (
                payload is None
                or payload.encoded_length
                < MIN_LAZY_TURN_IMAGE_ENCODED_CHARS
            ):
                projected_images.append(image)
                continue
            projected_image = {
                key: value
                for key, value in image.items()
                if key != "base64"
            }
            projected_image["preview"] = (
                f"/api/v3/conversations/{encoded_conversation_id}/turns/"
                f"{encoded_turn_id}/images/{image_index}"
                f"?projectionRevision={revision}&ownerScope="
                f"{quote(owner_cache_scope, safe='')}"
            )
            projected_images.append(projected_image)
            changed = True
        if not changed:
            projected_turns.append(turn)
            continue
        projected_turns.append({
            **turn,
            "projection": {**projection, "images": projected_images},
        })
    return projected_turns


def snapshot_with_reference_tool_segments(
    stable_snapshot: Any,
    *,
    owner_cache_scope: str = "",
) -> Any:
    """Build the generated browser's exact, request-local snapshot view."""
    if not isinstance(stable_snapshot, Mapping):
        return stable_snapshot
    referenced = dict(stable_snapshot)
    # This representation field is minted here only. Never forward a stored
    # lookalike into the generated browser boundary.
    referenced.pop("sharedToolDocuments", None)
    referenced.pop(_SNAPSHOT_PROJECTION_REFS_FIELD, None)
    referenced_turns = turns_with_reference_tool_segments(
        stable_snapshot.get("turns")
    )
    referenced_turns = _turns_with_lazy_legacy_images(
        referenced_turns,
        conversation_id=stable_snapshot.get("conversationId"),
        owner_cache_scope=owner_cache_scope,
    )
    referenced_turns, projection_references = (
        _turns_with_referenced_projection_fields(referenced_turns)
    )
    referenced_turns, shared_documents = _turns_with_shared_tool_documents(
        referenced_turns
    )
    referenced["turns"] = referenced_turns
    if shared_documents:
        referenced["sharedToolDocuments"] = shared_documents
    if projection_references:
        referenced[_SNAPSHOT_PROJECTION_REFS_FIELD] = projection_references
    return referenced


__all__ = [
    "projection_with_reference_tool_segments",
    "projection_with_stable_segments",
    "public_turn_with_stable_segments",
    "public_value_with_stable_segments",
    "snapshot_with_reference_tool_segments",
    "turn_with_reference_tool_segments",
    "turns_with_reference_tool_segments",
]

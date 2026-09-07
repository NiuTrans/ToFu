"""Causal replay rules for persisted tool-round occurrences.

Tool-call ids and local round counters are correlation metadata, not proof that
an execution happened.  In particular, an early ``tool_start`` from a provider
attempt that was later discarded is a transport artifact even though it lives
beside real tool rounds for UI settlement.

This module is the backend semantic boundary used by Continue/checkpoint paths:

* discarded provider-attempt artifacts are transparent;
* a terminal error/rejection with an authoritative result is still an execution
  fact and is replayable (``status == 'done'`` is not required);
* a real call without a result or valid caller authority stops the causal
  prefix, so later calls are never replayed across an invented gap.

The helpers are pure except for caller dictionaries being copied and
canonicalized in returned round records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.tool_caller_identity import (
    MAX_TOOL_CALLER_ID_CHARS,
    normalize_tool_caller,
)


SUPERSEDED_PROVIDER_ATTEMPT_FIELD = "_providerAttemptDiscarded"
_MAX_REPLAY_TOOL_NAME_CHARS = 512


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def normalize_replay_tool_arguments(value: Any) -> tuple[str | None, bool]:
    """Return safe JSON-object text and whether compatibility repair occurred.

    Provider-origin strings are preserved byte-for-byte when they decode to a
    JSON object. Malformed/non-object strings are mapped to ``{}``, matching
    the live post-parse sanitizer: the paired error result still tells the
    model why execution was rejected while the next provider cannot reject the
    history itself. Structured mappings are serialized deterministically so a
    legacy/storage adapter cannot silently erase real arguments. An
    unserializable mapping (or another non-protocol type) returns ``None``;
    callers must stop the causal replay prefix instead of inventing arguments.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "{}", True
        if not isinstance(decoded, dict):
            return "{}", True
        return value, False
    if isinstance(value, Mapping):
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ), False
        except (TypeError, ValueError, OverflowError):
            return None, False
    if value is None:
        return "{}", True
    return None, False


def is_superseded_provider_attempt_round(value: Any) -> bool:
    """Return whether *value* is a result-less discarded-attempt artifact.

    New rows carry an explicit machine marker.  The result-metadata fallback is
    retained for already-persisted rows created before that marker existed.
    A late real result does resurrect a legacy badge-only row because the
    badge is only compatibility evidence.  Only the explicit marker is
    unconditional.
    """
    if not isinstance(value, Mapping):
        return False
    if value.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD) is True:
        return True
    raw_results = value.get("results")
    if not isinstance(raw_results, (list, tuple)) or not raw_results:
        return False
    first = raw_results[0]
    if not isinstance(first, Mapping) or first.get("badge") != "superseded":
        return False
    fetched_chars = first.get("fetchedChars")
    has_fetched_chars = (
        isinstance(fetched_chars, (int, float))
        and not isinstance(fetched_chars, bool)
        and fetched_chars > 0
    )
    has_real_result = (
        value.get("toolContent") is not None
        or first.get("fetched") is True
        or has_fetched_chars
    )
    return not has_real_result


@dataclass(frozen=True)
class ToolRoundReplayPrefix:
    """One validated causal prefix and its positions in the raw projection."""

    rounds: tuple[dict[str, Any], ...]
    positions: tuple[int, ...]
    raw_prefix_length: int
    ignored_count: int
    blocked_position: int | None = None
    blocked_reason: str = ""


def scan_replayable_tool_round_prefix(values: Any) -> ToolRoundReplayPrefix:
    """Validate the longest replayable tool-execution prefix.

    Non-mapping and identity-free rows are display/progress carriers: live
    executable calls are assigned an id before dispatch, so those rows are
    transparent.  A discarded provider-attempt row is also transparent.  Once
    a row claims a tool-call identity, however, malformed identity/authority or
    a missing result is a causal gap and stops the scan.
    """
    raw_values = list(values) if isinstance(values, (list, tuple)) else []
    replayable: list[dict[str, Any]] = []
    positions: list[int] = []
    ignored_count = 0
    blocked_position: int | None = None
    blocked_reason = ""

    for position, raw_value in enumerate(raw_values):
        if not isinstance(raw_value, Mapping):
            ignored_count += 1
            continue
        if is_superseded_provider_attempt_round(raw_value):
            ignored_count += 1
            continue

        if ("toolCallId" not in raw_value
                or raw_value.get("toolCallId") is None):
            # Synthetic inbox/progress/display rows never reached tool dispatch.
            ignored_count += 1
            continue
        tool_call_id = raw_value.get("toolCallId")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or len(tool_call_id) > MAX_TOOL_CALLER_ID_CHARS
        ):
            blocked_position = position
            blocked_reason = "invalid_tool_call_id"
            break

        tool_name = raw_value.get("toolName")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or len(tool_name) > _MAX_REPLAY_TOOL_NAME_CHARS
        ):
            blocked_position = position
            blocked_reason = "invalid_tool_name"
            break

        # The model-visible result is the execution receipt.  Status is only a
        # verdict: error/rejected/aborted results remain valid protocol facts.
        tool_content = raw_value.get("toolContent")
        if not isinstance(tool_content, str):
            blocked_position = position
            blocked_reason = (
                "missing_tool_result" if tool_content is None
                else "invalid_tool_result"
            )
            break

        caller, caller_error = normalize_tool_caller(
            raw_value.get("caller") if "caller" in raw_value else None
        )
        if caller_error:
            blocked_position = position
            blocked_reason = "invalid_tool_caller"
            break

        tool_arguments, arguments_repaired = normalize_replay_tool_arguments(
            raw_value.get("toolArgs") if "toolArgs" in raw_value else None)
        if tool_arguments is None:
            blocked_position = position
            blocked_reason = "invalid_tool_arguments"
            break

        normalized = dict(raw_value)
        normalized["toolArgs"] = tool_arguments
        if arguments_repaired:
            normalized["_toolArgsSanitizedForReplay"] = True
        if caller is None:
            normalized.pop("caller", None)
        else:
            normalized["caller"] = caller
        replayable.append(normalized)
        positions.append(position)

    raw_prefix_length = positions[-1] + 1 if positions else 0
    return ToolRoundReplayPrefix(
        rounds=tuple(replayable),
        positions=tuple(positions),
        raw_prefix_length=raw_prefix_length,
        ignored_count=ignored_count,
        blocked_position=blocked_position,
        blocked_reason=blocked_reason,
    )


def checkpoint_retention_positions(
    values: Any,
    prefix: ToolRoundReplayPrefix,
) -> tuple[int, list[int]]:
    """Return the raw retention boundary and row positions a checkpoint keeps.

    Replay authority stays the causal prefix, but the durable projection must
    retain every row before the first causal gap — display/progress carriers
    (program shells, inbox rows) included — or a checkpoint_resume would
    permanently erase rendered history the model legitimately produced.  Only
    explicitly superseded provider-attempt artifacts are filtered: they are
    transport noise about calls that never executed.  The gap row itself and
    everything after it is amputated; it has no result, and its diagnostic
    lives on in the activity timeline.
    """
    raw_values = list(values) if isinstance(values, (list, tuple)) else []
    boundary = (
        prefix.blocked_position
        if prefix.blocked_position is not None
        else len(raw_values)
    )
    positions = [
        position for position in range(boundary)
        if not is_superseded_provider_attempt_round(raw_values[position])
    ]
    return boundary, positions


__all__ = [
    "SUPERSEDED_PROVIDER_ATTEMPT_FIELD",
    "checkpoint_retention_positions",
    "ToolRoundReplayPrefix",
    "is_superseded_provider_attempt_round",
    "normalize_replay_tool_arguments",
    "scan_replayable_tool_round_prefix",
]

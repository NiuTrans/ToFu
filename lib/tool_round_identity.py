"""Attempt-scoped identity for tool rounds inside one durable Turn.

One visible assistant Turn may contain several generation attempts after a
restart, Continue, or checkpoint resume.  ``roundNum`` and ``llmRound`` are
executor-local counters and restart from zero for every attempt, so neither is
a durable identity by itself.  This module owns the public ``attemptId`` /
``taskId`` stamps used by persistence, segment assembly, and presentation.

The helpers are pure and copy-on-write.  Storage and task managers decide when
to persist the returned values; executor-owned live dictionaries are never
mutated here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_MAX_EXECUTION_ID_CHARS = 256
_MAX_LLM_ROUND_TEXT_CHARS = 64


def _identity_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()[:_MAX_EXECUTION_ID_CHARS]
    # Dispatch claims are internal storage sentinels, not task identities.
    return "" if text.startswith("@dispatching:") else text


def execution_llm_round(value: Mapping[str, Any] | None) -> int | str | None:
    """Return a bounded, hashable executor-local round identifier."""
    if not isinstance(value, Mapping):
        return None
    raw = value.get("llmRound")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip()
        if normalized and len(normalized) <= _MAX_LLM_ROUND_TEXT_CHARS:
            return normalized
    return None


def execution_identity(value: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return the public attempt/task identity carried by ``value``."""
    if not isinstance(value, Mapping):
        return "", ""
    return (
        _identity_text(value.get("attemptId") or value.get("_attemptId")),
        _identity_text(value.get("taskId") or value.get("_taskId")),
    )


def execution_batch_key(
    value: Mapping[str, Any],
    *,
    position: int | None = None,
) -> tuple[Any, ...]:
    """Return one attempt-aware model batch identity.

    Multiple parallel calls from one model response share this key.  A resumed
    attempt with the same ``llmRound`` does not.  Legacy rows retain their old
    behavior; ``position`` keeps rounds without an LLM counter independent.
    """
    attempt_id, task_id = execution_identity(value)
    scope = attempt_id or task_id
    llm_round = execution_llm_round(value)
    if llm_round is not None:
        return ("llm", scope, llm_round)
    return ("position", scope, position)


def _round_number(value: Mapping[str, Any]) -> int | None:
    """Return a tool ordinal from a round or a rehydrated segment."""
    raw = value.get("roundNum")
    if raw is None and isinstance(value.get("_round"), Mapping):
        raw = value["_round"].get("roundNum")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _is_tool_carrier(value: Mapping[str, Any]) -> bool:
    return value.get("type") == "tool_use" or bool(value.get("toolCallId"))


def execution_batch_keys(values: Sequence[Any]) -> list[tuple[Any, ...]]:
    """Return collision-free, ordered model-batch keys for one Turn.

    ``execution_batch_key`` names a batch when attempt scope is present.  Old
    projections predate that scope, however, and may contain ``L0,L1,L0``
    after Continue.  A global dict keyed by the base key would merge those two
    L0 provider responses and reorder the model wire.  This helper adds a
    contiguous occurrence to every base key and recognizes the otherwise
    ambiguous adjacent legacy reset through ``roundNum`` (or a new prose
    prefix after tool-use segments).

    The returned keys are positional metadata only; no legacy row is falsely
    attributed to an attempt and the input is never mutated.
    """
    occurrence_counts: dict[tuple[Any, ...], int] = {}
    keys: list[tuple[Any, ...]] = []
    previous_base: tuple[Any, ...] | None = None
    previous_value: Mapping[str, Any] | None = None
    current_key: tuple[Any, ...] | None = None
    current_has_tool = False

    for position, raw_value in enumerate(values):
        value = raw_value if isinstance(raw_value, Mapping) else {}
        base = execution_batch_key(value, position=position)
        attempt_id, task_id = execution_identity(value)
        unscoped = not (attempt_id or task_id)
        starts_new_occurrence = current_key is None or base != previous_base

        if not starts_new_occurrence and unscoped and previous_value is not None:
            previous_round = _round_number(previous_value)
            current_round = _round_number(value)
            counter_restarted = (
                previous_round is not None
                and current_round is not None
                and current_round <= previous_round
            )
            prose_after_tools = (
                current_has_tool
                and value.get("type") in {"text", "thinking"}
                and not value.get("terminal")
            )
            round_prose_after_tools = (
                current_has_tool
                and bool(value.get("assistantContent") or value.get("thinking"))
            )
            starts_new_occurrence = (
                counter_restarted
                or prose_after_tools
                or round_prose_after_tools
            )

        if starts_new_occurrence:
            occurrence = occurrence_counts.get(base, 0)
            occurrence_counts[base] = occurrence + 1
            current_key = (*base, "occurrence", occurrence)
            current_has_tool = False

        # ``current_key`` is initialized by the first row above.
        assert current_key is not None
        keys.append(current_key)
        current_has_tool = current_has_tool or _is_tool_carrier(value)
        previous_base = base
        previous_value = value

    return keys


def tool_round_batches(values: Sequence[Any]) -> list[list[Any]]:
    """Group tool rounds into provider-response batches without reordering.

    Modern rows use attempt-scoped ``llmRound`` plus contiguous occurrence.
    Rows predating ``llmRound`` retain the historical round-gap fallback.
    """
    rows = list(values or [])
    if not rows:
        return []

    has_llm_round = any(
        isinstance(value, Mapping) and value.get("llmRound") is not None
        for value in rows
    )
    if has_llm_round:
        keys = execution_batch_keys(rows)
        batches: list[list[Any]] = []
        current: list[Any] = []
        previous_key: tuple[Any, ...] | None = None
        for value, key in zip(rows, keys):
            if current and key != previous_key:
                batches.append(current)
                current = []
            current.append(value)
            previous_key = key
        if current:
            batches.append(current)
        return batches

    batches = []
    current = []
    previous_round: int | None = None
    for raw_value in rows:
        value = raw_value if isinstance(raw_value, Mapping) else {}
        round_number = _round_number(value)
        if (current and previous_round is not None and round_number is not None
                and round_number > previous_round + 1):
            batches.append(current)
            current = []
        current.append(raw_value)
        previous_round = round_number
    if current:
        batches.append(current)
    return batches


def model_batch_block_suffix(
    llm_round: Any,
    *,
    attempt_id: Any = "",
    task_id: Any = "",
) -> str:
    """Return the stable segment suffix for one attempt-local model round."""
    scope = _identity_text(attempt_id) or _identity_text(task_id)
    scope_prefix = f"attempt-{scope}:" if scope else ""
    return f"{scope_prefix}llm-{llm_round}"


def model_batch_segment_block_id(
    segment_type: str,
    llm_round: Any,
    *,
    attempt_id: Any = "",
    task_id: Any = "",
) -> str:
    """Return the segment ``blockId`` for narration/reasoning in a batch."""
    return (
        f"{segment_type}:"
        f"{model_batch_block_suffix(llm_round, attempt_id=attempt_id, task_id=task_id)}"
    )


def tool_round_with_execution_identity(
    value: Any,
    *,
    attempt_id: Any,
    task_id: Any = "",
    overwrite: bool = False,
) -> Any:
    """Return a copied tool round carrying its owning execution identity."""
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    normalized_attempt = _identity_text(attempt_id)
    normalized_task = _identity_text(task_id)
    if normalized_attempt and (overwrite or not result.get("attemptId")):
        result["attemptId"] = normalized_attempt
    if normalized_task and (overwrite or not result.get("taskId")):
        result["taskId"] = normalized_task
    return result


def tool_rounds_with_execution_identity(
    values: Any,
    *,
    attempt_id: Any,
    task_id: Any = "",
    overwrite: bool = False,
) -> list[Any]:
    """Copy a round list and stamp each mapping with one execution identity."""
    if not isinstance(values, (list, tuple)):
        return []
    return [
        tool_round_with_execution_identity(
            value,
            attempt_id=attempt_id,
            task_id=task_id,
            overwrite=overwrite,
        )
        for value in values
    ]


def projection_history_with_execution_identity(
    raw_projection: Any,
    *,
    attempt_id: Any,
    task_id: Any = "",
) -> dict[str, Any]:
    """Backfill one settled attempt's identity before a successor starts.

    Existing stamps always win.  That makes repeated resumes incremental: old
    attempts keep their own identity while only legacy, previously unstamped
    history is attributed to the attempt that currently owns the Turn.
    """
    projection = dict(raw_projection) if isinstance(raw_projection, Mapping) else {}
    rounds = projection.get("toolRounds")
    if isinstance(rounds, list):
        projection["toolRounds"] = tool_rounds_with_execution_identity(
            rounds,
            attempt_id=attempt_id,
            task_id=task_id,
        )

    segments = projection.get("segments")
    if not isinstance(segments, list):
        return projection
    scoped_segments: list[Any] = []
    for value in segments:
        if not isinstance(value, Mapping):
            scoped_segments.append(value)
            continue
        segment = dict(value)
        # Terminal answer/thinking blocks describe the whole visible Turn.
        # Attempt scope is needed only for model-batch and tool identities.
        if segment.get("type") == "tool_use" or segment.get("llmRound") is not None:
            normalized_attempt = _identity_text(attempt_id)
            normalized_task = _identity_text(task_id)
            if normalized_attempt and not segment.get("attemptId"):
                segment["attemptId"] = normalized_attempt
            if normalized_task and not segment.get("taskId"):
                segment["taskId"] = normalized_task
            if isinstance(segment.get("_round"), Mapping):
                segment["_round"] = tool_round_with_execution_identity(
                    segment["_round"],
                    attempt_id=attempt_id,
                    task_id=task_id,
                )
        scoped_segments.append(segment)
    projection["segments"] = scoped_segments
    return projection


__all__ = [
    "execution_batch_key",
    "execution_batch_keys",
    "execution_identity",
    "execution_llm_round",
    "model_batch_block_suffix",
    "model_batch_segment_block_id",
    "projection_history_with_execution_identity",
    "tool_round_batches",
    "tool_round_with_execution_identity",
    "tool_rounds_with_execution_identity",
]

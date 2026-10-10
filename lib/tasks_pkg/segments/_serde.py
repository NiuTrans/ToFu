"""lib/tasks_pkg/segments/_serde.py — persistable ("thin") form + rehydration.

``segments_to_json`` strips the ``_round`` mirror (a full copy of the origin
round dict, already persisted in the sibling ``tool_rounds`` column) so the
persisted segments don't duplicate the complete round or become a second
source of truth. The remaining render result is intentionally self-contained;
Turn projection normalization aligns it with any explicit durable L1/frame
compaction so pre-compaction result bytes cannot survive in that mirror.
Conversation attempts persist their complete timeline only in the authoritative
Turn projection; this serializer remains the task-result authority for
inline/headless tasks.
``rehydrate_segments`` is the inverse — re-zips the k-th ``tool_use`` segment
with the k-th co-persisted round so ``derive_tool_rounds`` is byte-identical
again.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from lib.log import get_logger
from lib.tool_round_identity import execution_identity, execution_llm_round

from lib.tasks_pkg.segments._types import SEG_TOOL_USE, is_synthetic_inbox_round

logger = get_logger(__name__)


def segments_to_json(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the PERSISTABLE ("thin") form of the segment list.

    Strips the ``_round`` mirror off every ``tool_use`` segment. ``_round``
    embeds the ENTIRE origin round dict (assistantContent / toolArgs / thinking
    / results / …), which is already persisted verbatim in the sibling
    ``task_results.tool_rounds`` column and ``last_msg['toolRounds']``. Keeping
    it inside ``segments`` too would double the largest payload AND create a
    second source of truth that can drift from the ``toolRounds`` column.

    The thin form keeps everything a reader needs WITHOUT ``toolRounds``:
    ``thinking`` / ``text`` (with ``deliverable``) segments are complete, and a
    ``tool_use`` keeps ``id`` / ``name`` / ``input`` / ``llmRound`` / ``result``
    (the nested ``{content,status}``) — enough for the compat surfaces (step 3)
    to render block-by-block. The full round is recoverable via
    ``rehydrate_segments`` when ``derive_tool_rounds`` is needed (step 4).
    The nested result is a render mirror, not another compaction authority;
    the Turn projection boundary synchronizes it when its sibling round carries
    an explicit durable compaction receipt.

    Returns NEW segment dicts (shallow copies); the input is not mutated.
    """
    out: list[dict[str, Any]] = []
    for s in (segments or []):
        if not isinstance(s, dict):
            continue
        if s.get('type') == SEG_TOOL_USE and '_round' in s:
            s = {k: v for k, v in s.items() if k != '_round'}
        out.append(s)
    return out


def _text_identity(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _segment_round_compatible(segment: dict[str, Any], round_entry: dict[str, Any]
                              ) -> bool:
    """Whether two occurrence carriers can be the same assembled tool use."""
    segment_id = _text_identity(segment.get('id'))
    round_id = _text_identity(round_entry.get('toolCallId'))
    if segment_id and round_id and segment_id != round_id:
        return False
    segment_name = _text_identity(segment.get('name'))
    round_name = _text_identity(round_entry.get('toolName'))
    if segment_name and round_name and segment_name != round_name:
        return False

    segment_attempt, segment_task = execution_identity(segment)
    round_attempt, round_task = execution_identity(round_entry)
    segment_scope = segment_attempt or segment_task
    round_scope = round_attempt or round_task
    if segment_scope and round_scope and segment_scope != round_scope:
        return False

    segment_llm_round = execution_llm_round(segment)
    round_llm_round = execution_llm_round(round_entry)
    if (segment_llm_round is not None and round_llm_round is not None
            and segment_llm_round != round_llm_round):
        return False
    return bool(segment_id or round_id or segment_name or round_name)


def _carrier_identity_key(value: dict[str, Any], *, segment: bool
                          ) -> tuple[Any, ...] | None:
    call_id = _text_identity(value.get('id' if segment else 'toolCallId'))
    name = _text_identity(value.get('name' if segment else 'toolName'))
    if not call_id and not name:
        return None
    attempt_id, task_id = execution_identity(value)
    return (
        attempt_id or task_id,
        execution_llm_round(value),
        call_id,
        name,
    )


def rehydrate_segments(thin_segments: list[dict[str, Any]],
                       tool_rounds: list) -> list[dict[str, Any]]:
    """Re-attach the ``_round`` mirror to a thin (persisted) segment list.

    The inverse of ``segments_to_json``: walks ``tool_use`` segments in order
    and occurrence-pairs them with real (non-synthetic) rounds. Pairing checks
    call/name/execution/batch identity before attaching; it never blindly zips
    a display-only or malformed row onto the next real tool occurrence.
    After rehydration ``derive_tool_rounds`` is byte-identical to
    ``_merge_tool_rounds`` again, proving the strip is LOSSLESS given
    ``tool_rounds`` was co-persisted.

    Non-``tool_use`` segments pass through unchanged. If identities/counts
    disagree, unmatched ``tool_use`` segments remain thin; consumers skip the
    missing mirror instead of borrowing authority metadata from another call.

    Returns NEW segment dicts; inputs are not mutated.
    """
    if not isinstance(thin_segments, (list, tuple)):
        return []
    candidate_rounds = [
        round_entry
        for round_entry in (tool_rounds or [])
        if isinstance(round_entry, dict)
        and not is_synthetic_inbox_round(round_entry)
    ] if isinstance(tool_rounds, (list, tuple)) else []
    candidates_by_identity: dict[tuple[Any, ...], deque[dict[str, Any]]] = (
        defaultdict(deque))
    for round_entry in candidate_rounds:
        identity_key = _carrier_identity_key(round_entry, segment=False)
        if identity_key is not None:
            candidates_by_identity[identity_key].append(round_entry)
    out: list[dict[str, Any]] = []
    unmatched_tool_uses = 0
    for source_segment in thin_segments:
        if not isinstance(source_segment, dict):
            continue
        segment = source_segment
        if segment.get('type') == SEG_TOOL_USE:
            identity_key = _carrier_identity_key(segment, segment=True)
            candidate_queue = candidates_by_identity.get(identity_key)
            round_entry = candidate_queue.popleft() if candidate_queue else None
            if (round_entry is None
                    or not _segment_round_compatible(segment, round_entry)):
                unmatched_tool_uses += 1
            else:
                segment = {
                    **segment,
                    '_round': round_entry,
                }
        out.append(segment)
    if unmatched_tool_uses:
        logger.warning(
            '[segments] Left %d tool_use segment(s) thin because no '
            'identity-compatible durable round occurrence exists',
            unmatched_tool_uses)
    return out

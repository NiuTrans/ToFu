"""lib/tasks_pkg/segments/_project.py — wire/prefill projections OFF segments.

These rebuild higher-level structures from the segment list:
``reconstruct_tool_messages_from_segments`` (assistant(tool_calls)+tool wire
messages), ``tool_history_from_segments`` (the Continue ``toolHistory`` shape),
and ``resume_prefill_from_segments`` (the resumable assistant-prefill tail).

Pure functions; no Flask, no DB. (``model_supports_assistant_prefill`` is a
pure model-capability lookup, lazy-imported to avoid a package-load cycle.)
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.segments._types import SEG_TEXT, RESUMABLE_FINISH_REASONS
from lib.tasks_pkg.segments._derive import _rounds_view_from_segments

logger = get_logger(__name__)


def reconstruct_tool_messages_from_segments(segments: list[dict[str, Any]]):
    """Segment-driven equivalent of `_reconstruct_tool_call_messages(rounds)`.

    Rebuilds the per-round view from the segment structure
    (`_rounds_view_from_segments`) then delegates to the vetted reconstructor,
    so the emitted assistant(tool_calls)+tool message shape is byte-identical
    to the toolRounds-fed path — proving segments can drive the exact wire
    messages. Returns the message list, or None (→ caller uses the legacy
    fallback) when any round lacks the required identity fields.
    """
    from lib.tasks_pkg.conv_message_builder import _reconstruct_tool_call_messages
    rounds = _rounds_view_from_segments(segments)
    return _reconstruct_tool_call_messages(rounds)


def tool_history_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the `cfg['toolHistory']` shape (what `inject_tool_history`
    consumes on Continue) from the segment structure — one entry per llmRound
    batch: `{assistantContent, thinking, thinkingSignature, toolCalls[], toolResults[]}`.

    Lets a Continue rebuild be driven from persisted segments byte-identically
    to the frontend-supplied toolHistory (the step-4 parity gate).
    """
    rounds = _rounds_view_from_segments(segments)
    history: list[dict[str, Any]] = []
    by_batch: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for r in rounds:
        lr = r.get('llmRound')
        if lr not in by_batch:
            entry: dict[str, Any] = {'toolCalls': [], 'toolResults': []}
            if r.get('assistantContent'):
                entry['assistantContent'] = r['assistantContent']
            if r.get('thinking'):
                entry['thinking'] = r['thinking']
            if r.get('thinkingSignature'):
                entry['thinkingSignature'] = r['thinkingSignature']
            by_batch[lr] = entry
            order.append(lr)
        tc: dict[str, Any] = {'id': r['toolCallId'], 'name': r['toolName'],
                              'arguments': r.get('toolArgs') or '{}'}
        if r.get('extraContent'):
            tc['extraContent'] = r['extraContent']
        by_batch[lr]['toolCalls'].append(tc)
        by_batch[lr]['toolResults'].append(
            {'tool_call_id': r['toolCallId'], 'content': r.get('toolContent') or ''})
    for lr in order:
        history.append(by_batch[lr])
    return history


def resume_prefill_from_segments(segments: list[dict[str, Any]] | None,
                                 model: str,
                                 finish_reason: str | None = None) -> str | None:
    """Extract the resumable assistant-prefill string for a Continue turn.

    Returns the terminal deliverable text (the tail the model was mid-writing)
    IFF the provider tolerates a trailing ``role='assistant'`` prefill AND the
    turn ended in a resumable state. Otherwise ``None`` → the caller keeps the
    universal contentPrefix-seed behaviour (Claude / clean stop).

    Why the terminal deliverable segment is the correct prefill for BOTH
    interruption shapes: segments are assembled from the LIVE task where
    ``task['content']`` holds ONLY the terminal round's prose (the accumulator
    is zeroed after each tool batch by ``_discard_pretool_prose``). So the
    terminal deliverable is exactly the trailing tail after the last completed
    tool batch (case 2), and for a no-tool turn it is the whole in-progress
    answer (case 3). The pre-tool prose of earlier batches is replayed
    separately via ``inject_tool_history`` — so prefilling the terminal
    deliverable never double-counts it.

    Args:
        segments: the persisted (thin) segment list, or None.
        model: target model id — gated via ``model_supports_assistant_prefill``
            (False for Claude → prefill removed / rejected; the fail-closed
            gate).
        finish_reason: authoritative finish reason from the message dict. Used
            when the persisted segment was assembled at a partial checkpoint
            (status='running', no finishReason yet) so its ``resumable`` flag
            was not stamped. When provided and resumable, it overrides.

    Returns:
        The prefill string, or ``None`` when prefill is unavailable/unwanted.
    """
    if not segments:
        return None
    # Lazy import: importing lib.model_info at module-load time creates a
    # circular import (its init chain is not yet complete when the segments
    # package is first imported during boot). It is a pure capability lookup.
    from lib.model_info import model_supports_assistant_prefill
    if not model_supports_assistant_prefill(model):
        return None  # Claude — fail closed (Messages API rejects the prefill)
    fr_resumable = (finish_reason or '') in RESUMABLE_FINISH_REASONS
    for s in segments:
        if s.get('type') == SEG_TEXT and s.get('terminal') and s.get('deliverable'):
            text = s.get('text') or ''
            if not text:
                return None
            if s.get('resumable') or fr_resumable:
                return text
            return None
    return None

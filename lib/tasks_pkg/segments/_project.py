"""lib/tasks_pkg/segments/_project.py — wire projections OFF segments.

These rebuild higher-level structures from the segment list:
``reconstruct_tool_messages_from_segments`` (assistant(tool_calls)+tool wire
messages) and ``tool_history_from_segments`` (the Continue ``toolHistory``
shape).

Pure functions; no Flask, no DB.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tool_results.tool_history_projection import build_tool_history_round
from lib.tool_results.tool_round_identity import tool_round_batches
from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix

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
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )
    rounds = _rounds_view_from_segments(segments)
    return _reconstruct_tool_call_messages(rounds)


def tool_history_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the `cfg['toolHistory']` shape (what `inject_tool_history`
    consumes on Continue) from the segment structure — one entry per llmRound
    batch: `{assistantContent, thinking, thinkingSignature, toolCalls[], toolResults[]}`.

    Lets a Continue rebuild be driven from persisted segments byte-identically
    to the frontend-supplied toolHistory (the step-4 parity gate).
    """
    replay_prefix = scan_replayable_tool_round_prefix(
        _rounds_view_from_segments(segments))
    rounds = list(replay_prefix.rounds)
    # ``llmRound`` is local to one execution attempt.  Delegating both batch
    # identity and shape to their shared owners prevents this compatibility
    # path from recreating the old global-dict merge after Continue.
    return [
        build_tool_history_round(batch)
        for batch in tool_round_batches(rounds)
    ]

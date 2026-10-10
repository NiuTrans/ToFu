"""Logical round frontier for one resumed conversation Turn.

Entry point: continuation_round_counters. Retained receipts are evidence, not
work to execute. Producer identities keep their audit meaning; legacy local
counter resets are folded in chronological order without rewriting history.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from lib.tool_round_identity import execution_identity, tool_round_batches


def continuation_round_counters(rounds: Sequence[Mapping]) -> tuple[int, int]:
    """Return (next zero-based model round, last allocated root tool ordinal).

    Model batches remain execution-scoped even though their display numbering
    is continuous. Gateway children have their own ordinal allocator and do
    not advance the root allocator. Sparse counters keep their gaps; unstamped
    historical resets advance from the already observed logical frontier.
    """
    next_model_round = 0
    model_scope = None
    model_offset = 0
    for batch in tool_round_batches(rounds):
        row = next((r for r in batch if isinstance(r, Mapping)
                    and type(r.get('llmRound')) is int
                    and r['llmRound'] >= 0), None)
        if row is None:
            continue
        scope = execution_identity(row)
        raw = row['llmRound']
        if scope != model_scope:
            model_offset = 0
        logical = max(raw + model_offset, next_model_round)
        model_offset = logical - raw
        next_model_round = logical + 1
        model_scope = scope

    last_tool_round = 0
    tool_scope = None
    tool_offset = 0
    for row in rounds:
        if not isinstance(row, Mapping) or not row.get('toolCallId'):
            continue
        if row.get('source') in {'execute_calls', 'execute_program'}:
            continue
        raw = row.get('roundNum')
        if type(raw) is not int or raw < 1:
            continue
        scope = execution_identity(row)
        if scope != tool_scope:
            tool_offset = 0
        logical = max(raw + tool_offset, last_tool_round + 1)
        tool_offset = logical - raw
        last_tool_round = logical
        tool_scope = scope
    return next_model_round, last_tool_round

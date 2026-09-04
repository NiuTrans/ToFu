"""Occurrence-safe pairing for adjacent assistant tool calls and results.

Responsibility
--------------
Interpret only the protocol-local assistant -> tool-result adjacency run.
Provider call ids are queue selectors inside that one run, never global
conversation keys: legacy positional-id models may legally reuse the same id
in later rounds.  Consumers use the returned object pairs instead of building
lossy ``call_id -> last call`` dictionaries.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any


def adjacent_tool_call_result_pairs(
    messages: Sequence[Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Return unambiguous call/result pairs in transcript order.

    Results must be contiguous after their owning assistant message.  Within
    that run, duplicate ids are paired by occurrence.  Blank ids, orphan
    results, malformed entries, and missing receipts remain unpaired so every
    authority/safety consumer fails closed instead of guessing across rounds.
    """
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    position = 0
    while position < len(messages):
        message = messages[position]
        if (not isinstance(message, Mapping)
                or message.get("role") != "assistant"):
            position += 1
            continue

        calls = message.get("tool_calls") or ()
        results_by_id: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        result_position = position + 1
        while result_position < len(messages):
            result = messages[result_position]
            if (not isinstance(result, Mapping)
                    or result.get("role") != "tool"):
                break
            call_id = str(result.get("tool_call_id") or "").strip()
            if call_id:
                results_by_id[call_id].append(result)
            result_position += 1

        for call in calls:
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("id") or "").strip()
            result_queue = results_by_id.get(call_id)
            if call_id and result_queue:
                pairs.append((call, result_queue.popleft()))

        position = max(position + 1, result_position)
    return pairs


__all__ = ["adjacent_tool_call_result_pairs"]

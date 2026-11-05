"""Plan a smaller source query using the shared model-view token counter.

This module returns only a byte/match limit. Its temporary page sketches never
leave planning: the handler must obtain and budget a fresh authority page before
delivery. No cursor is minted, decoded, or advanced here.
"""
from __future__ import annotations

from collections.abc import Callable


def plan_source_limit(fn_name: str, result: dict, token_budget: int, source_tokens: int,
                      count_page: Callable[[dict], int]) -> int | None:
    """Choose a useful prefix, allowing room for changed cursor/number encoding."""
    is_read = fn_name == 'read_tool_artifact'
    values = result.get('content') if is_read else result.get('items')
    if not isinstance(values, str if is_read else list) or len(values) <= 1:
        return None
    cursor = result.get('nextCursor') or result.get('resumeCursor')
    if not cursor:
        return None
    spans = result.get('provenance') if is_read else None
    if spans is not None:
        if not isinstance(spans, list) or any(
                not isinstance(span, dict)
                or type(span.get('start')) is not int
                or type(span.get('end')) is not int
                or span['start'] < 0 or span['end'] < span['start'] for span in spans):
            return None
        if sum(span['end'] - span['start'] for span in spans) != len(values.encode('utf-8')):
            return None
    partial = {**result, 'nextCursor': cursor, 'truncated': True}
    if partial.get('coverage') == 'complete':
        partial['coverage'] = 'incomplete'
    if is_read:
        partial.pop('sizeBytes', None)
    # Cursor signatures and integer widths may change at the source. Actual
    # returned pages are still measured; this margin is only a planning hint.
    planning_budget = max(1, token_budget - min(64, max(1, token_budget // 20)))

    def prefix(count):
        selected = values[:count]
        candidate = {**partial, 'content' if is_read else 'items': selected}
        extent = len(selected.encode('utf-8')) if is_read else count
        if spans is not None:
            remaining = extent
            selected_spans = []
            for span in spans:
                if remaining <= 0:
                    break
                taken = min(remaining, span['end'] - span['start'])
                if taken:
                    selected_spans.append({**span, 'end': span['start'] + taken})
                    remaining -= taken
            candidate['provenance'] = selected_spans
        return candidate, extent

    smallest, best = prefix(1)
    low_cost = count_page(smallest)
    if low_cost > planning_budget:
        return None
    low, high, high_cost = 1, len(values), source_tokens
    # A minimum-prefix probe plus at most three interpolated probes bounds
    # tokenizer work. The observed full-page cost is already available. Binary
    # search to the final character spent more CPU than the avoided queries.
    for _ in range(3):
        if high - low <= 1:
            break
        middle = low + (high - low) * (planning_budget - low_cost) // max(1, high_cost - low_cost)
        middle = max(low + 1, min(high - 1, middle))
        candidate, extent = prefix(middle)
        cost = count_page(candidate)
        if cost <= planning_budget:
            best, low, low_cost = extent, middle, cost
            if cost * 20 >= planning_budget * 19:
                return best
        else:
            high, high_cost = middle, cost
    # A poor estimate must not replace a useful source page with a tiny prefix.
    # Let the existing source-halving path handle irregular/unknown demand.
    return best if low_cost * 4 >= planning_budget * 3 else None

"""Launch-probed resident budgets owned by the tool execution domain.

Entry points
------------
``tool_search_term_cache_capacity`` resolves the process-wide Tool Search
tokenization cache. The catalog-index and sticky-selection capacities derive
from that same launch-time working-set signal. ``tool_result_cache_capacity``
resolves each task's live execution-receipt cache. Query/schema and receipt
semantics remain with their respective task/gateway owners.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Mapping

from runtime_guards import resolve_resource_budget


TOOL_SEARCH_TERM_CACHE_HARD_CAPACITY = 16_384
TOOL_SEARCH_CATALOG_INDEX_HARD_CAPACITY = 32
TOOL_SEARCH_SELECTION_STATE_HARD_CAPACITY = 4_096
TOOL_RESULT_CACHE_HARD_CAPACITY = 1_024


@lru_cache(maxsize=1)
def tool_search_term_cache_capacity() -> int:
    """Return the finite short-text tokenization working-set capacity."""
    return resolve_resource_budget(
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY',
        minimum=64,
        maximum=TOOL_SEARCH_TERM_CACHE_HARD_CAPACITY,
    )


def tool_search_catalog_index_capacity() -> int:
    """Return the finite count of content-addressed MCP catalog indexes."""
    term_capacity = tool_search_term_cache_capacity()
    return max(
        2,
        min(TOOL_SEARCH_CATALOG_INDEX_HARD_CAPACITY, term_capacity // 128),
    )


def tool_search_selection_state_capacity() -> int:
    """Return the finite owner/conversation sticky-selection working set."""
    term_capacity = tool_search_term_cache_capacity()
    return max(
        128,
        min(TOOL_SEARCH_SELECTION_STATE_HARD_CAPACITY, term_capacity * 2),
    )


def tool_result_cache_capacity(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the finite per-task execution-receipt working-set capacity."""
    return resolve_resource_budget(
        'TOFU_TOOL_RESULT_CACHE_CAPACITY',
        environment,
        minimum=16,
        maximum=TOOL_RESULT_CACHE_HARD_CAPACITY,
    )


__all__ = [
    'TOOL_RESULT_CACHE_HARD_CAPACITY',
    'TOOL_SEARCH_CATALOG_INDEX_HARD_CAPACITY',
    'TOOL_SEARCH_SELECTION_STATE_HARD_CAPACITY',
    'TOOL_SEARCH_TERM_CACHE_HARD_CAPACITY',
    'tool_result_cache_capacity',
    'tool_search_catalog_index_capacity',
    'tool_search_selection_state_capacity',
    'tool_search_term_cache_capacity',
]

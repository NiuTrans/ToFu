"""tofu_search.search — Multi-engine web search with dedup, reranking, vertical lookups."""

from tofu_search.search.deepen import is_deepen_enabled
from tofu_search.search.format import format_search_for_tool_response
from tofu_search.search.orchestrator import SearchResultList, perform_web_search
from tofu_search.search.vertical import (
    detect_vertical_intent,
    list_domains,
    search_vertical,
    search_vertical_domain,
)

__all__ = [
    'perform_web_search',
    'SearchResultList',
    'format_search_for_tool_response',
    'detect_vertical_intent',
    'search_vertical',
    'search_vertical_domain',
    'list_domains',
    'is_deepen_enabled',
]

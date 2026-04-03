"""lib/tools/search.py — Web search & fetch tool definitions."""

import logging

logger = logging.getLogger(__name__)

SEARCH_TOOL_SINGLE = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. "
            "You will receive summaries and partial content of top results. "
            "After reviewing, use fetch_url to read the most relevant 1-2 pages in full. "
            "IMPORTANT: You can only search ONCE, so make your query precise and specific."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Precise, specific search query. Use English for broader coverage."
                }
            },
            "required": ["query"]
        }
    }
}

SEARCH_TOOL_MULTI = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web. You may call this multiple times with different queries. "
            "Strategy: search → review summaries → fetch_url most relevant pages → "
            "refine with another search if needed. "
            "Prefer fewer, targeted searches over many broad ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and targeted"
                }
            },
            "required": ["query"]
        }
    }
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch and read the full content of a specific URL (HTML, PDF, plain text). "
            "Use this when the user pastes or mentions a URL they want you to read, "
            "or to deeply read pages you found promising from search results. "
            "You can call this multiple times for different URLs. "
            "When a page contains links to sub-pages (shown in '--- Page Links ---' section), "
            "you SHOULD use fetch_url to follow the most relevant links and explore deeper."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Complete URL starting with https://"
                }
            },
            "required": ["url"]
        }
    }
}

__all__ = ['SEARCH_TOOL_SINGLE', 'SEARCH_TOOL_MULTI', 'FETCH_URL_TOOL']

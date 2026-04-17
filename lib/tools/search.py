"""lib/tools/search.py — Web search & fetch tool definitions."""

from lib.log import get_logger

logger = get_logger(__name__)

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
            "Prefer fewer, targeted searches over many broad ones.\n"
            "For MULTIPLE searches in one call, provide a 'queries' array — "
            "each entry has {query}. All queries run concurrently. "
            "This is much faster than multiple separate web_search calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and targeted"
                },
                "queries": {
                    "type": "array",
                    "description": "Array of search queries (for batch mode). All queries run concurrently. Much faster than multiple separate web_search calls.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query"
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        }
    }
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch and read the full content of a remote URL (HTML, PDF, plain text) via HTTP/HTTPS. "
            "Use this when the user pastes or mentions a web URL they want you to read, "
            "or to deeply read pages you found promising from search results. "
            "You can call this multiple times for different URLs. "
            "When a page contains links to sub-pages (shown in '--- Page Links ---' section), "
            "you SHOULD use fetch_url to follow the most relevant links and explore deeper.\n"
            "IMPORTANT: This tool is for REMOTE web URLs only (http:// or https://). "
            "Do NOT use for local file paths or file:// URIs — use read_files with an absolute path instead.\n"
            "For MULTIPLE URLs in one call, provide a 'urls' array — "
            "each entry has {url}. All fetches run concurrently. "
            "This is much faster than multiple separate fetch_url calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Complete remote URL starting with http:// or https://"
                },
                "urls": {
                    "type": "array",
                    "description": "Array of URLs to fetch (for batch mode). All fetches run concurrently. Much faster than multiple separate fetch_url calls.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "Complete remote URL starting with http:// or https://"
                            }
                        },
                        "required": ["url"]
                    }
                }
            }
        }
    }
}

__all__ = ['SEARCH_TOOL_SINGLE', 'SEARCH_TOOL_MULTI', 'FETCH_URL_TOOL']

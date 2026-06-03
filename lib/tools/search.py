"""lib/tools/search.py — Web search & fetch tool definitions."""

from lib.log import get_logger

logger = get_logger(__name__)

SEARCH_TOOL_MULTI = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web. You may call this multiple times with different queries. "
            "You will receive summaries and partial content of the top results.\n\n"
            "**Recommended strategy: search → review the summaries first → fetch_url "
            "the 1-2 most promising pages in full → refine with another search only "
            "if needed.** Don't fetch every result; the summaries usually decide which "
            "pages are worth reading. Prefer fewer, targeted searches over many broad "
            "ones.\n\n"
            "For MULTIPLE searches in one call, provide a 'queries' array — each entry "
            "has ``{query, freshness?}``. All queries run concurrently and this is much "
            "faster than multiple separate web_search calls.\n\n"
            "**Vertical domain search**: Queries containing structured identifiers are "
            "auto-detected and enriched with data from specialized APIs:\n"
            "- CVE IDs (e.g. CVE-2024-1234) → NVD/NIST vulnerability data\n"
            "- arXiv IDs (e.g. 2301.07041) → paper metadata + abstract\n"
            "- DOIs (e.g. 10.1038/s41586-023-06221-2) → CrossRef citation data\n"
            "- Stock tickers (e.g. AAPL, $TSLA) → Yahoo Finance price data\n"
            "- PyPI packages (e.g. pypi:requests) → package info\n"
            "- npm packages (e.g. npm:express) → registry data\n"
            "- GitHub repos (e.g. github:facebook/react) → repo stats + README\n"
            "- IP addresses (e.g. 8.8.8.8) → geolocation + org info\n\n"
            "Vertical data is returned alongside regular web results automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and targeted"
                },
                "freshness": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "Time filter for results recency. Only use when the user explicitly wants recent results."
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
                            },
                            "freshness": {
                                "type": "string",
                                "enum": ["day", "week", "month", "year"],
                                "description": "Time filter for this specific query"
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

__all__ = ['SEARCH_TOOL_MULTI', 'FETCH_URL_TOOL']

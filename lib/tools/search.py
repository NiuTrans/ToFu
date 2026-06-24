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
            "has ``{query, freshness?, vertical?}``. All queries run concurrently and "
            "this is much faster than multiple separate web_search calls. NOTE: when "
            "'queries' is present the top-level 'query' is IGNORED — use one or the "
            "other, not both.\n\n"
            "**Vertical domain search**: Queries containing structured identifiers are "
            "auto-detected and enriched with data from specialized APIs:\n"
            "- CVE IDs (e.g. CVE-2024-1234) → NVD/NIST vulnerability data\n"
            "- arXiv IDs (e.g. 2301.07041) → paper metadata + abstract\n"
            "- DOIs (e.g. 10.1038/s41586-023-06221-2) → CrossRef citation data\n"
            "- Stock tickers (e.g. AAPL, $TSLA) → Yahoo Finance price data\n"
            "- PyPI packages (e.g. pypi:requests) → package info\n"
            "- npm packages (e.g. npm:express) → registry data\n"
            "- GitHub repos (e.g. github:facebook/react) → repo stats + README\n"
            "- IP addresses (e.g. 8.8.8.8) → geolocation + org info\n"
            "- Trending AI papers (e.g. 'hf daily papers', 'trending papers this week', "
            "'daily papers on diffusion models') → Hugging Face curated papers ranked by "
            "upvotes; supports day/week/month windows\n"
            "- Related work / citations (e.g. 'papers related to Mamba', 'what cites "
            "2312.00752') → Semantic Scholar relevance + citation graph\n\n"
            "Vertical data is returned alongside regular web results automatically. "
            "('freshness' filters only the WEB results, best-effort per engine; it does "
            "NOT change the Hugging Face time window — that comes from query phrasing "
            "like 'this week'.)\n\n"
            "**Explicit vertical**: pass ``vertical='academic'`` (or another domain) to "
            "FORCE a domain-level vertical search regardless of phrasing.\n"
            "- ``academic`` — works with FREE-TEXT topics. The dispatcher adapts to the "
            "query: an arXiv ID → paper metadata + Semantic Scholar citations; a DOI → "
            "CrossRef; 'trending/daily' phrasing → Hugging Face Papers; 'related to / "
            "citing X' → Semantic Scholar; otherwise free-text → Hugging Face keyword "
            "search + Semantic Scholar related work (run in parallel).\n"
            "- ``code`` — needs a package/repo IDENTIFIER, not a free-text concept "
            "(tries PyPI + npm + GitHub for that exact name; 'best react libraries' "
            "returns nothing — use a plain web search for discovery).\n"
            "- ``finance`` — needs a ticker symbol (e.g. AAPL).\n"
            "- ``security`` — needs a CVE ID (e.g. CVE-2024-1234).\n"
            "- ``network`` — needs an IP address.\n"
            "- ``auto`` (default) → phrase-detect from query.\n"
            "- ``off`` → web only, skip vertical entirely.\n"
            "Rule of thumb: use ``academic`` for any research/paper query; the other "
            "explicit domains only help when the query already IS an identifier (and "
            "``auto`` would catch those anyway)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and targeted. Single-search mode; omit when using 'queries'."
                },
                "freshness": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "Best-effort time filter on the WEB results only (some engines ignore it; does not affect vertical sources). Only use when the user explicitly wants recent results."
                },
                "vertical": {
                    "type": "string",
                    "enum": ["auto", "academic", "code", "finance", "security", "network", "off"],
                    "description": "Force a vertical data source. 'auto' (default) phrase-detects. 'academic' accepts free-text topics (papers/citations). 'code'/'finance'/'security'/'network' only work when the query already IS an identifier (package name / ticker / CVE ID / IP) — not for free-text discovery. 'off' = web only."
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
                            },
                            "vertical": {
                                "type": "string",
                                "enum": ["auto", "academic", "code", "finance", "security", "network", "off"],
                                "description": "Force a vertical data source for this query. 'academic' = free-text papers/citations; 'code'/'finance'/'security'/'network' need an identifier; 'off' = web only."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        }
    }
}

def _build_fetch_url_tool():
    """Build the fetch_url tool schema.

    The single-URL ``reason`` parameter and the content-filter prose are only
    exposed when the LLM content filter is actually enabled
    (``FETCH_LLM_FILTER``, default on). With the filter off, ``reason`` is a
    pure no-op (see lib/fetch/content_filter.py), so advertising it would just
    invite the model to waste tokens — we omit it entirely instead.
    """
    import os
    filter_on = os.environ.get('FETCH_LLM_FILTER', '1') == '1'

    description = (
        "Fetch and read the full content of a remote URL (HTML, PDF, plain text) via HTTP/HTTPS. "
        "Use this when the user pastes or mentions a web URL they want you to read, "
        "or to deeply read pages you found promising from search results. "
        "You can call this multiple times for different URLs. "
        "When a page contains links to sub-pages (shown in '--- Page Links ---' section), "
        "you SHOULD use fetch_url to follow the most relevant links and explore deeper.\n"
        "IMPORTANT: This tool is for REMOTE web URLs only (http:// or https://). "
        "Do NOT use for local file paths or file:// URIs — use read_files with an absolute path instead.\n"
        "If the URL points to a file asset rather than a web page (e.g. an SVG, image, "
        "archive, font or Office document), it is handled automatically: text-like assets "
        "(SVG/JSON/source code) are returned inline, while binary assets are downloaded to a "
        "local staging path and the response tells you the path to open with read_files.\n"
    )
    if filter_on:
        description += (
            "Large HTML pages (>~3000 chars) pass through a cheap LLM cleaner that "
            "strips boilerplate (nav/ads/banners) and applies a relevance GATE keyed "
            "on your 'reason': a page that doesn't help is dropped and returns 'Failed "
            "to fetch'. 'reason' decides keep-vs-drop for the whole page (be accurate, "
            "not narrow) — it never trims to matching parts or summarizes. PDFs, short "
            "pages, and batch fetches are not filtered. Content is capped (large "
            "pages/PDFs are truncated).\n"
        )
    else:
        description += (
            "Content is returned as raw extracted text (large pages/PDFs are truncated).\n"
        )
    description += (
        "For MULTIPLE URLs in one call, provide a 'urls' array — each entry has "
        "{url}. All fetches run concurrently and this is much faster than multiple "
        "separate fetch_url calls. NOTE: when 'urls' is present the top-level 'url' "
        "is IGNORED" + ("; 'reason' applies to single-URL mode only." if filter_on else ".")
    )

    properties = {
        "url": {
            "type": "string",
            "description": "Complete remote URL starting with http:// or https://. Single-fetch mode; omit when using 'urls'."
        },
    }
    if filter_on:
        properties["reason"] = {
            "type": "string",
            "description": "What you're looking for on this page (single-URL mode). Used as the keep-vs-drop relevance gate for large HTML pages — be accurate, not narrow (it decides whether the whole page is kept, not which parts)."
        }
    properties["urls"] = {
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

    return {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": description,
            "parameters": {"type": "object", "properties": properties},
        }
    }


FETCH_URL_TOOL = _build_fetch_url_tool()

__all__ = ['SEARCH_TOOL_MULTI', 'FETCH_URL_TOOL']

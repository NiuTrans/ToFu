"""lib/tools/search.py — Web search & fetch tool definitions."""

from lib.log import get_logger

logger = get_logger(__name__)


def _vertical_domains() -> list[dict]:
    """Capability metadata for every currently-usable vertical domain.

    Sourced from tofu_search rather than restated here: a domain whose
    credential is missing must not appear in the enum. Before the optional
    runtime's first real use, return a conservative ``auto``/``off`` schema
    instead of importing the whole search/fetch stack merely to build a prompt.
    Auto-detection remains available; later turns gain the runtime-derived
    explicit domain enum once the library is resident.
    """
    from lib.search_runtime import search_library_is_loaded
    if not search_library_is_loaded():
        return []
    try:
        from tofu_search.search.vertical import describe_domains
        return describe_domains()
    except Exception as e:
        logger.warning('[Tools] vertical capability lookup failed: %s', e)
        return []


def _vertical_enum(domains: list[dict]) -> list[str]:
    return ['auto'] + [d['domain'] for d in domains] + ['off']


def _render_vertical_section(domains: list[dict]) -> str:
    """Render the explicit-vertical prose from capability metadata."""
    if not domains:
        return ''
    lines = ["**Explicit vertical**: pass ``vertical='<domain>'`` to FORCE a "
             "domain-level vertical search regardless of phrasing.\n"]
    for d in domains:
        line = f"- ``{d['domain']}`` — {d.get('purpose', '')} {d.get('when_to_use', '')}".rstrip()
        examples = d.get('examples') or []
        if examples:
            line += ' Examples: ' + '; '.join(f'"{e}"' for e in examples[:3]) + '.'
        # A partially-available domain must say so, or the model will ask it for
        # the capability that is switched off and come back empty-handed.
        gap = [u['type'] for u in (d.get('unavailable_types') or [])]
        if gap and d.get('available_types'):
            line += (f" NOTE: only {', '.join(d['available_types'])} is available "
                     f"right now; {', '.join(gap)} needs "
                     f"{d.get('credential_env') or 'a credential'} to be configured "
                     f"— do NOT use this domain for {', '.join(gap)} queries.")
        lines.append(line)
    lines.append("- ``auto`` (default) → phrase-detect from query.")
    lines.append("- ``off`` → web only, skip vertical entirely.")
    return '\n'.join(lines)


def build_search_tool() -> dict:
    """Build the web_search tool schema.

    Built per call, NOT cached at import: the set of available vertical domains
    depends on runtime credentials, so a module-level constant would freeze
    whatever was configured when the process started.
    """
    domains = _vertical_domains()
    vertical_enum = _vertical_enum(domains)
    vertical_section = _render_vertical_section(domains)
    return {
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
            + vertical_section
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
                    "enum": vertical_enum,
                    "description": "Force a vertical data source. 'auto' (default) phrase-detects. 'off' = web only. See the tool description for what each domain covers and which need an identifier."
                },
                "queries": {
                    "type": "array",
                    "description": "Array of search queries (for batch mode). All queries run concurrently. Much faster than multiple separate web_search calls. Each element MUST be an object like {\"query\": \"...\"} — never a single bare string or a concatenation of queries; for one search use the top-level 'query' field instead.",
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
                                "enum": vertical_enum,
                                "description": "Force a vertical data source for this query. 'off' = web only. See the tool description for per-domain coverage."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        }
    }
}

def build_fetch_url_tool() -> dict:
    """Build the fetch_url tool schema.

    The single-URL ``reason`` parameter and the content-filter prose are only
    exposed when the LLM content filter is actually enabled. The source of
    truth is the RUNTIME flag ``lib.LLM_CONTENT_FILTER_ENABLED`` — the
    Settings toggle hot-applied by routes/config.py — NOT the
    ``FETCH_LLM_FILTER`` env var, which only seeds the flag's default at
    import time (reading env here left this schema stale after a Settings
    toggle). Built per call by the consumers for the same reason: a
    module-level snapshot freezes whatever was set at import.
    """
    import lib as _lib
    filter_on = bool(getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True))

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
        "(SVG/JSON/source code) are returned inline, while binary assets are transferred to a "
        "server staging path and the response tells you the path to open with read_files. "
        "If the server lacks the site's login but the selected browser has it, bytes stream "
        "from that authenticated browser session to server staging; they are never silently "
        "saved in the browser device's Downloads folder. Server staging is temporary and is "
        "NOT the user's requested final destination: when they named a project/path, perform "
        "a separate authorized filesystem copy/move from the returned staging path, and only "
        "claim success after that destination operation returns its own receipt.\n"
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
        "description": "Array of URLs to fetch (for batch mode). All fetches run concurrently. Much faster than multiple separate fetch_url calls. Each element MUST be an object like {\"url\": \"...\"} — never a single bare string; for one URL use the top-level 'url' field instead.",
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


def build_download_url_to_server_tool() -> dict:
    """Build the explicit remote-URL → server-staging contract.

    ``fetch_url`` remains the page-reading tool and may discover a binary
    response incidentally.  This tool is the unambiguous entry point when the
    requested outcome is a file on the Tofu server.  Transport selection is an
    implementation detail: server HTTP is tried first, then the selected
    browser session streams the response when its login/network is required.
    """
    return {
        "type": "function",
        "function": {
            "name": "download_url_to_server",
            "description": (
                "Download one http:// or https:// URL into temporary storage ON "
                "THE TOFU SERVER. Use this whenever the user asks to download, "
                "save, install, unzip, or copy a remote file on the server or into "
                "a server-side project. The tool automatically tries safe server "
                "HTTP first and, when the server cannot reach/authenticate the URL, "
                "streams the response through the user's selected logged-in browser "
                "to server staging. Cookies remain inside Chrome: NEVER call "
                "browser_get_cookies and replay them with curl/wget. This tool does "
                "not use Chrome Downloads and never reports a device-local file as a "
                "server file. Success returns location=server_staging, an absolute "
                "path, byte size, SHA-256, and transport. Staging is temporary; if "
                "the user named a final project path, perform a separate authorized "
                "filesystem copy/move and verify that destination before claiming the "
                "whole task is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "Exact remote file URL starting with http:// or https://. "
                            "Signed query parameters are accepted and are not logged."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }


# Boot-time snapshot for static capability listing (routes/api_v1/capabilities.py).
# Per-request consumers must call build_fetch_url_tool() instead — see its docstring.
FETCH_URL_TOOL = build_fetch_url_tool()
DOWNLOAD_URL_TO_SERVER_TOOL = build_download_url_to_server_tool()

__all__ = [
    'build_search_tool', 'build_fetch_url_tool',
    'build_download_url_to_server_tool', 'FETCH_URL_TOOL',
    'DOWNLOAD_URL_TO_SERVER_TOOL',
]
# Boot-time snapshot for static capability listing (routes/api_v1/capabilities.py).
# Per-request consumers must call build_fetch_url_tool() instead — see its docstring.
FETCH_URL_TOOL = build_fetch_url_tool()


def build_update_search_settings_tool() -> dict:
    """Build the update_search_settings tool schema.

    STATIC description (prompt-cache contract): current values are NOT baked
    in — the model reads them by calling the tool with no arguments, which is
    a pure read. Writes go through lib.search_settings.apply_updates
    (validate → clamp → persist → hot-reload → audit), never through raw
    file edits.
    """
    return {
        "type": "function",
        "function": {
            "name": "update_search_settings",
            "description": (
                "Read or adjust the SERVER-WIDE web search & fetch pipeline settings "
                "(the same knobs shown in Settings → Search). Changes apply globally, "
                "persist across restarts, and take effect immediately (hot reload) — "
                "they affect every conversation, so change them only when the user "
                "asks or when search/fetch results are demonstrably hurt by the current "
                "values (e.g. repeated fetch timeouts, too-shallow results).\n\n"
                "Call with NO arguments to read the current effective values first — "
                "always do this before proposing a change.\n\n"
                "Adjustable knobs (all optional; integers are clamped to safe ranges):\n"
                "- profile (fast/balanced/deep): 3 pages/30k/no filter; "
                "6 pages/60k/gate; or 10 pages/100k/gate+link deepening.\n"
                "- overrides: optional profile-key overrides; legacy concrete knobs "
                "below remain accepted and are stored as overrides.\n"
                "- fetch_top_n (1-20): pages auto-fetched per search. Higher = more "
                "complete, slower, more tokens.\n"
                "- fetch_timeout (5-120s): per-page fetch timeout.\n"
                "- max_chars_search / max_chars_direct: content caps for search-result "
                "pages / direct fetch_url calls.\n"
                "- max_chars_pdf (0 = unlimited): PDF text cap.\n"
                "- max_download_mb (>0, MB): max download size. Use MB, NOT bytes.\n"
                "- llm_content_filter (boolean): when ON, a cheap LLM strips nav/ads "
                "from fetched pages (better quality, slower); OFF returns raw text "
                "(faster, cheaper, noisier).\n"
                "- block_domain / unblock_domain: add/remove a domain in the fetch "
                "blocklist (e.g. block_domain='pinterest.com' when results keep "
                "surfacing useless pages from one host).\n\n"
                "The result reports what was applied, what was rejected (with reasons), "
                "env-var overrides that shadow a saved change, and the new effective "
                "values. Report the outcome to the user in plain language."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string", "enum": ["fast", "balanced", "deep"],
                        "description": "Search depth preset."
                    },
                    "overrides": {
                        "type": "object",
                        "description": "Optional custom overrides on top of the preset.",
                        "properties": {
                            "fetch_top_n": {"type": "integer"},
                            "max_chars_search": {"type": "integer"},
                            "llm_content_filter": {"type": "boolean"},
                            "deepen_enabled": {"type": "boolean"}
                        }
                    },
                    "fetch_top_n": {
                        "type": "integer",
                        "description": "Pages auto-fetched per search (1-20)."
                    },
                    "fetch_timeout": {
                        "type": "integer",
                        "description": "Per-page fetch timeout in seconds (5-120)."
                    },
                    "max_chars_search": {
                        "type": "integer",
                        "description": "Max characters kept from a search-result page (1000-500000)."
                    },
                    "max_chars_direct": {
                        "type": "integer",
                        "description": "Max characters kept from a direct fetch_url page (1000-1000000)."
                    },
                    "max_chars_pdf": {
                        "type": "integer",
                        "description": "Max characters kept from a PDF (0 = unlimited, up to 2000000)."
                    },
                    "max_download_mb": {
                        "type": "integer",
                        "description": "Max download size in MEGABYTES (>0, ≤500) — converted to bytes server-side."
                    },
                    "llm_content_filter": {
                        "type": "boolean",
                        "description": "ON = LLM strips boilerplate from fetched pages; OFF = raw text (faster)."
                    },
                    "block_domain": {
                        "type": "string",
                        "description": "Add a domain to the fetch blocklist (normalized: scheme/www/path stripped)."
                    },
                    "unblock_domain": {
                        "type": "string",
                        "description": "Remove a domain from the fetch blocklist."
                    }
                }
            }
        }
    }


__all__ = [
    'build_search_tool', 'build_fetch_url_tool',
    'build_download_url_to_server_tool', 'FETCH_URL_TOOL',
    'DOWNLOAD_URL_TO_SERVER_TOOL', 'build_update_search_settings_tool',
]

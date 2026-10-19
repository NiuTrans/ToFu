"""lib/tools/search.py — Web search & fetch tool definitions."""

from lib.log import get_logger

logger = get_logger(__name__)

_VERTICAL_PURPOSE_MAX_CHARS = 96


def _vertical_domains() -> list[dict]:
    """Capability metadata for every currently-usable vertical domain.

    Sourced from tofu_search rather than restated here: a domain whose
    credential is missing must not appear in the enum. Before the optional
    runtime's first real use, return a conservative ``auto``/``off`` schema
    instead of importing the whole search/fetch stack merely to build a prompt.
    Auto-detection remains available; later turns gain the runtime-derived
    explicit domain enum once the library is resident.
    """
    from lib.search.search_runtime import search_library_is_loaded
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
    """Render a bounded explicit-vertical summary from capability metadata.

    The main tool description already owns identifier rules and examples.
    Repeating upstream ``when_to_use`` text plus three examples per domain made
    every search-capable request pay for the same guidance twice.  Keep the
    capability-specific purpose and partial-availability warning here, while
    bounding upstream prose so runtime activation cannot inflate the schema
    without limit.
    """
    if not domains:
        return ''
    lines = [
        "**Explicit vertical**: set ``vertical='<domain>'`` to force one "
        "source."
    ]
    for d in domains:
        purpose = ' '.join(str(d.get('purpose') or '').split())
        if len(purpose) > _VERTICAL_PURPOSE_MAX_CHARS:
            purpose = (
                purpose[:_VERTICAL_PURPOSE_MAX_CHARS - 1].rstrip() + '…'
            )
        line = f"- ``{d['domain']}``"
        if purpose:
            line += f" — {purpose}"
        # A partially-available domain must say so, or the model will ask it for
        # the capability that is switched off and come back empty-handed.
        gap = [u['type'] for u in (d.get('unavailable_types') or [])]
        if gap and d.get('available_types'):
            line += (f" NOTE: only {', '.join(d['available_types'])} is available "
                     f"right now; {', '.join(gap)} needs "
                     f"{d.get('credential_env') or 'a credential'} to be configured "
                     f"— do NOT use this domain for {', '.join(gap)} queries.")
        lines.append(line)
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
    """Build the canonical, runtime-state-independent fetch_url schema."""
    description = (
        "Read remote HTTP(S) HTML/PDF/text; follow relevant Page Links. "
        "Local paths/file://: read_files. Text supports artifact paging; "
        "binaries return server-staging paths. If HTTP lacks login, "
        "the selected browser streams bytes to staging, never browser Downloads. "
        "Use an authorized filesystem tool to copy/move and verify receipt. "
        "Large HTML relevance GATE: reason keeps/drops the whole "
        "page (Failed to fetch); it does not select passages. Use a broad "
        "accurate reason (ignored when the gate is disabled). PDFs, short pages "
        "and batches bypass the gate. Concurrent batch urls:[{url}]: url ignored; "
        "reason/extract: single mode. extract returns quote-backed findings "
        "and unresolved requests from bounded windows; gaps do not prove absence. "
        "Raw source stays artifact-readable; failure returns raw."
    )

    properties = {
        "url": {
            "type": "string",
            "description": (
                "Single HTTP(S) URL; omit with urls."
            ),
        },
    }
    properties["reason"] = {
        "type": "string",
        "description": (
            "Whole-page relevance goal."
        ),
    }
    properties["extract"] = {
        "type": "string",
        "description": (
            "Question; bounded extraction, single mode."
        ),
    }
    properties["urls"] = {
        "type": "array",
        "description": (
            "Concurrent batch of {url} objects; url ignored."
        ),
        "items": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Remote HTTP(S) URL."
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


def build_browser_download_url_to_server_tool() -> dict:
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
            "name": "browser_download_url_to_server",
            "description": (
                "Stage one remote file ON THE TOFU SERVER for download/save/install/"
                "unzip/export/copy. Supply its exact HTTP(S) url. text/selector "
                "(+ optional tab_id) instead names an <a href> or "
                "data-download-url element whose full signed link is recovered; "
                "action buttons have no link — use browser_click. It tries "
                "bounded server HTTP first, then the selected logged-in browser; "
                "cookies stay in Chrome—never use browser_get_cookies, curl/wget, "
                "or browser Downloads, and never call a device-local file "
                "server-side. Success: location=server_staging, absolute path, "
                "byte size, SHA-256, transport. Staging is temporary; a named "
                "final project path needs a separately authorized copy/move and "
                "verified destination."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                        "description": "Exact HTTP(S) file URL; keep its signed query.",
                    },
                    "tab_id": {
                        "type": "integer",
                        "description": "Open-page tab; omit for the working tab.",
                    },
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "Visible link/button text.",
                    },
                    "selector": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2048,
                        "description": "Exact link CSS selector; wins over text.",
                    },
                },
                "anyOf": [
                    {
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                    {
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    {
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    },
                ],
                "additionalProperties": False,
            },
        },
    }


# Boot-time snapshot for static capability listing (routes/api_v1/capabilities.py).
# Per-request consumers must call build_fetch_url_tool() instead — see its docstring.
FETCH_URL_TOOL = build_fetch_url_tool()
BROWSER_DOWNLOAD_URL_TO_SERVER_TOOL = build_browser_download_url_to_server_tool()
# Python compatibility only. Both aliases build/point at the single canonical
# browser-prefixed wire schema; the legacy function name is never exposed.
def build_download_url_to_server_tool() -> dict:
    return build_browser_download_url_to_server_tool()


DOWNLOAD_URL_TO_SERVER_TOOL = BROWSER_DOWNLOAD_URL_TO_SERVER_TOOL


def build_update_search_settings_tool() -> dict:
    """Build the update_search_settings tool schema.

    STATIC description (prompt-cache contract): current values are NOT baked
    in — the model reads them by calling the tool with no arguments, which is
    a pure read. Writes go through lib.search.search_settings.apply_updates
    (validate → clamp → persist → hot-reload → audit), never through raw
    file edits.
    """
    return {
        "type": "function",
        "function": {
            "name": "update_search_settings",
            "description": (
                "Read or change SERVER-WIDE web search/fetch settings. No arguments is "
                "a PURE READ: always inspect current effective values before proposing "
                "a change. Writes persist across restarts, hot-reload immediately, and "
                "affect every conversation; write only when the user asks or repeated "
                "results prove current values harmful. Integers clamp to safe ranges.\n"
                "Profiles: fast=3 pages/30k chars/no LLM filter; balanced=6/60k/filter; "
                "deep=10/100k/filter+link deepening. overrides customizes a profile; "
                "legacy concrete knobs remain accepted and are stored as overrides.\n"
                "Trade-offs: fetch_top_n adds coverage, latency, and tokens; "
                "fetch_timeout is seconds; max_download_mb is MB, not bytes; "
                "llm_content_filter=true uses a slower selective LLM gate, false uses "
                "faster/cheaper lexical ranking. block_domain/unblock_domain edit the "
                "normalized fetch blocklist.\n"
                "The result reports applied and rejected values with reasons, env "
                "overrides shadowing saved values, and the new effective state. Explain "
                "the outcome to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string", "enum": ["fast", "balanced", "deep"],
                        "description": "Depth preset."
                    },
                    "overrides": {
                        "type": "object",
                        "description": "Custom values layered on the preset.",
                        "properties": {
                            "fetch_top_n": {"type": "integer"},
                            "llm_content_filter": {"type": "boolean"},
                            "deepen_enabled": {"type": "boolean"}
                        }
                    },
                    "fetch_top_n": {
                        "type": "integer",
                        "description": "Pages fetched per search (1-20)."
                    },
                    "fetch_timeout": {
                        "type": "integer",
                        "description": "Per-page fetch timeout in seconds (5-120)."
                    },
                    "max_download_mb": {
                        "type": "integer",
                        "description": "Download cap in MB (>0, max 500), not bytes."
                    },
                    "llm_content_filter": {
                        "type": "boolean",
                        "description": "true=LLM relevance gate; false=local lexical ranking."
                    },
                    "block_domain": {
                        "type": "string",
                        "description": "Block a normalized fetch domain."
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
    'build_browser_download_url_to_server_tool',
    'build_download_url_to_server_tool', 'FETCH_URL_TOOL',
    'BROWSER_DOWNLOAD_URL_TO_SERVER_TOOL', 'DOWNLOAD_URL_TO_SERVER_TOOL',
    'build_update_search_settings_tool',
]

"""tofu-search — Standalone multi-engine web search with LLM content filtering.

Provides a 7-step search pipeline: multi-engine search, URL dedup,
content dedup, concurrent page fetch, optional LLM content filter,
BM25 reranking, and result formatting.

Usage::

    from tofu_search import search, fetch_url, configure

    # Basic search (no LLM filter — still useful):
    results = search("Python asyncio tutorial")

    # With OpenAI-compatible LLM for content filtering:
    configure(
        llm_api_key="sk-...",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
    )
    results = search("Python asyncio tutorial")

    # With custom LLM callable:
    def my_llm(messages, **kwargs):
        return "your response"
    configure(llm_function=my_llm)
    results = search("Python asyncio tutorial")

    # Fetch a single URL:
    content = fetch_url("https://example.com")

    # Format results for display:
    text = format_results(results)
"""

# Single source of truth: the version lives ONLY in pyproject.toml and is read
# back from the installed distribution metadata. Hard-coding it here too meant
# two places to bump, and one release shipped with them out of sync.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version('tofu-search')
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = '0.0.0.dev0'

from tofu_search.config import SearchConfig, configure, get_config
from tofu_search.fetch.core import (
    extract_urls_from_text,
    fetch_page_content,
    fetch_url_bytes,
    fetch_urls,
)
from tofu_search.fetch.readers import SiteReader, register_reader
from tofu_search.fetch.utils import looks_like_text_asset
from tofu_search.providers import (
    AuthSourceProvider,
    BrowserProvider,
    SiteKnowledgeProvider,
    SiteSearchProvider,
    clear_site_drift_listeners,
    get_site_search_provider,
    normalize_site_search_results,
    register_auth_source_provider,
    register_browser_provider,
    register_site_drift_listener,
    register_site_knowledge_provider,
    register_site_search_provider,
)
from tofu_search.search.format import format_search_for_tool_response as format_results
from tofu_search.search.orchestrator import perform_web_search
from tofu_search.search.vertical import (
    describe_domains,
    detect_vertical_intent,
    list_domains,
    search_vertical,
    search_vertical_domain,
)
from tofu_search.verify import (
    parse_bibtex,
    parse_references,
    summarize,
    verify_bibtex,
    verify_citations,
    verify_references,
)

__all__ = [
    'search',
    'fetch_url',
    'configure',
    'get_config',
    'SearchConfig',
    'perform_web_search',
    'format_results',
    'fetch_urls',
    'fetch_page_content',
    'fetch_url_bytes',
    'looks_like_text_asset',
    'extract_urls_from_text',
    # Vertical (structured-identifier) search
    'detect_vertical_intent',
    'search_vertical',
    'search_vertical_domain',
    'list_domains',
    'describe_domains',
    # Citation verification (reference hallucination detection)
    'verify_bibtex',
    'verify_references',
    'verify_citations',
    'parse_bibtex',
    'parse_references',
    'summarize',
    # Provider seams (host integration)
    'BrowserProvider',
    'SiteSearchProvider',
    'AuthSourceProvider',
    'SiteKnowledgeProvider',
    'register_browser_provider',
    'register_site_search_provider',
    'get_site_search_provider',
    'normalize_site_search_results',
    'register_auth_source_provider',
    'register_site_knowledge_provider',
    'register_site_drift_listener',
    'clear_site_drift_listeners',
    # Site-reader tier (public-endpoint handlers, e.g. x.com syndication)
    'SiteReader',
    'register_reader',
]


def search(query: str, *, max_results: int | None = None,
           user_question: str = '', **kwargs) -> list[dict]:
    """Search the web and return processed results.

    This is the primary public API. Runs the full 7-step pipeline:
    multi-engine search → URL dedup → content dedup → page fetch →
    LLM content filter (if configured) → BM25 rerank → format.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
                     Default: 6 (configurable via configure(fetch_top_n=N)).
        user_question: The user's original question (helps LLM filter judge
                       relevance). If not provided, query is used.
        **kwargs: Additional SearchConfig overrides for this call only.

    Returns:
        List of result dicts, each with keys:
        - title (str): Page title
        - url (str): Page URL
        - snippet (str): Search result snippet
        - source (str): Search engine name
        - full_content (str, optional): Fetched and cleaned page content

    Example::

        results = search("Python asyncio tutorial")
        for r in results:
            print(f"{r['title']}: {r['url']}")
            if r.get('full_content'):
                print(f"  Content: {r['full_content'][:200]}...")
    """
    config = None
    if kwargs:
        config = get_config().copy(**kwargs)

    return perform_web_search(
        query,
        max_results=max_results,
        user_question=user_question or query,
        config=config,
    )


def fetch_url(url: str, *, max_chars: int | None = None,
              timeout: int | None = None) -> str | None:
    """Fetch and extract text content from a single URL.

    Args:
        url: URL to fetch.
        max_chars: Max characters of extracted text. Default: 200,000.
        timeout: Request timeout in seconds. Default: 15.

    Returns:
        Extracted text content string, or None if fetch failed.

    Example::

        content = fetch_url("https://example.com")
        if content:
            print(f"Got {len(content)} chars")
    """
    cfg = get_config()
    if max_chars is None:
        max_chars = cfg.fetch_max_chars_direct
    return fetch_page_content(url, max_chars=max_chars, timeout=timeout)

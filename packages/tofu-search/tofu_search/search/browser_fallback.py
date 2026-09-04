"""tofu_search.search.browser_fallback — Host-browser fallback for web search.

When all server-side engines fail (network blocked), a host that registered a
:class:`tofu_search.providers.BrowserProvider` can run the search through the
user's own browser. No-op (returns []) when no provider is registered.

Two integration modes, in order of preference:

  1. **``fetch_html``** — the host browser fetches the raw DuckDuckGo SERP and
     the LIBRARY parses it (same bs4 parser the server-side DDG engine uses).
     This keeps result-parsing inside tofu-search; the host only supplies a
     dumb "fetch this URL's HTML" capability.
  2. **``search``** — legacy last resort: the host performs the whole search
     (fetch + parse) and hands back result dicts. Used only when ``fetch_html``
     is unavailable / returns nothing.
"""

from urllib.parse import quote_plus

from tofu_search.log import get_logger
from tofu_search.providers import get_browser_provider
from tofu_search.search.engines.ddg import parse_ddg_html_text

logger = get_logger(__name__)

__all__ = ['search_via_browser']

_DDG_HTML_URL = 'https://html.duckduckgo.com/html/?q='


def search_via_browser(query, max_results=8):
    """Fall back to the host browser provider for web search.

    Returns a list of ``{title, snippet, url, source}`` dicts, or [] when no
    browser provider is registered / connected or the search yields nothing.
    """
    provider = get_browser_provider()
    if provider is None:
        return []
    try:
        if not provider.is_connected():
            logger.info('[Search] Browser search fallback skipped (provider not connected) query=%r',
                        query[:80])
            return []
        logger.info('[Search] Browser search fallback TRIGGERED (all engines failed) query=%r',
                    query[:80])

        # ── Mode 1: host fetches raw HTML, library parses (preferred) ──
        serp_url = _DDG_HTML_URL + quote_plus(query)
        try:
            html = provider.fetch_html(serp_url, timeout=25)
        except Exception as e:
            logger.warning('[Search] Browser fetch_html failed (%s) — trying host search()', e)
            html = None
        if html:
            results = parse_ddg_html_text(html, max_results=max_results,
                                          source='DuckDuckGo (via browser)')
            logger.info('[Search] Browser fetch_html -> parsed %d results (%d HTML chars)',
                        len(results), len(html))
            if results:
                return results
            logger.info('[Search] Browser fetch_html returned HTML but 0 parseable results — '
                        'falling back to host search()')

        # ── Mode 2: host performs the whole search (legacy) ──
        results = provider.search(query, max_results=max_results) or []
        logger.info('[Search] Browser search() got %d results', len(results))
        return results
    except Exception as e:
        logger.error('[Search] Browser search fallback failed: %s', e, exc_info=True)
        return []

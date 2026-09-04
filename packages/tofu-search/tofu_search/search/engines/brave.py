"""tofu_search/search/engines/brave.py — Brave Search HTML scraping.

Brave has its own independent web index (not recycled Bing/Google),
making it the best diversity complement to DDG.
"""

import re
from html import unescape

from tofu_search.log import get_logger
from tofu_search.search._common import http_search_get, make_result, soup_of

logger = get_logger(__name__)

__all__ = ['search_brave']

_AGO_PREFIX_RE = re.compile(r'^\d+\s+\w+\s+ago\s*[-–—]\s*')


def _parse_brave(resp):
    """Parse Brave HTML response into result dicts (bs4 CSS selectors)."""
    results = []
    soup = soup_of(resp.text)
    # Each organic result lives in a [data-pos] container.
    for block in soup.select('[data-pos]'):
        a = block.select_one('a[href^="http"]')
        if not a:
            continue
        url = a['href']
        # Skip Brave's own domains (ads, internal)
        if 'search.brave.com' in url or 'brave.com/search' in url:
            continue

        title_node = block.select_one('.search-snippet-title, .title')
        title = ''
        if title_node:
            title = title_node.get('title') or title_node.get_text(' ', strip=True)
        if not title:
            title = a.get('title') or a.get_text(' ', strip=True)
        if not title:
            continue

        snip_node = block.select_one('.snippet-content, .content, .snippet-description')
        snippet = snip_node.get_text(' ', strip=True) if snip_node else ''
        snippet = _AGO_PREFIX_RE.sub('', unescape(snippet))

        results.append(make_result(title, snippet, url, 'Brave'))

    if not results and len(resp.text) > 20000:
        logger.warning('[Search] Brave 200 but parsed 0 result blocks (%d bytes) — '
                       'likely layout change or soft block', len(resp.text))
    return results


def search_brave(query, max_results=6, freshness=''):
    """Scrape Brave Search HTML results."""
    params = {'q': query, 'source': 'web'}
    # Brave supports tf= param: pd (past day), pw (past week), pm (past month), py (past year)
    _FRESHNESS_MAP = {'day': 'pd', 'week': 'pw', 'month': 'pm', 'year': 'py'}
    if freshness and freshness in _FRESHNESS_MAP:
        params['tf'] = _FRESHNESS_MAP[freshness]
    return http_search_get(
        name='Brave',
        url='https://search.brave.com/search',
        params=params,
        query=query,
        parser=_parse_brave,
        max_results=max_results,
    )

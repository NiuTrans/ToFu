"""tofu_search/search/engines/marginalia.py — Marginalia search (free public API).

Marginalia (https://search.marginalia.nu) runs its own independent crawler
focused on small, text-heavy, non-commercial sites. It surfaces long-tail and
old-web pages that Google/Bing/Brave bury under SEO-optimised content, making
it a high-diversity complement to the mainstream engines.

Uses the free JSON API at api.marginalia.nu with the shared ``public`` key
(rate-limited; returns HTTP 503 when the shared quota is exhausted — handled
as an empty result, never an error). No scraping, no anti-bot concerns.
"""

from urllib.parse import quote

from tofu_search.log import get_logger
from tofu_search.search._common import clean_text, http_search_get

logger = get_logger(__name__)

__all__ = ['search_marginalia']

# Shared public key — no signup. Heavily rate-limited; request your own key
# (email the operator) for higher throughput. Override via env if desired.
_MARGINALIA_KEY = 'public'


def _parse_marginalia(resp):
    """Parse Marginalia JSON API response into result dicts."""
    data = resp.json()
    results = []
    for item in data.get('results', []):
        url = item.get('url', '')
        title = item.get('title', '')
        if not url or not title or not url.startswith('http'):
            continue
        results.append({
            'title': clean_text(title)[:200],
            'snippet': clean_text(item.get('description', ''))[:500],
            'url': url,
            'source': 'Marginalia',
        })
    return results


def search_marginalia(query, max_results=6, freshness=''):
    """Query the Marginalia public JSON API.

    ``freshness`` is accepted for signature uniformity but ignored — Marginalia
    exposes no time-range filter.
    """
    # Query travels in the URL path, not the query string.
    url = 'https://api.marginalia.nu/%s/search/%s' % (_MARGINALIA_KEY, quote(query))
    return http_search_get(
        name='Marginalia',
        url=url,
        params={'count': max_results},
        query=query,
        parser=_parse_marginalia,
        max_results=max_results,
        timeout=10,
    )

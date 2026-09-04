"""tofu_search/search/engines/bing.py — Bing HTML scraping.

Bing wraps result URLs in /ck/a redirects with base64-encoded
real URLs in the 'u' parameter.

Bing geolocates by the requesting server's IP when no market is pinned. On a
datacenter egress IP that lands in an unrelated locale, so a query gets
locale-default junk (e.g. Korean homepages / Japanese tutorials) instead of
query-relevant results. We therefore always send an explicit ``mkt`` +
``setlang`` inferred from the query's script.
"""

import base64
import os
from urllib.parse import parse_qs, urlparse

from tofu_search.log import get_logger
from tofu_search.search._common import http_search_get, make_result, soup_of

logger = get_logger(__name__)

__all__ = ['search_bing']

# Default market when the query script doesn't indicate Chinese. Bing serves
# IP-geolocated locale defaults otherwise. Override via BING_DEFAULT_MARKET
# (e.g. 'en-GB', 'de-DE'); the UI language (setlang) is derived from it.
_DEFAULT_MARKET = os.environ.get('BING_DEFAULT_MARKET', 'en-US') or 'en-US'

# Fraction of CJK characters (among all letters) at/above which we treat the
# query as Chinese and pin the zh-CN market.
_CJK_MARKET_THRESHOLD = 0.2


def _market_for_query(query):
    """Pick a Bing market (``mkt``) + UI language (``setlang``) for the query.

    Inferred cheaply from the script of the query text: a meaningful share of
    CJK characters → Chinese market, else the configured default. Pinning a
    market stops Bing from serving IP-geolocated locale-default results that
    are unrelated to the query.

    Args:
        query: The search query string.

    Returns:
        A ``(mkt, setlang)`` tuple, e.g. ``('zh-CN', 'zh-hans')`` or
        ``('en-US', 'en')``.
    """
    cjk = sum(1 for ch in query if '\u4e00' <= ch <= '\u9fff')
    letters = sum(1 for ch in query if ch.isalpha())
    if cjk and (letters == 0 or cjk / letters >= _CJK_MARKET_THRESHOLD):
        return 'zh-CN', 'zh-hans'
    return _DEFAULT_MARKET, _DEFAULT_MARKET.split('-')[0]


def _bing_decode_url(raw_url):
    """Decode Bing's /ck/a redirect URL to the real destination.

    Bing encodes the real URL as base64 in the 'u' query parameter with an
    'a1' prefix.

    Args:
        raw_url: The href scraped from a result block.

    Returns:
        The decoded destination URL; the original URL when it is a direct
        (non-redirect) link; or ``None`` when it is a ``/ck/a`` redirect we
        failed to decode — so the caller drops it instead of surfacing a
        useless ``bing.com/ck/a`` link as a result.
    """
    try:
        parsed = urlparse(raw_url)
        if '/ck/a' not in parsed.path:
            return raw_url  # direct (non-redirect) result URL
        qs = parse_qs(parsed.query)
        encoded = qs.get('u', [''])[0]
        if encoded.startswith('a1'):
            # Bing uses URL-safe base64 with 'a1' prefix
            payload = encoded[2:]
            # Add padding if needed
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            decoded = base64.b64decode(payload).decode('utf-8', errors='replace')
            if decoded.startswith('http'):
                return decoded
        logger.debug('[Search] Bing redirect not decodable, dropping: %s', raw_url[:80])
        return None
    except Exception as _e:
        logger.debug('[Search] Bing URL decode failed for %s: %s', raw_url[:80], _e)
        return None


def _parse_bing(resp):
    """Parse Bing HTML response into result dicts (bs4 CSS selectors)."""
    results = []
    soup = soup_of(resp.text)
    for li in soup.select('li.b_algo'):
        a = li.select_one('h2 a[href]')
        if not a:
            continue
        # Decode Bing redirect to real URL; drop undecodable redirects
        url = _bing_decode_url(a['href'])
        if not url or not url.startswith('http'):
            continue
        title = a.get_text(' ', strip=True)
        p = li.select_one('p')
        snippet = p.get_text(' ', strip=True) if p else ''
        results.append(make_result(title, snippet, url, 'Bing'))

    # Parse-health signal: a substantial page that yields 0 result blocks is a
    # silent scraper break (layout A/B test, consent interstitial, soft block)
    # — not a genuine "no matches". Surface it so it's diagnosable in the log
    # instead of looking like Bing returning bad results.
    if not results and len(resp.text) > 20000:
        logger.warning('[Search] Bing 200 but parsed 0 result blocks (%d bytes) — '
                       'likely layout change or soft block', len(resp.text))
    return results


def search_bing(query, max_results=6, freshness=''):
    """Scrape Bing HTML search results."""
    mkt, setlang = _market_for_query(query)
    params = {'q': query, 'mkt': mkt, 'setlang': setlang}
    # Bing supports filters= with ex1:"ez1" (past day), ex1:"ez2" (past week), ex1:"ez3" (past month)
    _FRESHNESS_MAP = {'day': 'ex1:"ez1"', 'week': 'ex1:"ez2"', 'month': 'ex1:"ez3"'}
    if freshness and freshness in _FRESHNESS_MAP:
        params['filters'] = _FRESHNESS_MAP[freshness]
    return http_search_get(
        name='Bing',
        url='https://www.bing.com/search',
        params=params,
        query=query,
        parser=_parse_bing,
        max_results=max_results,
    )

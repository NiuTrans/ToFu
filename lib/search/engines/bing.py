"""lib/search/engines/bing.py — Bing HTML scraping.

Bing wraps result URLs in /ck/a redirects with base64-encoded
real URLs in the 'u' parameter.
"""

import base64
import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from lib.log import get_logger
from lib.search._common import clean_text, http_search_get

logger = get_logger(__name__)

__all__ = ['search_bing']


def _bing_decode_url(raw_url):
    """Decode Bing's /ck/a redirect URL to the real destination.

    Bing encodes the real URL as base64 in the 'u' query parameter
    with an 'a1' prefix.
    """
    try:
        parsed = urlparse(raw_url)
        qs = parse_qs(parsed.query)
        if 'u' in qs:
            encoded = qs['u'][0]
            if encoded.startswith('a1'):
                # Bing uses URL-safe base64 with 'a1' prefix
                payload = encoded[2:]
                # Add padding if needed
                padding = 4 - len(payload) % 4
                if padding != 4:
                    payload += '=' * padding
                return base64.b64decode(payload).decode('utf-8', errors='replace')
    except Exception as _e:
        logger.debug('[Search] Bing URL decode failed for %s: %s', raw_url[:80], _e)
    return raw_url


def _parse_bing(resp):
    """Parse Bing HTML response into result dicts."""
    results = []
    blocks = resp.text.split('class="b_algo"')
    for block in blocks[1:]:
        # Title + URL from <h2><a href="...">Title</a></h2>
        h2_m = re.search(
            r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL)
        if not h2_m:
            continue
        raw_url = unescape(h2_m.group(1))
        title = re.sub(r'<[^>]+>', '', h2_m.group(2)).strip()

        # Decode Bing redirect to real URL
        url = _bing_decode_url(raw_url)
        if not url.startswith('http'):
            continue

        # Snippet from first <p>
        snippet = ''
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        if sm:
            snippet = re.sub(r'<[^>]+>', '', unescape(sm.group(1))).strip()

        results.append({
            'title': clean_text(title)[:200],
            'snippet': clean_text(snippet)[:500],
            'url': url,
            'source': 'Bing',
        })
    return results


def search_bing(query, max_results=6):
    """Scrape Bing HTML search results."""
    return http_search_get(
        name='Bing',
        url='https://www.bing.com/search',
        params={'q': query},
        query=query,
        parser=_parse_bing,
        max_results=max_results,
    )

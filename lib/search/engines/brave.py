"""lib/search/engines/brave.py — Brave Search HTML scraping.

Brave has its own independent web index (not recycled Bing/Google),
making it the best diversity complement to DDG.
"""

import re
from html import unescape

import requests

from lib.log import get_logger
from lib.search._common import HEADERS, clean_text

logger = get_logger(__name__)

__all__ = ['search_brave']


def search_brave(query, max_results=6):
    """Scrape Brave Search HTML results."""
    results = []
    try:
        resp = requests.get(
            'https://search.brave.com/search',
            params={'q': query, 'source': 'web'},
            headers=HEADERS, timeout=12,
        )
        if not resp.ok:
            logger.warning('[Search] Brave returned HTTP %d for query: %s', resp.status_code, query[:80])
            return results

        html = resp.text
        # Each organic result lives in a data-pos="N" block
        pos_blocks = re.split(r'data-pos="\d+"', html)
        for block in pos_blocks[1:]:
            if len(results) >= max_results:
                break
            # URL: first <a href="https://..."> with svelte class
            url_m = re.search(
                r'<a[^>]*href="(https?://[^"]+)"[^>]*class="[^"]*svelte', block)
            if not url_m:
                continue
            url = unescape(url_m.group(1))
            # Skip Brave's own domains (ads, internal)
            if 'search.brave.com' in url or 'brave.com/search' in url:
                continue

            # Title: inside .search-snippet-title (via title attr or inner text)
            title = ''
            title_attr = re.search(
                r'class="title search-snippet-title[^"]*"[^>]*title="([^"]+)"', block)
            if title_attr:
                title = unescape(title_attr.group(1))
            else:
                title_div = re.search(
                    r'class="title search-snippet-title[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
                if title_div:
                    title = re.sub(r'<[^>]+>', '', title_div.group(1)).strip()
            if not title:
                continue

            # Snippet: inside .generic-snippet .content
            snippet = ''
            snip_m = re.search(
                r'class="content[^"]*svelte[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
            if snip_m:
                snippet = re.sub(r'<[^>]+>', '', unescape(snip_m.group(1))).strip()
                # Remove leading date prefix like "2 days ago -"
                snippet = re.sub(r'^\d+\s+\w+\s+ago\s*[-–—]\s*', '', snippet)

            results.append({
                'title': clean_text(title)[:200],
                'snippet': clean_text(snippet)[:500],
                'url': url,
                'source': 'Brave',
            })
    except requests.Timeout:
        logger.warning('[Search] Brave timeout for query: %s', query[:80])
    except Exception as e:
        logger.error('[Search] Brave error: %s', e, exc_info=True)
    return results

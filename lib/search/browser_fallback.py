"""lib/search/browser_fallback.py — Browser extension fallback for web search."""

import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from lib.log import get_logger
from lib.search._common import clean_text

logger = get_logger(__name__)

__all__ = ['search_via_browser']


def search_via_browser(query, max_results=8):
    """Fall back to browser extension for web search when server-side engines fail.

    Fetches the DuckDuckGo HTML search page via the user's browser (which may
    have working network even when the server doesn't), then parses the same
    result format as search_ddg_html.
    """
    from lib.browser import fetch_url_via_browser, is_extension_connected
    if not is_extension_connected():
        logger.info('[Search] Browser search fallback skipped (extension not connected) query=%r', query[:80])
        return []

    logger.info('[Search] Browser search fallback TRIGGERED (all engines failed) query=%r', query[:80])
    search_url = 'https://html.duckduckgo.com/html/?q=' + quote_plus(query)
    try:
        from lib.browser import send_browser_command
        result, error = send_browser_command('fetch_url', {
            'url': search_url,
            'maxChars': 200000,
            'timeoutMs': 20000,
        }, timeout=25)
        if error:
            logger.warning('[Search] Browser search fetch failed: %s', str(error)[:200])
            return []
        if not isinstance(result, dict):
            logger.warning('[Search] Browser search unexpected result type: %s', type(result).__name__)
            return []

        html = result.get('html', '') or result.get('text', '')
        if not html or len(html) < 100:
            logger.debug('[Search] Browser search returned empty/short content')
            return []

        results = []
        blocks = html.split('class="result results_links')
        link_re = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
        snip_re = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
        for block in blocks[1:]:
            if len(results) >= max_results:
                break
            lm = link_re.search(block)
            if not lm:
                continue
            raw_url = lm.group(1)
            title = re.sub(r'<[^>]+>', '', lm.group(2)).strip()
            snippet = ''
            sm = snip_re.search(block)
            if sm:
                snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            if '/y.js?' in raw_url and 'ad_' in raw_url:
                continue
            url = raw_url
            if 'uddg=' in raw_url:
                try:
                    url = unquote(parse_qs(urlparse(raw_url).query).get('uddg', [raw_url])[0])
                except Exception as _parse_err:
                    logger.debug('[Search] DDG uddg URL parse failed: %s', _parse_err)
            if url.startswith('http'):
                results.append({
                    'title': clean_text(title)[:200],
                    'snippet': clean_text(snippet)[:500],
                    'url': url,
                    'source': 'DuckDuckGo (via browser)',
                })

        logger.info('[Search] Browser DDG parse got %d results', len(results))
        return results

    except Exception as e:
        logger.error('[Search] Browser search fallback failed: %s', e, exc_info=True)
        return []

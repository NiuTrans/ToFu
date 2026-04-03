"""lib/search/engines/ddg.py — DuckDuckGo HTML lite + Instant Answer API."""

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests

from lib.log import get_logger
from lib.search._common import HEADERS, clean_text

logger = get_logger(__name__)

__all__ = ['search_ddg_html', 'search_ddg_api']


def search_ddg_html(query, max_results=6):
    """Scrape DDG lite HTML. Returns list of {title, snippet, url, source}."""
    results = []
    try:
        resp = requests.get('https://html.duckduckgo.com/html/',
                            params={'q': query}, headers=HEADERS, timeout=12)
        if not resp.ok:
            logger.warning('[Search] DDG HTML returned HTTP %d for query: %s', resp.status_code, query[:80])
            return results
        # DDG rate-limit: HTTP 202 returns empty page; retry once
        if resp.status_code == 202:
            logger.info('[Search] DDG HTML 202 (rate-limited), retry in 0.6s: %s', query[:80])
            time.sleep(0.6)
            resp = requests.get('https://html.duckduckgo.com/html/',
                                params={'q': query}, headers=HEADERS, timeout=12)
            if resp.status_code == 202 or not resp.ok:
                logger.warning('[Search] DDG HTML retry still %d: %s', resp.status_code, query[:80])
                return results
        blocks = resp.text.split('class="result results_links')
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
                except Exception:
                    logger.debug('[Search] uddg URL decode failed: %s', raw_url[:100])
            if url.startswith('http'):
                results.append({
                    'title': clean_text(title)[:200],
                    'snippet': clean_text(snippet)[:500],
                    'url': url, 'source': 'DuckDuckGo',
                })
    except Exception as e:
        logger.error('[Search] DDG HTML error: %s', e, exc_info=True)
    return results


def search_ddg_api(query, max_results=4):
    """Query DDG Instant Answer API for definitions/abstracts."""
    results = []
    try:
        resp = requests.get('https://api.duckduckgo.com/',
                            params={'q': query, 'format': 'json',
                                    'no_html': '1', 'skip_disambig': '1'},
                            timeout=10)
        if not resp.ok:
            logger.warning('[Search] DDG API returned HTTP %d for query: %s', resp.status_code, query[:80])
            return results
        data = resp.json()
        if data.get('AbstractText') and data.get('AbstractURL'):
            results.append({
                'title': clean_text(data.get('Heading', query)),
                'snippet': clean_text(data['AbstractText'])[:600],
                'url': data['AbstractURL'],
                'source': data.get('AbstractSource', 'DuckDuckGo'),
            })
        for topic in data.get('RelatedTopics', []):
            if len(results) >= max_results:
                break
            if topic.get('Text') and topic.get('FirstURL'):
                parts = clean_text(topic['Text']).split(' - ', 1)
                results.append({
                    'title': parts[0][:120],
                    'snippet': (parts[1] if len(parts) > 1 else parts[0])[:400],
                    'url': topic['FirstURL'], 'source': 'DuckDuckGo',
                })
            for sub in topic.get('Topics', []):
                if len(results) >= max_results:
                    break
                if sub.get('Text') and sub.get('FirstURL'):
                    parts = clean_text(sub['Text']).split(' - ', 1)
                    results.append({
                        'title': parts[0][:120],
                        'snippet': (parts[1] if len(parts) > 1 else parts[0])[:400],
                        'url': sub['FirstURL'], 'source': 'DuckDuckGo',
                    })
    except Exception as e:
        logger.error('[Search] DDG API error: %s', e, exc_info=True)
    return results

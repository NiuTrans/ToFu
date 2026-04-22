"""lib/search/engines/ddg.py — DuckDuckGo HTML lite + Instant Answer API."""

import re
from urllib.parse import parse_qs, unquote, urlparse

from lib.log import get_logger
from lib.search._common import clean_text, http_search_get

logger = get_logger(__name__)

__all__ = ['search_ddg_html', 'search_ddg_api']


def _parse_ddg_html(resp):
    """Parse DuckDuckGo lite HTML response into result dicts."""
    results = []
    blocks = resp.text.split('class="result results_links')
    link_re = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
    snip_re = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    for block in blocks[1:]:
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
            except Exception as e:
                logger.debug('[Search] uddg URL decode failed: %s (%s)', raw_url[:100], e)
        if url.startswith('http'):
            results.append({
                'title': clean_text(title)[:200],
                'snippet': clean_text(snippet)[:500],
                'url': url, 'source': 'DuckDuckGo',
            })
    return results


def _build_ddg_api_parser(query):
    """Build a DDG Instant-Answer JSON parser closed over ``query`` for title fallback."""
    def _parse(resp):
        data = resp.json()
        results = []
        # Abstract (definition / summary)
        if data.get('AbstractText') and data.get('AbstractURL'):
            results.append({
                'title': clean_text(data.get('Heading', query)),
                'snippet': clean_text(data['AbstractText'])[:600],
                'url': data['AbstractURL'],
                'source': data.get('AbstractSource', 'DuckDuckGo'),
            })
        # Related topics (flat + nested)
        for topic in data.get('RelatedTopics', []):
            if topic.get('Text') and topic.get('FirstURL'):
                parts = clean_text(topic['Text']).split(' - ', 1)
                results.append({
                    'title': parts[0][:120],
                    'snippet': (parts[1] if len(parts) > 1 else parts[0])[:400],
                    'url': topic['FirstURL'], 'source': 'DuckDuckGo',
                })
            for sub in topic.get('Topics', []):
                if sub.get('Text') and sub.get('FirstURL'):
                    parts = clean_text(sub['Text']).split(' - ', 1)
                    results.append({
                        'title': parts[0][:120],
                        'snippet': (parts[1] if len(parts) > 1 else parts[0])[:400],
                        'url': sub['FirstURL'], 'source': 'DuckDuckGo',
                    })
        return results
    return _parse


def search_ddg_html(query, max_results=6):
    """Scrape DDG lite HTML. Returns list of {title, snippet, url, source}."""
    return http_search_get(
        name='DDG-HTML',
        url='https://html.duckduckgo.com/html/',
        params={'q': query},
        query=query,
        parser=_parse_ddg_html,
        max_results=max_results,
        on_ratelimit_retry=True,  # DDG HTML often returns 202 on first try
    )


def search_ddg_api(query, max_results=4):
    """Query DDG Instant Answer API for definitions/abstracts."""
    return http_search_get(
        name='DDG-API',
        url='https://api.duckduckgo.com/',
        params={'q': query, 'format': 'json',
                'no_html': '1', 'skip_disambig': '1'},
        query=query,
        parser=_build_ddg_api_parser(query),
        max_results=max_results,
        timeout=10,
    )

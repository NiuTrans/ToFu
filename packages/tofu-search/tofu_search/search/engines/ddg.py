"""tofu_search/search/engines/ddg.py — DuckDuckGo HTML lite + Instant Answer API."""

from urllib.parse import parse_qs, unquote, urlparse

from tofu_search.log import get_logger
from tofu_search.search._common import clean_text, http_search_get, make_result, soup_of

logger = get_logger(__name__)

__all__ = ['search_ddg_html', 'search_ddg_api', 'parse_ddg_html_text']


def parse_ddg_html_text(html: str, *, max_results: int | None = None,
                        source: str = 'DuckDuckGo') -> list[dict]:
    """Parse a raw DuckDuckGo lite HTML string into result dicts.

    Shares the exact bs4 selector logic of the in-engine parser, but accepts
    a plain HTML string instead of a ``requests`` response — so a host that
    fetched the SERP through a browser (see
    :func:`tofu_search.search.browser_fallback.search_via_browser`) gets the
    same parsing quality as the server-side engine, without re-implementing it.

    Args:
        html: Raw HTML of an ``html.duckduckgo.com/html/`` results page.
        max_results: Cap on the number of results returned (None ⇒ no cap).
        source: ``source`` field stamped on each result dict.

    Returns:
        List of ``{title, snippet, url, source}`` dicts (possibly empty).
    """
    results = []
    soup = soup_of(html or '')
    for a in soup.select('a.result__a[href]'):
        if max_results is not None and len(results) >= max_results:
            break
        raw_url = a['href']
        if '/y.js?' in raw_url and 'ad_' in raw_url:
            continue
        url = raw_url
        if 'uddg=' in raw_url:
            try:
                url = unquote(parse_qs(urlparse(raw_url).query).get('uddg', [raw_url])[0])
            except Exception as e:
                logger.debug('[Search] uddg URL decode failed: %s (%s)', raw_url[:100], e)
        if raw_url.startswith('//'):
            url = 'https:' + url if url.startswith('//') else url
        if not url.startswith('http'):
            continue
        title = a.get_text(' ', strip=True)
        snippet = ''
        block = a.find_parent(class_='result')
        if block:
            snip = block.select_one('.result__snippet')
            if snip:
                snippet = snip.get_text(' ', strip=True)
        results.append(make_result(title, snippet, url, source))
    return results


def _parse_ddg_html(resp):
    """Parse DuckDuckGo lite HTML response into result dicts (bs4 selectors)."""
    results = []
    soup = soup_of(resp.text)
    for a in soup.select('a.result__a[href]'):
        raw_url = a['href']
        if '/y.js?' in raw_url and 'ad_' in raw_url:
            continue
        # DDG wraps the destination in a uddg= redirect param.
        url = raw_url
        if 'uddg=' in raw_url:
            try:
                url = unquote(parse_qs(urlparse(raw_url).query).get('uddg', [raw_url])[0])
            except Exception as e:
                logger.debug('[Search] uddg URL decode failed: %s (%s)', raw_url[:100], e)
        if raw_url.startswith('//'):
            url = 'https:' + url if url.startswith('//') else url
        if not url.startswith('http'):
            continue
        title = a.get_text(' ', strip=True)
        # The snippet anchor lives in the same result block.
        snippet = ''
        block = a.find_parent(class_='result')
        if block:
            snip = block.select_one('.result__snippet')
            if snip:
                snippet = snip.get_text(' ', strip=True)
        results.append(make_result(title, snippet, url, 'DuckDuckGo'))
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


def search_ddg_html(query, max_results=6, freshness=''):
    """Scrape DDG lite HTML. Returns list of {title, snippet, url, source}."""
    params = {'q': query}
    # DDG HTML supports df= param: d (past day), w (past week), m (past month), y (past year)
    _FRESHNESS_MAP = {'day': 'd', 'week': 'w', 'month': 'm', 'year': 'y'}
    if freshness and freshness in _FRESHNESS_MAP:
        params['df'] = _FRESHNESS_MAP[freshness]
    return http_search_get(
        name='DDG-HTML',
        url='https://html.duckduckgo.com/html/',
        params=params,
        query=query,
        parser=_parse_ddg_html,
        max_results=max_results,
        on_ratelimit_retry=True,  # DDG HTML often returns 202 on first try
    )


def search_ddg_api(query, max_results=4, freshness=''):
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

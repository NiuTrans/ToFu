"""DuckDuckGo HTML/Lite parsing and anonymous search entry points.

Entries: search_ddg_html, search_ddg_api and parse_ddg_html_text.
The common HTTP envelope owns endpoint recovery, routing and attempt budgets.
"""

from urllib.parse import parse_qs, urlparse

from tofu_search.http_client import _search_http2_get
from tofu_search.search._common import clean_text, http_search_get, make_result, soup_of

__all__ = ['search_ddg_html', 'search_ddg_api', 'parse_ddg_html_text']


def _result_url(raw_url):
    """Unwrap DDG navigation once without changing destination URL escapes."""
    url = 'https:' + raw_url if raw_url.startswith('//') else raw_url
    try:
        parsed = urlparse(url)
        if parsed.hostname in ('duckduckgo.com', 'www.duckduckgo.com',
                                'html.duckduckgo.com', 'lite.duckduckgo.com'):
            if parsed.path == '/y.js':
                return None
            if parsed.path.rstrip('/') == '/l':
                url = parse_qs(parsed.query).get('uddg', [''])[0]
                parsed = urlparse(url)
        if parsed.scheme in ('http', 'https') and parsed.hostname:
            return url
    except ValueError:
        pass
    return None


def parse_ddg_html_text(html: str, *, max_results: int | None = None,
                        source: str = 'DuckDuckGo') -> list[dict]:
    """Parse DuckDuckGo's HTML or Lite layouts into the same result dicts.

    Shares the exact bs4 selector logic of the in-engine parser, but accepts
    a plain HTML string instead of a ``requests`` response — so a host that
    fetched the SERP through a browser (see
    :func:`tofu_search.search.browser_fallback.search_via_browser`) gets the
    same parsing quality as the server-side engine, without re-implementing it.

    Args:
        html: Raw HTML of a DDG HTML or Lite results page.
        max_results: Cap on the number of results returned (None ⇒ no cap).
        source: ``source`` field stamped on each result dict.

    Returns:
        List of ``{title, snippet, url, source}`` dicts (possibly empty).
    """
    results = []
    soup = soup_of(html or '')
    for a in soup.select('a.result__a[href], a.result-link[href]'):
        if max_results is not None and len(results) >= max_results:
            break
        url = _result_url(a['href'])
        if not url or a.find_parent(class_='result--ad') is not None:
            continue
        title = a.get_text(' ', strip=True)
        snippet = ''
        block = a.find_parent(class_='result')
        if block:
            snip = block.select_one('.result__snippet')
            if snip:
                snippet = snip.get_text(' ', strip=True)
        elif 'result-link' in a.get('class', ()):
            row = a.find_parent('tr')
            # Lite places the snippet in following table rows. Stop before
            # the next result, including ads, so missing snippets cannot
            # borrow another source's evidence.
            for sibling in row.next_siblings if row else ():
                if getattr(sibling, 'name', None) != 'tr':
                    continue
                if sibling.select_one('a.result-link'):
                    break
                snip = sibling.select_one('.result-snippet')
                if snip:
                    snippet = snip.get_text(' ', strip=True)
                    break
        results.append(make_result(title, snippet, url, source))
    return results


def _parse_ddg_html(resp):
    """Use the same parser for HTTP and browser-provided search evidence."""
    return parse_ddg_html_text(resp.text)


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
        request_get=_search_http2_get,
        alternate_urls=('https://lite.duckduckgo.com/lite/',),
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

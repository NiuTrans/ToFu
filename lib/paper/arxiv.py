"""arXiv URL/ID extraction + title/keyword search.

Handles modern (e.g. ``2301.12345``) and legacy hep-th/9407028 style IDs,
with or without ``v<N>``, embedded in URLs or standalone. Also wraps the
public arXiv Atom search API so Paper Reading Mode can resolve a free-text
title query into a list of candidate papers.
"""

import html
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from lib.http_client import http_get
from lib.log import get_logger

logger = get_logger(__name__)

_ARXIV_API_URL = 'http://export.arxiv.org/api/query'
_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}
# arXiv's Atom API is frequently slow / rate-limited. Retry the API a couple
# of times, then fall back to the abs HTML page (a different endpoint that is
# rarely throttled in lockstep with the API).
_ARXIV_TITLE_RETRIES = 2
_ARXIV_TITLE_RETRY_SLEEP = 1.5


def _extract_arxiv_id(url_or_id):
    """Extract arXiv paper ID from various URL formats.

    Supports:
        - 2301.12345
        - 2301.12345v2
        - arxiv.org/abs/2301.12345
        - arxiv.org/pdf/2301.12345
        - arxiv.org/pdf/2301.12345.pdf
        - arxiv.org/abs/hep-th/0601001
        - https://arxiv.org/abs/2301.12345
    """
    url_or_id = url_or_id.strip()

    m = re.match(r'^(\d{4}\.\d{4,5})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.match(r'^([a-z-]+/\d{7})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    m = re.search(r'arxiv\.org/(?:abs|pdf)/([a-z-]+/\d{7}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    return None


def _fetch_title_via_api(arxiv_id):
    """Resolve a title via the arXiv Atom API. Returns '' on failure (logged)."""
    url = f'{_ARXIV_API_URL}?id_list={quote(arxiv_id)}&max_results=1'
    resp = http_get(url, timeout=15,
                    headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entry = root.find('atom:entry', _ATOM_NS)
    if entry is None:
        return ''
    title_el = entry.find('atom:title', _ATOM_NS)
    return _strip_arxiv_text(title_el.text if title_el is not None else '')


def _fetch_title_via_abs_page(arxiv_id):
    """Scrape the title from the arXiv abs HTML page (API fallback).

    The abs page exposes the title two ways: the ``citation_title`` meta tag
    (clean, preferred) and the ``<title>`` element (``[id] Title``). Returns
    '' on failure (logged by the caller).
    """
    url = f'https://arxiv.org/abs/{quote(arxiv_id)}'
    resp = http_get(url, timeout=15,
                    headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
    resp.raise_for_status()
    body = resp.text or ''

    # Preferred: <meta name="citation_title" content="…">
    m = re.search(
        r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)["\']',
        body, re.IGNORECASE)
    if not m:
        # Order-independent: content may precede name.
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_title["\']',
            body, re.IGNORECASE)
    if m:
        return _strip_arxiv_text(html.unescape(m.group(1)))

    # Fallback: <title>[2301.12345] Real Paper Title</title>
    m = re.search(r'<title>(.*?)</title>', body, re.IGNORECASE | re.DOTALL)
    if m:
        title = html.unescape(m.group(1))
        # Strip a leading "[arxiv-id] " prefix the page prepends.
        title = re.sub(r'^\s*\[[^\]]+\]\s*', '', title)
        return _strip_arxiv_text(title)
    return ''


def fetch_arxiv_title(arxiv_id):
    """Fetch a single paper's title from arXiv by ID — robust, multi-source.

    Used so Paper Reading Mode can label a fetched paper by its title instead
    of the bare ``arXiv:<id>``. Tries the Atom API with a short retry (the API
    is frequently slow / rate-limited), then falls back to scraping the abs
    HTML page (a different endpoint rarely throttled in lockstep). Every
    failed attempt is logged per §2.2 — never a silent empty.

    Args:
        arxiv_id: A bare arXiv ID (e.g. ``2301.12345`` or ``hep-th/0601001``),
            with or without a version suffix.

    Returns:
        The paper title with whitespace collapsed, or '' if every source
        failed (in which case the failure chain is logged).
    """
    arxiv_id = (arxiv_id or '').strip()
    if not arxiv_id:
        return ''

    for attempt in range(1, _ARXIV_TITLE_RETRIES + 1):
        try:
            title = _fetch_title_via_api(arxiv_id)
            if title:
                if attempt > 1:
                    logger.info('[Paper:arXiv:Title] API recovered title for %s on attempt %d',
                                arxiv_id, attempt)
                return title
            logger.warning('[Paper:arXiv:Title] API returned no title for %s (attempt %d/%d)',
                           arxiv_id, attempt, _ARXIV_TITLE_RETRIES)
        except Exception as e:
            logger.warning('[Paper:arXiv:Title] API lookup failed for %s (attempt %d/%d): %s',
                           arxiv_id, attempt, _ARXIV_TITLE_RETRIES, e)
        if attempt < _ARXIV_TITLE_RETRIES:
            time.sleep(_ARXIV_TITLE_RETRY_SLEEP)

    # API exhausted — fall back to the abs HTML page.
    try:
        title = _fetch_title_via_abs_page(arxiv_id)
        if title:
            logger.info('[Paper:arXiv:Title] Recovered title for %s via abs-page fallback', arxiv_id)
            return title
        logger.warning('[Paper:arXiv:Title] abs-page fallback found no title for %s', arxiv_id)
    except Exception as e:
        logger.warning('[Paper:arXiv:Title] abs-page fallback failed for %s: %s', arxiv_id, e)

    logger.error('[Paper:arXiv:Title] All title sources exhausted for %s — '
                 'caller will fall back to bare arXiv:<id>', arxiv_id)
    return ''


def _strip_arxiv_text(s):
    """Collapse whitespace/newlines in arXiv Atom title/summary fields."""
    return re.sub(r'\s+', ' ', (s or '').strip())


def _token_set(s):
    """Lowercase alphanumeric token set, for title-overlap scoring."""
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


def _rerank_by_title(query, results):
    """Re-rank arXiv results so on-topic titles beat arXiv's loose ``all:`` scoring.

    arXiv's relevance order frequently floats tangential papers above the
    obvious title match (verified: ``all:agentic rubrics`` returns a security
    paper at #1). We re-sort by query/title token overlap, with a bonus for an
    exact title substring, falling back to arXiv's own order on ties so we never
    do worse than the API.
    """
    qt = _token_set(query)
    if not qt:
        return results
    q_lower = query.lower()

    def score(item):
        idx, r = item
        overlap = len(qt & _token_set(r.get('title')))
        exact = 2 if q_lower in (r.get('title') or '').lower() else 0
        return (overlap + exact, -idx)  # -idx preserves arXiv order on ties

    ranked = sorted(enumerate(results), key=score, reverse=True)
    return [r for _, r in ranked]


def search_arxiv(query, max_results=10):
    """Search arXiv by free-text title/keyword query via the public Atom API.

    Args:
        query: Free-text query (paper title, keywords, author names).
        max_results: Maximum number of candidate papers to return (capped at 25).

    Returns:
        A list of dicts, each with keys:
            arxiv_id, title, authors (list[str]), summary, published (YYYY-MM-DD),
            primary_category, pdf_url, abs_url.
        Returns an empty list on any failure (logged).
    """
    query = (query or '').strip()
    if not query:
        return []

    max_results = max(1, min(int(max_results or 10), 25))
    # Over-fetch so the title re-rank has a deeper pool to pull the obvious
    # match up from (arXiv often buries it past the naive top-N).
    fetch_n = min(max_results * 2 + 5, 50)
    params = (
        f'search_query={quote("all:" + query)}'
        f'&start=0&max_results={fetch_n}'
        f'&sortBy=relevance&sortOrder=descending'
    )
    url = f'{_ARXIV_API_URL}?{params}'

    try:
        resp = http_get(url, timeout=15,
                        headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
        resp.raise_for_status()
    except Exception as e:
        logger.warning('[Paper:arXiv:Search] Query failed for %.120s: %s', query, e)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.warning('[Paper:arXiv:Search] Atom parse failed for %.120s: %s', query, e)
        return []

    results = []
    for entry in root.findall('atom:entry', _ATOM_NS):
        id_el = entry.find('atom:id', _ATOM_NS)
        raw_id = (id_el.text or '').strip() if id_el is not None else ''
        arxiv_id = _extract_arxiv_id(raw_id)
        if not arxiv_id:
            # Atom <id> is like http://arxiv.org/abs/2301.12345v2
            m = re.search(r'/abs/([^\s]+?)(?:v\d+)?$', raw_id)
            arxiv_id = m.group(1) if m else None
        if not arxiv_id:
            continue

        title_el = entry.find('atom:title', _ATOM_NS)
        summary_el = entry.find('atom:summary', _ATOM_NS)
        published_el = entry.find('atom:published', _ATOM_NS)
        cat_el = entry.find(
            '{http://arxiv.org/schemas/atom}primary_category')

        authors = [
            _strip_arxiv_text(a.findtext('atom:name', default='', namespaces=_ATOM_NS))
            for a in entry.findall('atom:author', _ATOM_NS)
        ]
        authors = [a for a in authors if a]

        results.append({
            'arxiv_id': arxiv_id,
            'title': _strip_arxiv_text(title_el.text if title_el is not None else ''),
            'authors': authors,
            'summary': _strip_arxiv_text(summary_el.text if summary_el is not None else ''),
            'published': ((published_el.text or '')[:10] if published_el is not None else ''),
            'primary_category': (cat_el.get('term') if cat_el is not None else ''),
            'pdf_url': f'https://arxiv.org/pdf/{arxiv_id}.pdf',
            'abs_url': f'https://arxiv.org/abs/{arxiv_id}',
        })

    results = _rerank_by_title(query, results)[:max_results]
    logger.info('[Paper:arXiv:Search] %d results for %.120s', len(results), query)
    return results

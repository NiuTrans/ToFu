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

from lib.log import get_logger

logger = get_logger(__name__)


def http_get(*args, **kwargs):
    """Request-loaded HTTP seam retained for title-resolution tests."""
    from lib.http_client import http_get as _http_get

    return _http_get(*args, **kwargs)

_ARXIV_API_URL = 'http://export.arxiv.org/api/query'
_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}
# arXiv's Atom API is frequently slow / rate-limited. Retry the API a couple
# of times, then fall back to the abs HTML page (a different endpoint that is
# rarely throttled in lockstep with the API).
_ARXIV_TITLE_RETRIES = 2
_ARXIV_TITLE_RETRY_SLEEP = 1.5
# arXiv's search API rate-limits aggressively (HTTP 429) and times out under
# load; a single transient failure must NOT return an empty result that
# silently starves downstream callers (harvest seed / ideate novelty retrieval
# / recommend). Retry with backoff seeded at arXiv's documented ~3s rate limit.
_ARXIV_SEARCH_RETRIES = 3
_ARXIV_SEARCH_RETRY_SLEEP = 3.0

# `search_arxiv` takes FREE TEXT ONLY. These markers mean the caller already
# built arXiv query syntax, and feeding that here is a SILENT corruption: the
# free-text path sanitizes its input (correct for terms, destructive for built
# syntax), so `(ti:predictive OR ti:delta) AND all:"KV cache compression"`
# became `all:ti predictive OR ti delta AND all KV cache compression` and arXiv
# answered a KV-cache query with TITANIUM ALLOY papers — bare `ti` matched
# "Ti". No exception, no empty result, just five confidently wrong papers.
#
# One entry point cannot accept both "terms" and "a built query": whichever
# shape it was not written for gets mangled without a signal. So this raises
# instead of best-effort cleaning. Callers with structural legs (the novelty
# gate) must use `tofu_search.search.vertical.arxiv.search_by_query`, which
# takes identity/domain as PARAMETERS and owns the construction.
_FIELDED_SYNTAX_RE = re.compile(
    r'(?:\b(?:ti|abs|au|cat|jr|rn|id|all)\s*:)|(?:\s(?:AND|OR|ANDNOT)\s)')


class ArxivQuerySyntaxError(ValueError):
    """Raised when built arXiv syntax is passed to the free-text entry point."""


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
    if not isinstance(url_or_id, str):
        return None
    url_or_id = url_or_id.strip()

    m = re.match(r'^(\d{4}\.\d{4,5})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.match(r'^([a-z-]+/\d{7})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    # Models and citation renderers commonly include the human-facing prefix.
    m = re.match(r'^arxiv\s*:\s*([a-z-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?$',
                 url_or_id, flags=re.IGNORECASE)
    if m:
        return m.group(1) + (m.group(2) or '')

    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    m = re.search(r'arxiv\.org/(?:abs|pdf)/([a-z-]+/\d{7}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    return None


def normalize_arxiv_id(value) -> str:
    """Return a bare, versionless arXiv id from model/library wire shapes.

    Accepts bare ids, ``arXiv:<id>``, abs/pdf URLs, and the small dict shapes
    emitted in research JSON. Unlike the historical ``split('v')[0]``, this
    removes only a terminal version suffix, so the ``v`` in ``arXiv:`` cannot
    turn a valid citation into the bogus id ``arXi``.
    """
    if isinstance(value, dict):
        for key in ('arxiv_id', 'id', 'paper'):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                value = candidate
                break
        else:
            return ''
    extracted = _extract_arxiv_id(value) if isinstance(value, str) else None
    if not extracted:
        return ''
    return re.sub(r'v\d+$', '', extracted, flags=re.IGNORECASE).strip()


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


def search_arxiv_explained(query, max_results=10):
    """Search arXiv by free-text query, returning ``(results, error)``.

    The arXiv HTTP client and Atom parsing live in
    ``tofu_search.search.vertical.arxiv`` — this function does NOT own a second
    copy of them. It contributes only the two things that are genuinely local
    policy: retry/backoff for arXiv's aggressive 429s, and the title re-rank.

    Retry is driven by the shared helper's ``outcome`` field, which
    distinguishes "the query ran and matched nothing" (``no_matches`` — a real
    answer, do not retry) from "the request failed" (``request_failed`` — retry
    with backoff). A bare empty list cannot express that difference, and
    retrying a legitimate zero would just burn the rate limit.

    ``error`` is the distinction a UI caller MUST keep: it is ``''`` when
    the query ran clean — whether it matched papers or legitimately matched
    none — and carries a short human-readable reason when the request itself
    failed (after all retries) or never ran. Collapsing "failed" and "matched
    nothing" into the same ``[]`` is what made a live outage (a stale server
    process holding a pre-``search_by_query`` tofu_search, 2026-07-28) render
    on the frontend as "no papers found".

    Args:
        query: Free-text query (paper title, keywords, author names).
            MUST NOT contain arXiv field syntax (``ti:`` / ``all:`` / ``AND``
            …) — that raises :class:`ArxivQuerySyntaxError`. A caller with
            identity/domain legs wants
            ``tofu_search.search.vertical.arxiv.search_by_query`` instead.
        max_results: Maximum number of candidate papers to return (capped at 25).

    Returns:
        ``(results, error)`` — results is a list of dicts, each with keys:
            arxiv_id, title, authors (list[str]), summary, published (YYYY-MM-DD),
            primary_category, pdf_url, abs_url.
        error is ``''`` on success/no-match, else a short reason (always
        logged regardless).

    Raises:
        ArxivQuerySyntaxError: the query contains built arXiv field syntax.
    """
    from lib.search_runtime import ensure_search_runtime
    ensure_search_runtime()
    from tofu_search.search.vertical import arxiv as ts_arxiv

    query = (query or '').strip()
    if not query:
        return [], ''

    # Fail LOUDLY rather than mangling. The alternative (sanitize and proceed)
    # is what returned titanium-alloy papers for a KV-cache query with no error.
    if _FIELDED_SYNTAX_RE.search(query):
        raise ArxivQuerySyntaxError(
            f'search_arxiv() takes FREE TEXT, got built arXiv syntax: {query[:120]!r}. '
            'Sanitizing it here would strip the field structure and silently '
            'return wrong papers. Use '
            'tofu_search.search.vertical.arxiv.search_by_query(identity_terms, '
            'domain_terms, field=...) to pass legs as parameters instead.')

    max_results = max(1, min(int(max_results or 10), 25))
    # Over-fetch so the title re-rank has a deeper pool to pull the obvious
    # match up from (arXiv often buries it past the naive top-N).
    fetch_n = min(max_results * 2 + 5, 50)

    res = None
    for attempt in range(1, _ARXIV_SEARCH_RETRIES + 1):
        # Free-text search: every token is a query term, and the domain leg is
        # omitted (this entry point has no notion of a separate field
        # constraint — callers that DO, like the novelty gate, build the fielded
        # query themselves and call the shared helper directly).
        res = ts_arxiv.search_by_query(query, max_results=fetch_n)
        if res.get('outcome') != 'request_failed':
            if attempt > 1:
                logger.info('[Paper:arXiv:Search] recovered for %.80s on attempt %d',
                            query, attempt)
            break
        # Transient → back off and retry; a bare empty return here is what
        # silently starves harvest / novelty retrieval / recommend.
        if attempt < _ARXIV_SEARCH_RETRIES:
            sleep_s = _ARXIV_SEARCH_RETRY_SLEEP * attempt  # linear backoff
            logger.warning('[Paper:arXiv:Search] transient failure for %.80s '
                           '(attempt %d/%d): %s — retrying in %.1fs',
                           query, attempt, _ARXIV_SEARCH_RETRIES,
                           res.get('error'), sleep_s)
            time.sleep(sleep_s)
            continue
        logger.warning('[Paper:arXiv:Search] Query failed for %.120s: %s',
                       query, res.get('error'))
        return [], (res.get('error') or 'arXiv request failed')

    if not res or not res.get('ok'):
        # outcome 'unusable_query' lands here: the request never ran (every
        # term was sanitized away). That is a failure to ASK, not "nothing
        # matched" — surface it as an error, never as an empty result.
        return [], ((res or {}).get('error')
                    or 'query contained no usable search terms')
    if res.get('outcome') == 'no_matches':
        # A real answer, not a failure — log it as such so a genuinely empty
        # field is never mistaken for a broken query.
        logger.info('[Paper:arXiv:Search] 0 results for %.120s (query ran clean)',
                    query)
        return [], ''

    results = _rerank_by_title(query, res['papers'])[:max_results]
    logger.info('[Paper:arXiv:Search] %d results for %.120s', len(results), query)
    return results, ''


def search_arxiv(query, max_results=10):
    """Search arXiv by free-text query. Thin adapter over ``tofu_search``.

    Back-compatible list-only facade over :func:`search_arxiv_explained` for
    internal callers (harvest seed / novelty retrieval / recommend / insight)
    that treat "failed" and "matched nothing" identically. Callers rendering
    a user-facing result (the search route) MUST use the explained variant —
    see its docstring for why.

    Args / Returns / Raises: see :func:`search_arxiv_explained`; only the
    results list is returned here (an empty list means "nothing matched OR
    every attempt failed", always logged).
    """
    results, _error = search_arxiv_explained(query, max_results)
    return results

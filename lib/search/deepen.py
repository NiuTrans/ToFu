"""lib/search/deepen.py — One-hop link-following to deepen search coverage.

After the main pipeline fetches the top pages, those pages already carry an
extracted ``--- Page Links ---`` section (built by
``lib.fetch.html_extract.extract_links_from_soup``). This module harvests
those outbound links, scores them against the query with the same BM25
tokenizer used for reranking, and fetches the best few ONE hop deeper — the
cheap, bounded "depth" that deep-research crawlers (Firecrawl /map→/crawl,
crawl4ai BestFirst) use, without an unbounded recursive frontier.

Design constraints (mirror crawl4ai's budget caps):
  * Depth is fixed at exactly 1 hop — no recursion, no frontier explosion.
  * ``max_links`` hard-caps how many deeper pages we fetch.
  * A visited set (URL dedup keys) prevents re-fetching pages already in the
    result set or each other.
  * ``SKIP_DOMAINS`` and same-as-source URLs are filtered out.

OFF by default. Enable per-call via ``perform_web_search(..., deepen=True)``
or globally via env ``SEARCH_DEEPEN_HOPS=1`` / features.json ``search_deepen``.
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import lib as _lib
from lib.fetch import fetch_page_content
from lib.log import get_logger
from lib.search.rerank import _tokenize

logger = get_logger(__name__)

__all__ = ['deepen_results', 'is_deepen_enabled']

# "- [anchor text](https://url)" lines inside the --- Page Links --- section.
_LINK_RE = re.compile(r'^\s*-\s*\[(.*?)\]\((https?://[^)]+)\)\s*$', re.MULTILINE)

# Marker that fetch appends before the link list (see html_extract.extract_html_text).
_LINKS_HEADER = '--- Page Links ---'


def is_deepen_enabled() -> bool:
    """Resolve the default deepen toggle: env > features.json > off."""
    env = os.environ.get('SEARCH_DEEPEN_HOPS')
    if env is not None and env != '':
        try:
            return int(env) > 0
        except (ValueError, TypeError):
            logger.debug('[Deepen] Non-integer SEARCH_DEEPEN_HOPS=%r, treating as off', env)
            return False
    try:
        from lib import _resolve_feature_flag
        return _resolve_feature_flag('SEARCH_DEEPEN_HOPS', 'search_deepen', False)
    except Exception as e:
        logger.debug('[Deepen] feature-flag resolve failed: %s', e)
        return False


def _harvest_links(results):
    """Pull (anchor, url) candidates from the link sections of fetched pages.

    Returns a list of dicts: {url, anchor, parent}. Only the portion of
    ``full_content`` after the ``--- Page Links ---`` header is scanned, so
    body-text URLs are not treated as navigable links.
    """
    candidates = []
    for r in results:
        content = r.get('full_content') or ''
        idx = content.find(_LINKS_HEADER)
        if idx < 0:
            continue
        link_blob = content[idx:]
        for m in _LINK_RE.finditer(link_blob):
            anchor, url = m.group(1).strip(), m.group(2).strip()
            candidates.append({'url': url, 'anchor': anchor, 'parent': r.get('url', '')})
    return candidates


def _dedup_key(url: str) -> str:
    """Scheme/trailing-slash-insensitive key (mirrors orchestrator._url_dedup_key)."""
    key = url.lower().rstrip('/')
    for scheme in ('https://', 'http://'):
        if key.startswith(scheme):
            key = key[len(scheme):]
            break
    return key[:150]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception as e:
        logger.debug('[Deepen] domain parse failed for %.80s: %s', url, e)
        return ''


def _score_candidate(query_terms: set, anchor: str, url: str) -> float:
    """BM25-free heuristic: token overlap of query vs anchor text + URL slug.

    Anchor matches weigh more than URL-path matches (anchor is human-written
    and intent-bearing). Pure lexical — no API call.
    """
    if not query_terms:
        return 0.0
    anchor_tokens = set(_tokenize(anchor))
    slug = url.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')
    slug_tokens = set(_tokenize(slug))
    anchor_hits = len(query_terms & anchor_tokens)
    slug_hits = len(query_terms & slug_tokens)
    return anchor_hits * 2.0 + slug_hits * 1.0


def deepen_results(query, results, *, max_links=4, max_chars=None,
                   pdf_max_chars=None, fetch_workers=8):
    """Fetch the most query-relevant outbound links one hop deeper.

    Args:
        query: The search query (for relevance scoring).
        results: The already-fetched result dicts (with ``full_content``).
        max_links: Hard cap on how many deeper pages to fetch.
        max_chars / pdf_max_chars: Per-page fetch limits (default to lib config).
        fetch_workers: Thread pool size for the deeper fetches.

    Returns:
        A list of NEW result dicts (source='DeepCrawl') with ``full_content``
        populated. Empty list when nothing worth fetching is found.
    """
    t0 = time.time()
    if not results:
        return []
    if max_chars is None:
        max_chars = _lib.FETCH_MAX_CHARS_SEARCH
    if pdf_max_chars is None:
        pdf_max_chars = _lib.FETCH_MAX_CHARS_PDF

    query_terms = set(_tokenize(query))

    # URLs we already have (sources) — never re-fetch these one hop deeper.
    visited = {_dedup_key(r['url']) for r in results if r.get('url')}

    # Harvest + score + dedup candidates.
    scored = []
    seen_cand = set()
    for cand in _harvest_links(results):
        url = cand['url']
        key = _dedup_key(url)
        if key in visited or key in seen_cand:
            continue
        dom = _domain(url)
        if not dom or any(skip in dom for skip in _lib.SKIP_DOMAINS):
            continue
        seen_cand.add(key)
        score = _score_candidate(query_terms, cand['anchor'], url)
        if score <= 0:
            continue
        scored.append((score, cand))

    if not scored:
        logger.info('[Deepen] No query-relevant outbound links found (query=%r)', query[:60])
        return []

    scored.sort(key=lambda x: -x[0])
    chosen = [c for _s, c in scored[:max_links]]
    logger.info('[Deepen] Following %d/%d candidate links one hop deeper (query=%r)',
                len(chosen), len(scored), query[:60])

    new_results = []

    def _do_fetch(cand):
        url = cand['url']
        content = fetch_page_content(url, max_chars=max_chars, pdf_max_chars=pdf_max_chars)
        return cand, content

    with ThreadPoolExecutor(max_workers=min(fetch_workers, len(chosen))) as pool:
        futs = [pool.submit(_do_fetch, c) for c in chosen]
        for fut in as_completed(futs):
            try:
                cand, content = fut.result()
            except Exception as e:
                logger.warning('[Deepen] deeper fetch failed: %s', e, exc_info=True)
                continue
            if content and len(content) > 50:
                new_results.append({
                    'title': cand['anchor'][:200] or cand['url'],
                    'snippet': '',
                    'url': cand['url'],
                    'source': 'DeepCrawl',
                    'full_content': content,
                })

    logger.info('[Deepen] Fetched %d/%d deeper pages in %.1fs',
                len(new_results), len(chosen), time.time() - t0)
    return new_results

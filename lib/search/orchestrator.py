"""lib/search/orchestrator.py — Parallel multi-engine search pipeline.

Pipeline order (cheap → expensive):
  1. 5 engines in parallel (DDG ×2 + Brave + Bing + SearXNG) → ~72 raw
  2. URL dedup
  3. Content dedup (Jaccard on title+snippet shingles)
  4. Page fetch — "race to N" concurrent fetch (stops once enough fast pages complete)
  5. LLM content filter — relevance verdict + noise removal (parallel LLM calls)
  6. BM25 rerank — on cleaned full text → top-N (pure Python, no API call)
  7. Format for model (in executor, not here)
"""
# HOT_PATH

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import lib as _lib  # module ref for hot-reload
from lib.fetch import fetch_contents_for_results
from lib.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_contents_batch
from lib.log import get_logger
from lib.search.browser_fallback import search_via_browser
from lib.search.dedup import dedup_by_content
from lib.search.engines.bing import search_bing
from lib.search.engines.brave import search_brave
from lib.search.engines.ddg import search_ddg_api, search_ddg_html
from lib.search.engines.searxng import search_searxng
from lib.search.rerank import rerank_by_bm25

logger = get_logger(__name__)

__all__ = ['perform_web_search']


class SearchResultList(list):
    """A list subclass that can carry diagnostic metadata.

    When search returns 0 results, ``_search_diag`` is set to a dict
    with keys: reason, reason_detail, engine_errors, engine_empty, engine_ok.
    """
    _search_diag = None


def perform_web_search(query, max_results=None, user_question=''):
    """Run all search engines in parallel, then progressively narrow results.

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to FETCH_TOP_N (configurable
                     via Settings → Search → "Top N Results").
        user_question: The user's original question (true intent). Used by the
                       LLM content filter to judge relevance.

    Returns:
        list[dict]: Search results.  When the list is empty, a ``_search_diag``
        attribute is attached with diagnostic info about *why* no results
        were found (network errors vs genuinely empty).

    7-step pipeline (cheap operations first, expensive last):
      Step 1: 5 engines in parallel (DDG 20 + Brave 20 + Bing 20 + DDG-API 6 + SearXNG 6 = 72 max)
      Step 2: URL dedup — normalize and deduplicate by URL
      Step 3: Content dedup — Jaccard similarity on title+snippet shingles (CJK bigrams + Latin words)
      Step 4: Page fetch — concurrent HTTP requests for full page content
      Step 5: LLM content filter — relevance verdict (vs user question) + noise removal (parallel)
      Step 6: BM25 rerank — on LLM-cleaned text → top max_results
      Step 7: Format for model (in executor, not here)
    """
    if max_results is None:
        max_results = _lib.FETCH_TOP_N
    all_results = []
    engine_counts = {}      # engines that returned results
    engine_errors = {}      # engines that raised exceptions
    engine_empty = []       # engines that returned [] without error

    ALL_ENGINE_NAMES = ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG']

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {
            pool.submit(search_ddg_html, query, 20): 'DDG-HTML',
            pool.submit(search_brave, query, 20):     'Brave',
            pool.submit(search_bing, query, 20):       'Bing',
            pool.submit(search_ddg_api, query, 6):    'DDG-API',
            pool.submit(search_searxng, query, 6):    'SearXNG',
        }
        try:
            for fut in as_completed(futs, timeout=20):
                tag = futs[fut]
                try:
                    r = fut.result()
                    if r:
                        all_results.extend(r)
                        engine_counts[tag] = len(r)
                    else:
                        engine_empty.append(tag)
                except Exception as e:
                    logger.warning('[Search] %s failed: %s', tag, e)
                    engine_errors[tag] = str(e)[:200]
        except TimeoutError:
            # as_completed() raises TimeoutError when the deadline expires
            # while some futures are still pending.  Collect the results
            # we already have (from the completed engines) and record the
            # timed-out engines as errors instead of discarding everything.
            timed_out = [futs[f] for f in futs if not f.done()]
            for name in timed_out:
                engine_errors[name] = 'Timed out after 20s'
            logger.warning('[Search] %d/%d engines timed out (%s), keeping %d results from others. query=%r',
                           len(timed_out), len(futs), ', '.join(timed_out),
                           len(all_results), query[:80])

    if engine_counts:
        logger.info('[Search] Engine results: %s (query=%r)',
                    ', '.join(f'{k}={v}' for k, v in engine_counts.items()),
                    query[:60])

    # ── Retry: if we got nothing, give DDG another chance ──
    if not all_results:
        logger.info('[Search] 0 results on first attempt, retrying DDG+Brave after 0.8s for query=%r', query[:80])
        time.sleep(0.8)
        retry = search_ddg_html(query, max_results)
        if retry:
            all_results.extend(retry)
        else:
            # Try Brave as second retry
            retry_brave = search_brave(query, max_results)
            if retry_brave:
                all_results.extend(retry_brave)

    # ── Browser fallback: server network may be down but user browser works ──
    if not all_results:
        browser_results = search_via_browser(query, max_results)
        if browser_results:
            logger.info('[Search] Browser fallback produced %d results for query=%r',
                        len(browser_results), query[:80])
            all_results.extend(browser_results)

    # ── Step 2: Deduplicate by normalised URL ──
    seen, unique = set(), []
    for r in all_results:
        key = r['url'].lower().rstrip('/').replace('https://', '').replace('http://', '')[:150]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    url_dedup_count = len(unique)

    # ── Step 3: Content dedup — remove near-duplicate title+snippets ──
    if len(unique) > max_results:
        unique = dedup_by_content(unique)
    content_dedup_count = len(unique)

    # ── Step 4: Page fetch — get full content for all candidates ──
    # Fetch ALL deduplicated candidates (not just top-N) so the LLM
    # filter and embedding reranker operate on real page content.
    unique = fetch_contents_for_results(unique, max_fetch=len(unique))
    fetch_count = sum(1 for r in unique if r.get('full_content'))

    # ── Step 5: LLM content filter — relevance + cleaning ──
    # Run in parallel (concurrency = number of documents).  Irrelevant pages
    # (don't help answer the user's question) get their full_content cleared
    # so they're excluded from embedding reranking and the final model context.
    # ALL documents go through the filter — including short ones, which may
    # be bot-protection pages, cookie walls, or other junk.
    to_filter = [(r['url'], r['full_content']) for r in unique
                 if r.get('full_content')]
    irrelevant_urls: set[str] = set()
    if to_filter:
        logger.info('[Search] LLM-filtering %d/%d fetched pages, query=%r user_question=%r',
                    len(to_filter), len(unique), query[:80], user_question[:80])
        filtered = filter_web_contents_batch(to_filter, query=query,
                                             user_question=user_question,
                                             min_chars=0)
        for r in unique:
            if r['url'] in filtered:
                val = filtered[r['url']]
                if val == IRRELEVANT_SENTINEL:
                    irrelevant_urls.add(r['url'])
                    r['full_content'] = ''
                    logger.info('[Search] ✗ IRRELEVANT dropped: %s', r['url'][:100])
                else:
                    r['full_content'] = val
        if irrelevant_urls:
            logger.info('[Search] Dropped %d/%d irrelevant pages',
                        len(irrelevant_urls), len(to_filter))

    # Remove fully irrelevant results from candidate set
    relevant = [r for r in unique if r['url'] not in irrelevant_urls]

    # ── Step 5b: Deprioritize results without full content ──
    # Results that failed to fetch (SKIP_DOMAINS, HTTP error, etc.) only
    # have title+snippet.  They waste a slot in the final top-N because
    # the model rarely calls fetch_url on them.  Move them to the back
    # so results WITH content always get priority.
    has_content = [r for r in relevant if r.get('full_content')]
    no_content  = [r for r in relevant if not r.get('full_content')]
    relevant = has_content + no_content

    # ── Step 6: BM25 rerank on cleaned full text → top-N ──
    if len(has_content) > max_results:
        # Enough content-bearing results — rerank only those
        relevant = rerank_by_bm25(query, has_content, max_results)
    elif len(relevant) > max_results:
        # Not enough content results — rerank all, content ones are already first
        relevant = rerank_by_bm25(query, relevant, max_results)
    final_count = min(len(relevant), max_results)

    logger.info('[Search] Pipeline: %d raw → %d url-dedup → %d content-dedup → '
                '%d fetched → -%d irrelevant → %d relevant → %d reranked  query=%r',
                len(all_results), url_dedup_count, content_dedup_count,
                fetch_count, len(irrelevant_urls), len(relevant),
                final_count, query[:60])

    final_results = SearchResultList(relevant[:max_results])

    # ── Attach diagnostics when 0 results ──
    if not final_results:
        total_engines = len(ALL_ENGINE_NAMES)
        errored = len(engine_errors)
        empty = len(engine_empty)
        # Determine the primary reason
        if errored == total_engines:
            reason = 'network_error'
            reason_detail = 'All %d search engines failed due to network errors.' % total_engines
        elif errored > 0 and errored >= empty:
            reason = 'partial_network_error'
            failed_names = ', '.join(sorted(engine_errors.keys()))
            reason_detail = (
                '%d/%d engines had network errors (%s); the rest returned no matches.'
                % (errored, total_engines, failed_names)
            )
        else:
            reason = 'no_matches'
            reason_detail = (
                'All search engines responded but found no matching results for this query.'
            )
        diag = {
            'reason': reason,
            'reason_detail': reason_detail,
            'engine_errors': engine_errors,
            'engine_empty': engine_empty,
            'engine_ok': list(engine_counts.keys()),
        }
        final_results._search_diag = diag
        logger.warning('[Search] 0 final results — diag: reason=%s errors=%s empty=%s query=%r',
                       reason, list(engine_errors.keys()), engine_empty, query[:80])

    return final_results

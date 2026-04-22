"""lib/search/orchestrator.py — Parallel multi-engine search pipeline.

Pipeline order (cheap → expensive):
  1+4 MERGED: engines fire in parallel; as each engine returns results,
      URLs are immediately deduped and submitted to the fetch pool.
      Page fetching starts as soon as the FIRST engine responds (~0.7s),
      overlapping with slower engines (SearXNG, retries, etc.).
  2. URL dedup — runs incrementally as each engine batch arrives
  3. Content dedup (Jaccard on title+snippet shingles) — runs once after
     all engines complete, only on not-yet-submitted URLs
  5. LLM content filter — relevance verdict + noise removal (parallel LLM calls)
  6. BM25 rerank — on cleaned full text → top-N (pure Python, no API call)
  7. Format for model (in executor, not here)
"""
# HOT_PATH

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

import lib as _lib  # module ref for hot-reload
from lib.fetch import fetch_page_content
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

    ``_engine_breakdown`` is always set: a dict mapping engine tag →
    list of {url, title} for *all* raw results before dedup/filter.
    This lets the frontend show which engine contributed which URLs.
    """
    _search_diag = None
    _engine_breakdown = None


def _url_dedup_key(url: str) -> str:
    """Normalise a URL into a dedup key."""
    return url.lower().rstrip('/').replace('https://', '').replace('http://', '')[:150]


def perform_web_search(query, max_results=None, user_question=''):
    """Run search engines and page fetches in an overlapping streaming pipeline.

    As each engine returns results, its URLs are immediately deduped and
    submitted to the page-fetch thread pool — no waiting for slower engines.
    This means the first page fetch starts at ~0.7s (when DDG returns)
    instead of ~11s (when SearXNG finally times out).

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to FETCH_TOP_N.
        user_question: The user's original question (true intent).

    Returns:
        SearchResultList: Search results with diagnostics.
    """
    pipeline_t0 = time.time()
    step_timings = {}

    if max_results is None:
        max_results = _lib.FETCH_TOP_N

    # ── Shared state for the streaming pipeline ──
    # All access is protected by _lock since engine callbacks and
    # the fetch pool run on different threads.
    _lock = threading.Lock()
    seen_urls: set[str] = set()              # URL dedup keys already seen
    all_results: list[dict] = []             # all raw results (for diagnostics)
    unique_results: list[dict] = []          # URL-deduped results (final list)
    fetch_futs: dict[Future, dict] = {}      # fetch future → result dict
    ok_count = 0                             # pages fetched successfully
    url_timings: list[tuple] = []            # (url, elapsed, ok, chars)

    target_ok = _lib.FETCH_TOP_N * 2        # Race-to-N target

    engine_counts = {}
    engine_timings = {}
    engine_errors = {}
    engine_empty = []

    ALL_ENGINE_NAMES = ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG']

    max_chars = _lib.FETCH_MAX_CHARS_SEARCH
    pdf_max_chars = _lib.FETCH_MAX_CHARS_PDF

    # ── The fetch pool lives for the entire pipeline ──
    # Engine threads submit fetch jobs into it as soon as results arrive.
    fetch_pool = ThreadPoolExecutor(max_workers=16)

    # Track first-fetch-submitted time for logging
    first_fetch_submitted_at = None

    def _submit_fetches_for_batch(batch: list[dict]):
        """Dedup a batch of engine results and submit new URLs to fetch pool.

        Called from engine-completion callbacks (inside the engine pool's
        as_completed loop). Thread-safe via _lock.
        """
        nonlocal first_fetch_submitted_at
        new_results = []
        with _lock:
            for r in batch:
                key = _url_dedup_key(r['url'])
                if key not in seen_urls:
                    seen_urls.add(key)
                    unique_results.append(r)
                    new_results.append(r)
            all_results.extend(batch)

        if not new_results:
            return

        # Submit fetches for new URLs (outside the lock — pool.submit is fast)
        def _do_fetch(result_dict):
            url = result_dict['url']
            t0 = time.time()
            content = fetch_page_content(url, max_chars=max_chars,
                                         pdf_max_chars=pdf_max_chars)
            elapsed = time.time() - t0
            return result_dict, content, elapsed

        with _lock:
            for r in new_results:
                fut = fetch_pool.submit(_do_fetch, r)
                fetch_futs[fut] = r
            if first_fetch_submitted_at is None:
                first_fetch_submitted_at = time.time()
                logger.info('[Search] ⚡ First fetch submitted at +%.1fs (pipeline overlap started)',
                            first_fetch_submitted_at - pipeline_t0)

    # ══════════════════════════════════════════════════════
    #  Step 1: Fire all engines + immediate fetch submission
    # ══════════════════════════════════════════════════════
    step1_t0 = time.time()

    with ThreadPoolExecutor(max_workers=5) as engine_pool:
        engine_futs = {
            engine_pool.submit(search_ddg_html, query, 20): 'DDG-HTML',
            engine_pool.submit(search_brave, query, 20):     'Brave',
            engine_pool.submit(search_bing, query, 20):       'Bing',
            engine_pool.submit(search_ddg_api, query, 6):    'DDG-API',
            engine_pool.submit(search_searxng, query, 6):    'SearXNG',
        }
        try:
            for fut in as_completed(engine_futs, timeout=20):
                tag = engine_futs[fut]
                engine_elapsed = time.time() - step1_t0
                try:
                    r = fut.result()
                    if r:
                        engine_counts[tag] = len(r)
                        engine_timings[tag] = engine_elapsed
                        logger.info('[Search] ✓ %s returned %d results in %.1fs → submitting fetches',
                                    tag, len(r), engine_elapsed)
                        # Immediately dedup and submit to fetch pool
                        _submit_fetches_for_batch(r)
                    else:
                        engine_empty.append(tag)
                        engine_timings[tag] = engine_elapsed
                        logger.info('[Search] ○ %s returned 0 results in %.1fs', tag, engine_elapsed)
                except Exception as e:
                    logger.warning('[Search] ✗ %s failed in %.1fs: %s', tag, engine_elapsed, e)
                    engine_errors[tag] = str(e)[:200]
                    engine_timings[tag] = engine_elapsed
        except TimeoutError:
            timed_out = [engine_futs[f] for f in engine_futs if not f.done()]
            for name in timed_out:
                engine_errors[name] = 'Timed out after 20s'
                engine_timings[name] = 20.0
            logger.warning('[Search] %d/%d engines timed out (%s), keeping %d results from others. query=%r',
                           len(timed_out), len(engine_futs), ', '.join(timed_out),
                           len(all_results), query[:80])

    step_timings['step1_engines'] = time.time() - step1_t0

    if engine_counts:
        logger.info('[Search] Engine results: %s  timings: %s  (query=%r)',
                    ', '.join(f'{k}={v}' for k, v in engine_counts.items()),
                    ', '.join(f'{k}={v:.1f}s' for k, v in sorted(engine_timings.items(), key=lambda x: x[1])),
                    query[:60])

    # ── Retry: if we got nothing, give DDG another chance ──
    if not all_results:
        logger.info('[Search] 0 results on first attempt, retrying DDG+Brave after 0.8s for query=%r', query[:80])
        time.sleep(0.8)
        retry = search_ddg_html(query, max_results)
        if retry:
            _submit_fetches_for_batch(retry)
        else:
            retry_brave = search_brave(query, max_results)
            if retry_brave:
                _submit_fetches_for_batch(retry_brave)

    # ── Browser fallback: server network may be down but user browser works ──
    if not all_results:
        browser_results = search_via_browser(query, max_results)
        if browser_results:
            logger.info('[Search] Browser fallback produced %d results for query=%r',
                        len(browser_results), query[:80])
            _submit_fetches_for_batch(browser_results)

    # ── Build engine breakdown for diagnostics (before dedup) ──
    engine_breakdown = {}
    for r in all_results:
        eng = r.get('source', 'Unknown')
        engine_breakdown.setdefault(eng, []).append({
            'url': r['url'],
            'title': r.get('title', '')[:100],
        })

    url_dedup_count = len(unique_results)
    step_timings['step2_url_dedup'] = 0.0  # done incrementally, ~0 cost

    # ── Step 3: Content dedup on the unique results ──
    # This runs after all engines have completed. We apply content dedup
    # to filter near-duplicate title+snippets. URLs already submitted to
    # the fetch pool will continue fetching (harmless — extra fetches just
    # populate the cache). The dedup only affects which results we KEEP
    # for the final ranking.
    step3_t0 = time.time()
    if len(unique_results) > max_results:
        unique_results = dedup_by_content(unique_results)
    content_dedup_count = len(unique_results)
    step_timings['step3_content_dedup'] = time.time() - step3_t0

    # Build set of URLs we want to keep after content dedup
    kept_urls = {r['url'] for r in unique_results}

    # ── Dynamically reduce target_ok when candidate pool is too small ──
    # Race-to-N needs enough headroom (successful pages) to actually exit
    # early. If len(kept_urls) < target_ok * 1.5, there aren't enough
    # candidates for target_ok pages to succeed — the loop would wait
    # for the 90s as_completed ceiling instead of racing.
    # Scale target_ok down so it stays achievable, but never below
    # max_results (we still need at least the user-requested count).
    _original_target_ok = target_ok
    if len(kept_urls) < target_ok * 1.5:
        target_ok = max(max_results, int(len(kept_urls) / 1.5))
        if target_ok < _original_target_ok:
            logger.info('[Fetch] target_ok reduced %d → %d '
                        '(candidate pool=%d, need headroom for Race-to-N)',
                        _original_target_ok, target_ok, len(kept_urls))

    # ══════════════════════════════════════════════════════
    #  Step 4: Wait for fetch futures (already running!)
    # ══════════════════════════════════════════════════════
    # Fetches have been running in parallel with the engine calls.
    # Now we just wait for completion with Race-to-N.
    step4_t0 = time.time()

    # Snapshot current fetch futures
    with _lock:
        pending_futs = set(fetch_futs.keys())

    if pending_futs:
        logger.info('[Fetch] Waiting for %d in-flight fetches (started %.1fs ago), target_ok=%d',
                    len(pending_futs), time.time() - (first_fetch_submitted_at or pipeline_t0),
                    target_ok)
        try:
            for fut in as_completed(pending_futs, timeout=90):
                try:
                    result_dict, content, fetch_elapsed = fut.result()
                    url = result_dict['url']
                    ok = bool(content and len(content) > 50)
                    chars = len(content) if content else 0
                    url_timings.append((url, fetch_elapsed, ok, chars))
                    if ok:
                        result_dict['full_content'] = content
                        ok_count += 1
                    if fetch_elapsed > 5:
                        logger.info('[Fetch] ⚠ SLOW url=%.80s  %.1fs  ok=%s chars=%d',
                                    url, fetch_elapsed, ok, chars)
                except Exception as e:
                    logger.warning('[Fetch] fetch thread error: %s', e, exc_info=True)

                # Race-to-N: count only kept URLs (after content dedup)
                kept_ok = sum(1 for r in unique_results
                              if r.get('full_content') and r['url'] in kept_urls)
                if kept_ok >= target_ok:
                    remaining = [f for f in pending_futs if not f.done()]
                    if remaining:
                        elapsed_so_far = time.time() - (first_fetch_submitted_at or step4_t0)
                        logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, '
                                    'cancelling %d slow fetches',
                                    kept_ok, len(pending_futs), elapsed_so_far,
                                    len(remaining))
                        for f in remaining:
                            f.cancel()
                        break
        except TimeoutError:
            logger.warning('[Fetch] as_completed timeout (90s)', exc_info=True)

    # Shut down the fetch pool (cancel any stragglers)
    fetch_pool.shutdown(wait=False)

    fetch_count = sum(1 for r in unique_results if r.get('full_content'))
    step_timings['step4_page_fetch'] = time.time() - step4_t0

    # Log overlap savings
    if first_fetch_submitted_at:
        overlap_duration = step_timings['step1_engines'] - (first_fetch_submitted_at - pipeline_t0)
        if overlap_duration > 0.5:
            logger.info('[Search] ⚡ Pipeline overlap saved ~%.1fs '
                        '(fetches started at +%.1fs, engines finished at +%.1fs)',
                        overlap_duration,
                        first_fetch_submitted_at - pipeline_t0,
                        step_timings['step1_engines'])

    # ── Step 5: LLM content filter — relevance + cleaning ──
    # Short-circuit when the filter is disabled: skip the per-page
    # (url, text) tuple build + batch round-trip (which otherwise costs
    # 1-3s per pipeline under concurrent searches on FUSE storage).
    step5_t0 = time.time()
    irrelevant_urls: set[str] = set()
    from lib.fetch.content_filter import FILTER_ENABLED as _FILTER_ENABLED
    if not _FILTER_ENABLED:
        logger.debug('[Search] step5 skipped — FETCH_LLM_FILTER disabled')
    else:
        to_filter = [(r['url'], r['full_content']) for r in unique_results
                     if r.get('full_content')]
        if to_filter:
            logger.info('[Search] LLM-filtering %d/%d fetched pages, query=%r user_question=%r',
                        len(to_filter), len(unique_results), query[:80], user_question[:80])
            filtered = filter_web_contents_batch(to_filter, query=query,
                                                 user_question=user_question,
                                                 min_chars=0)
            for r in unique_results:
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

    step_timings['step5_llm_filter'] = time.time() - step5_t0

    # Remove fully irrelevant results from candidate set
    relevant = [r for r in unique_results if r['url'] not in irrelevant_urls]

    # ── Step 5b: Deprioritize results without full content ──
    has_content = [r for r in relevant if r.get('full_content')]
    no_content  = [r for r in relevant if not r.get('full_content')]
    relevant = has_content + no_content

    # ── Step 6: BM25 rerank on cleaned full text → top-N ──
    step6_t0 = time.time()
    if len(has_content) > max_results:
        relevant = rerank_by_bm25(query, has_content, max_results)
    elif len(relevant) > max_results:
        relevant = rerank_by_bm25(query, relevant, max_results)
    final_count = min(len(relevant), max_results)
    step_timings['step6_bm25_rerank'] = time.time() - step6_t0

    pipeline_total = time.time() - pipeline_t0
    step_timings['total'] = pipeline_total

    # Build timing summary
    timing_parts = []
    for step_name in ['step1_engines', 'step2_url_dedup', 'step3_content_dedup',
                      'step4_page_fetch', 'step5_llm_filter', 'step6_bm25_rerank']:
        elapsed = step_timings.get(step_name, 0)
        timing_parts.append(f'{step_name}={elapsed:.1f}s')
    timing_str = ', '.join(timing_parts)

    logger.info('[Search] Pipeline: %d raw → %d url-dedup → %d content-dedup → '
                '%d fetched → -%d irrelevant → %d relevant → %d reranked  '
                'TOTAL=%.1fs  [%s]  query=%r',
                len(all_results), url_dedup_count, content_dedup_count,
                fetch_count, len(irrelevant_urls), len(relevant),
                final_count, pipeline_total, timing_str, query[:60])

    # Log fetch timing breakdown
    if url_timings:
        url_timings.sort(key=lambda x: -x[1])
        slow_summary = '  '.join(
            f'[{"✓" if ok else "✗"}]{url[:50]}={et:.1f}s'
            for url, et, ok, _chars in url_timings[:8]
        )
        logger.info('[Fetch] Timing breakdown (slowest first): %s', slow_summary)

    # Warn if any step is excessively slow
    if step_timings.get('step4_page_fetch', 0) > 15:
        logger.warning('[Search] ⚠ SLOW step4_page_fetch=%.1fs (>15s threshold) — '
                       'browser fallbacks or slow sites may be blocking the fetch pool. '
                       'query=%r', step_timings['step4_page_fetch'], query[:60])
    if step_timings.get('step5_llm_filter', 0) > 20:
        logger.warning('[Search] ⚠ SLOW step5_llm_filter=%.1fs (>20s threshold) — '
                       'LLM content filter calls are bottlenecking. query=%r',
                       step_timings['step5_llm_filter'], query[:60])
    if pipeline_total > 30:
        logger.warning('[Search] ⚠ SLOW PIPELINE total=%.1fs (>30s threshold) — '
                       'breakdown: %s  query=%r',
                       pipeline_total, timing_str, query[:60])

    final_results = SearchResultList(relevant[:max_results])
    final_results._engine_breakdown = engine_breakdown

    # ── Attach diagnostics when 0 results ──
    if not final_results:
        total_engines = len(ALL_ENGINE_NAMES)
        errored = len(engine_errors)
        empty = len(engine_empty)
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

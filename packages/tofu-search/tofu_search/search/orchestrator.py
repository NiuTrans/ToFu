"""tofu_search.search.orchestrator — Parallel multi-engine search pipeline.

Pipeline order (cheap → expensive):
  1+4 MERGED: engines fire in parallel; as each engine returns results,
      URLs are immediately deduped, passed through a cheap pre-fetch
      relevance gate, and the survivors submitted to the fetch pool. Page
      fetching starts as soon as the FIRST engine responds, overlapping
      with slower engines.
  2. URL dedup — runs incrementally as each engine batch arrives.
  2b. Pre-fetch gate — drop the FETCH of results with zero query-term overlap
      (off-topic SERP junk), fail-open, no LLM. See search/prefetch_gate.py.
  3. Snippet grouping (Jaccard on title+snippet shingles) — once after engines.
      Retain discovered alternatives and independently received page bodies;
      actual body dedup follows fetch completion.
  5. Optional LLM filter — cheap relevance verdict by default; rewrite mode
      can also regenerate cleaned text.
  6. BM25 rerank — on cleaned full text → top-N (pure Python, no API call).
"""
# HOT_PATH

import threading
import time
from concurrent.futures import Future, as_completed
from importlib import import_module

from tofu_search._worker_executor import DemandThreadPoolExecutor as ThreadPoolExecutor
from tofu_search.config import bind_call_config, get_config
from tofu_search.fetch import fetch_page_content
from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_contents_batch
from tofu_search.log import get_logger
from tofu_search.providers import (
    get_site_search_provider,
    submit_with_provider_context,
    with_bound_optional_providers,
)
from tofu_search.search.browser_fallback import search_via_browser
from tofu_search.search.candidates import CandidateAccumulator
from tofu_search.search.dedup import (
    attach_similar_snippets,
    canonical_url_key,
    dedup_by_full_content,
    group_by_content,
)
from tofu_search.search.deepen import deepen_results, is_deepen_enabled
from tofu_search.search.diagnostics import attach_search_diagnostics
from tofu_search.search.engine_selection import EngineSelectionError, select_engines
from tofu_search.search.engines.bing import search_bing
from tofu_search.search.engines.brave import search_brave
from tofu_search.search.engines.ddg import search_ddg_api, search_ddg_html
from tofu_search.search.engines.gateway import GATEWAY_TAG_PREFIX, search_gateway_source
from tofu_search.search.engines.marginalia import search_marginalia
from tofu_search.search.engines.searxng import search_searxng
from tofu_search.search.engines.xhs import search_xhs, xhs_search_available
from tofu_search.search.prefetch_gate import partition_fetchable
from tofu_search.search.quality import annotate_relevance, is_established_evidence, site_scopes
from tofu_search.search.ranking_phase import rank_search_candidates
from tofu_search.search.rerank import (
    rerank_by_bm25,
)

# Read the breaker through its module: callers and tests may replace it.
_common = import_module('tofu_search.search._common')
logger = get_logger(__name__)

__all__ = ['perform_web_search', 'SearchResultList']

#: Longest query, in characters without surrounding whitespace, that any engine
#: is asked to run. Brave documents ``q`` as 1-400 characters; even at 12 encoded
#: URL bytes per character a 400-character query keeps every engine URL under
#: 5 KB. A ~3.5K-character query (~16 KB URL) draws HTTP 414/400 or transport
#: failures from every engine, and repeated refusals open each engine's circuit
#: breaker for all callers. See docs/free-search.md, "Query length admission".
MAX_QUERY_CHARS = 400


class SearchResultList(list):
    """A list subclass that carries diagnostic metadata.

    ``_search_diag`` is set when 0 results (reason/detail/engine state).
    ``_engine_breakdown`` maps engine tag → [{url, title}] for raw results.
    ``_deadline_hit`` is True when the wall-clock budget forced a partial return.
    """
    _search_diag = None
    _engine_breakdown = None
    _deadline_hit = False


def _selection_error(reason, detail):
    result = SearchResultList()
    result._engine_breakdown = {}
    result._search_diag = {
        'reason': reason, 'reason_detail': detail,
        'engine_errors': {}, 'engine_empty': [], 'engine_ok': [],
        'deadline_hit': False,
    }
    return result


def _url_dedup_key(url: str) -> str:
    """Compatibility wrapper around the shared canonical URL key."""
    return canonical_url_key(url)


def perform_web_search(query, max_results=None, user_question='', freshness='',
                       *, fetch_pages=True, filter_pages=True, rerank=True,
                       engines=None, max_chars_per_page=None, deepen=None,
                       config=None):
    """Run search engines and page fetches in an overlapping streaming pipeline.

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to config.fetch_top_n.
        user_question: The user's original question (true intent).
        freshness: Time filter — 'day', 'week', 'month', 'year', or '' (none).
        fetch_pages: When False, skip page fetch and return engine snippets.
        filter_pages: When False, skip the LLM relevance filter.
        rerank: When False, skip BM25 rerank.
        engines: Optional iterable of engine tags. None selects Web engines only;
                an explicit empty/unknown/unavailable selection fails closed.
        max_chars_per_page: Override fetch_max_chars_search for this call.
        deepen: When True, follow top query-relevant links one hop deeper.
                None ⇒ SEARCH_DEEPEN_HOPS env default (off).
        config: Optional SearchConfig override — bound for the WHOLE call via
                :func:`bind_call_config`, so engine/fetch code reading
                ``get_config()`` in worker threads sees it too.

    Returns:
        SearchResultList: Search results with diagnostics.
    """
    if config is None:
        config = get_config()
    with bind_call_config(config):
        return _perform_web_search_impl(
            query, max_results=max_results, user_question=user_question,
            freshness=freshness, fetch_pages=fetch_pages,
            filter_pages=filter_pages, rerank=rerank, engines=engines,
            max_chars_per_page=max_chars_per_page, deepen=deepen, config=config)


def _awaited_engine_futures(engine_futs):
    """Futures an engine wave waits for: every engine except probes, whose last call failed.

    A probe that answers while the others run still contributes; the wave waits
    for probes only when every engine in it is probing. See docs/free-search.md,
    "Waiting only for useful work".
    """
    settled = {fut for fut, tag in engine_futs.items()
               if not _common.engine_circuit.last_call_failed(tag)}
    return settled or set(engine_futs)


def _release_recovery_probes(engine_futs, engine_unavailable, wave_t0):
    """Report still-running probes as circuit-open; they settle their breaker later."""
    abandoned = [engine_futs[f] for f in engine_futs if not f.done()]
    for name in abandoned:
        engine_unavailable[name] = 'circuit_open'
    if abandoned:
        logger.info('[Search] Not waiting for recovery probe(s) %s after %.1fs',
                    ', '.join(abandoned), time.monotonic() - wave_t0)
    return abandoned


def _counts_toward_target(result, query):
    """Race-to-N counts only pages that would be delivered as evidence."""
    return is_established_evidence(annotate_relevance(result, query))


@with_bound_optional_providers
def _perform_web_search_impl(query, max_results=None, user_question='', freshness='',
                             *, fetch_pages=True, filter_pages=True, rerank=True,
                             engines=None, max_chars_per_page=None, deepen=None,
                             config=None):
    """Run search engines and page fetches in an overlapping streaming pipeline.

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to config.fetch_top_n.
        user_question: The user's original question (true intent).
        freshness: Time filter — 'day', 'week', 'month', 'year', or '' (none).
        fetch_pages: When False, skip page fetch and return engine snippets.
        filter_pages: When False, skip the LLM relevance filter.
        rerank: When False, skip BM25 rerank.
        engines: Optional iterable of engine tags. None selects Web engines only;
                an explicit empty/unknown/unavailable selection fails closed.
        max_chars_per_page: Override fetch_max_chars_search for this call.
        deepen: When True, follow top query-relevant links one hop deeper.
                None ⇒ SEARCH_DEEPEN_HOPS env default (off).
        config: Optional SearchConfig override.

    Returns:
        SearchResultList: Search results with diagnostics.
    """
    if config is None:
        config = get_config()

    pipeline_t0 = time.monotonic()
    step_timings = {}

    # ── Hard wall-clock deadline for the whole call (0 = disabled) ──
    # Caps the fetch-wait loop and short-circuits the filter/deepen/rerank
    # stages so a cluster of dead/slow hosts can't wedge the round. When it
    # fires we force-return whatever's gathered, tagged with _deadline_hit.
    _deadline_secs = getattr(config, 'search_deadline_secs', 0) or 0
    _deadline_ts = (pipeline_t0 + _deadline_secs) if _deadline_secs > 0 else None
    _deadline_hit = False

    def _budget_left():
        """Seconds remaining until the deadline (None = uncapped)."""
        if _deadline_ts is None:
            return None
        return _deadline_ts - time.monotonic()

    if max_results is None:
        max_results = config.fetch_top_n

    if freshness and freshness not in ('day', 'week', 'month', 'year'):
        logger.warning('[Search] Ignoring unknown freshness=%r (expected day|week|month|year)', freshness)
        freshness = ''

    _lock = threading.Lock()
    fetch_futs: dict[Future, dict] = {}
    submitted_fetch_keys: set[str] = set()
    fetch_eligible_keys: set[str] = set()
    url_timings: list[tuple] = []

    target_ok = max_results

    engine_counts = {}
    engine_timings = {}
    engine_errors = {}
    engine_warnings = {}
    engine_unavailable = {}
    engine_empty = []

    query_chars = len(query.strip())
    if query_chars > MAX_QUERY_CHARS:
        logger.warning('[Search] Refused a %d-character query before any engine '
                       '(limit %d): %r', query_chars, MAX_QUERY_CHARS, query[:80])
        return _selection_error(
            'query_too_long',
            'The query is %d characters; search engines accept at most %d, so '
            'no engine was queried.' % (query_chars, MAX_QUERY_CHARS))

    web_engines = [
        ('DDG-HTML', search_ddg_html, 20),
        ('Brave',    search_brave,    20),
        ('Bing',     search_bing,     20),
        ('DDG-API',  search_ddg_api,   6),
        ('SearXNG',  search_searxng,   6),
        ('Marginalia', search_marginalia, 6),
    ]
    try:
        selection = select_engines(
            query, engines, freshness, web_engines=web_engines,
            site_provider=get_site_search_provider(),
            native_search_available=xhs_search_available, native_search=search_xhs,
            config=config, gateway_search=search_gateway_source,
            gateway_tag_prefix=GATEWAY_TAG_PREFIX)
    except EngineSelectionError as error:
        return _selection_error(error.reason, error.detail)
    engine_allow = selection.names
    deferred_engines = selection.deferred_names
    allow_free_recovery = selection.allow_free_recovery

    max_chars = max_chars_per_page if max_chars_per_page else config.fetch_max_chars_search
    pdf_max_chars = config.fetch_max_chars_pdf

    # Pre-fetch relevance gate knobs (resolved once; read in the batch callback).
    gate_enabled = getattr(config, 'prefetch_gate_enabled', True)
    gate_min_terms = getattr(config, 'prefetch_gate_min_query_terms', 2)
    gate_min_fetch = getattr(config, 'prefetch_gate_min_fetch', 3)

    constraint_scopes = site_scopes(query)
    candidates = CandidateAccumulator(
        query, constraint_scopes, fetch_pages=fetch_pages, max_chars=max_chars)
    all_results = candidates.all_results
    unique_results = candidates.unique_results

    fallback_attempts = []

    def _has_useful_candidates():
        # A nonempty SERP is not success when it only matches a generic word.
        return any(r.get('relevance') == 'lexical_match' for r in unique_results)

    fetch_pool = ThreadPoolExecutor(max_workers=16)
    first_fetch_submitted_at = None
    fetch_candidate_budget = max(12, max_results * 3)
    stream_batch_cap = max(2, (max_results + 1) // 2)

    def _do_fetch(result_dict):
        url = result_dict['url']
        t0 = time.monotonic()
        content = fetch_page_content(url, max_chars=max_chars, pdf_max_chars=pdf_max_chars)
        elapsed = time.monotonic() - t0
        return result_dict, content, elapsed

    def _submit_selected(candidates: list[dict], limit: int | None = None):
        """Submit a bounded set without letting the fastest engine monopolize workers."""
        nonlocal first_fetch_submitted_at
        submitted = 0
        with _lock:
            for result in candidates:
                key = _url_dedup_key(result.get('url') or '')
                if (not key or key in submitted_fetch_keys
                        or len(submitted_fetch_keys) >= fetch_candidate_budget):
                    continue
                fut = submit_with_provider_context(fetch_pool, _do_fetch, result)
                fetch_futs[fut] = result
                submitted_fetch_keys.add(key)
                submitted += 1
                if limit is not None and submitted >= limit:
                    break
            if submitted and first_fetch_submitted_at is None:
                first_fetch_submitted_at = time.monotonic()
                logger.info('[Search] First fetch submitted at +%.1fs (pipeline overlap started)',
                            first_fetch_submitted_at - pipeline_t0)

    def _submit_fetches_for_batch(batch: list[dict], engine_tag: str = ''):
        """Dedup a batch of engine results and submit new URLs to fetch pool."""
        with _lock:
            new_results = candidates.add_batch(batch, engine_tag)

        if not new_results or not fetch_pages:
            return
        new_results = [r for r in new_results if not r.get('full_content')]
        if not new_results:
            return

        # ── Pre-fetch relevance gate ──
        # Decline to fetch results that share ZERO query terms with the query
        # (obvious SERP junk), so off-topic pages never cost a fetch / never
        # flood a host's browser transport. Fail-open: short queries and the
        # leading `min_fetch` candidates always pass. Skipped results stay in
        # `unique_results` as snippet-only candidates (added above), so rerank
        # still sees them — we only skip the page FETCH, not the result.
        if gate_enabled:
            to_fetch, _skipped = partition_fetchable(
                query, new_results,
                min_query_terms=gate_min_terms,
                min_fetch=gate_min_fetch,
            )

            # Keep the recall floor for sparse/translated snippets, but do not
            # fetch every generic partial match from a degraded engine.
            floor_keys = {_url_dedup_key(r['url']) for r in new_results[:gate_min_fetch]}
            to_fetch = [r for r in to_fetch if r.get('relevance') != 'weak_match'
                        or _url_dedup_key(r['url']) in floor_keys]
            to_fetch.sort(key=lambda r: r.get('relevance') != 'lexical_match')
        else:
            to_fetch = new_results

        if not to_fetch:
            return

        fetch_eligible_keys.update(_url_dedup_key(r['url']) for r in to_fetch)
        # Start a few top candidates immediately for pipeline overlap, but leave
        # worker capacity for slower engines. Once all SERPs arrive, a cheap
        # global rank fills the remaining candidate budget below.
        _submit_selected(to_fetch, stream_batch_cap)

    # ══ Step 1: Fire all engines + immediate fetch submission ══
    step1_t0 = time.monotonic()

    engine_specs = selection.specs
    engines_run = []

    def _run_engine_wave(wave_tags):
        """Run one engine wave, streaming each finished SERP into fetch admission."""
        nonlocal _deadline_hit
        wave_t0 = time.monotonic()
        wave_specs = [(tag, fn, n) for tag, fn, n in engine_specs if tag in wave_tags]
        engines_run.extend(tag for tag, _fn, _n in wave_specs)
        engine_pool = ThreadPoolExecutor(max_workers=max(1, len(wave_specs)))
        engine_futs = {
            submit_with_provider_context(engine_pool, fn, query, n, freshness): tag
            for tag, fn, n in wave_specs
        }
        awaited = _awaited_engine_futures(engine_futs)
        _engine_timeout = False
        abandoned_probes = []
        try:
            _left = _budget_left()
            _engine_wait = 20.0 if _left is None else max(0.001, min(20.0, _left))
            try:
                for fut in as_completed(engine_futs, timeout=_engine_wait):
                    tag = engine_futs[fut]
                    engine_elapsed = time.monotonic() - wave_t0
                    try:
                        r = fut.result()
                        warnings = [code for code in getattr(r, 'coverage_warnings', ())
                                    if code in ('upstream_unavailable', 'instance_failures')]
                        if warnings:
                            engine_warnings[tag] = warnings
                        unavailable = getattr(r, 'unavailable_reason', '')
                        if unavailable:
                            engine_unavailable[tag] = unavailable
                            engine_timings[tag] = engine_elapsed
                        elif r:
                            engine_counts[tag] = len(r)
                            engine_timings[tag] = engine_elapsed
                            logger.info('[Search] %s returned %d results in %.1fs -> submitting fetches',
                                        tag, len(r), engine_elapsed)
                            _submit_fetches_for_batch(r, tag)
                        else:
                            engine_empty.append(tag)
                            engine_timings[tag] = engine_elapsed
                            logger.info('[Search] %s returned 0 results in %.1fs', tag, engine_elapsed)
                    except Exception as e:
                        logger.warning('[Search] %s failed in %.1fs: %s', tag, engine_elapsed, e)
                        engine_errors[tag] = str(e)[:200]
                        engine_timings[tag] = engine_elapsed
                    awaited.discard(fut)
                    if not awaited:
                        break
                abandoned_probes = _release_recovery_probes(
                    engine_futs, engine_unavailable, wave_t0)
            except TimeoutError:
                _engine_timeout = True
                timed_out = [engine_futs[f] for f in engine_futs if not f.done()]
                for name in timed_out:
                    engine_errors[name] = 'Timed out after %.1fs' % _engine_wait
                    engine_timings[name] = _engine_wait
                if _budget_left() is not None and _budget_left() <= 0:
                    _deadline_hit = True
                logger.warning('[Search] %d/%d engines timed out after %.1fs (%s), '
                               'keeping %d results from others. query=%r',
                               len(timed_out), len(engine_futs), _engine_wait,
                               ', '.join(timed_out), len(all_results), query[:80])
        finally:
            # A ThreadPoolExecutor context manager always waits for timed-out calls
            # on exit, defeating both the 20s engine cap and the global deadline.
            engine_pool.shutdown(wait=not (_engine_timeout or abandoned_probes),
                                 cancel_futures=True)

    _run_engine_wave(engine_allow)
    # Free engines deferred behind gateway sources run only when the gateway
    # produced no query-relevant candidate, including when every source failed.
    _left = _budget_left()
    if (deferred_engines and not _has_useful_candidates() and not _deadline_hit
            and (_left is None or _left > 5)):
        logger.info('[Search] No useful gateway evidence, running free engines: %r', query[:80])
        fallback_attempts.append('free-engines')
        _run_engine_wave(deferred_engines)

    step_timings['step1_engines'] = time.monotonic() - step1_t0

    if engine_counts:
        logger.info('[Search] Engine results: %s  timings: %s  (query=%r)',
                    ', '.join(f'{k}={v}' for k, v in engine_counts.items()),
                    ', '.join(f'{k}={v:.1f}s' for k, v in sorted(engine_timings.items(), key=lambda x: x[1])),
                    query[:60])

    # ── Retry: if we got nothing, give DDG+Brave another chance ──
    # A synchronous engine retry cannot be cancelled mid-request. Reserve its
    # normal network timeout instead of starting it with only milliseconds left
    # and quietly violating the advertised whole-search deadline.
    _left = _budget_left()
    if allow_free_recovery and not _has_useful_candidates() and not _deadline_hit and (_left is None or _left > 11):
        logger.info('[Search] No useful first-wave evidence, retrying original query after 0.8s: %r', query[:80])
        fallback_attempts.append('DDG-HTML')
        time.sleep(0.8)
        try:
            retry_left = _budget_left()
            if retry_left is not None and retry_left <= 0:
                _deadline_hit = True
                raise TimeoutError('Search budget expired before recovery')
            # Recovery is part of this search, not a new full-budget call.
            retry_config = (config.copy(search_deadline_secs=retry_left)
                            if retry_left is not None else config)
            with bind_call_config(retry_config):
                retry = search_ddg_html(query, max_results, freshness)
        except Exception as e:
            # A raising engine is a network failure, not "no matches" — the
            # diag classifier reads engine_errors, so record it there.
            logger.warning('[Search] DDG-HTML retry failed: %s', e)
            engine_errors['DDG-HTML'] = str(e)[:200]
            retry = None
        if retry:
            engine_counts['DDG-HTML'] = engine_counts.get('DDG-HTML', 0) + len(retry)
            _submit_fetches_for_batch(retry, 'DDG-HTML')
        if not _has_useful_candidates():
            _left = _budget_left()
            if _left is None or _left > 11:
                try:
                    fallback_attempts.append('Brave')
                    retry_brave = search_brave(query, max_results, freshness)
                except Exception as e:
                    logger.warning('[Search] Brave retry failed: %s', e)
                    engine_errors['Brave'] = str(e)[:200]
                    retry_brave = None
                if retry_brave:

                    engine_counts['Brave'] = engine_counts.get('Brave', 0) + len(retry_brave)
                    _submit_fetches_for_batch(retry_brave, 'Brave')

    # ── Browser fallback: server network may be down but user browser works ──
    # The provider contract permits a 25s browser fetch, so do not start one
    # unless that much search budget remains.
    _left = _budget_left()
    if allow_free_recovery and not _has_useful_candidates() and not _deadline_hit and (_left is None or _left > 25) and not freshness:
        # The legacy browser provider cannot enforce a freshness parameter.
        # Never silently discard it while recovering from poor evidence.
        fallback_attempts.append('Browser')
        browser_results = search_via_browser(query, max_results)
        if browser_results:
            logger.info('[Search] Browser fallback produced %d results for query=%r',
                        len(browser_results), query[:80])
            _submit_fetches_for_batch(browser_results, 'Browser')

    # ── Build engine breakdown for diagnostics (before dedup) ──
    engine_breakdown = {}
    for r in all_results:
        eng = r.get('source', 'Unknown')
        engine_breakdown.setdefault(eng, []).append({
            'url': r['url'],
            'title': r.get('title', '')[:100],
        })

    url_dedup_count = len(unique_results)
    step_timings['step2_url_dedup'] = 0.0

    # ── Step 3: Group similar snippets without discarding page evidence ──
    step3_t0 = time.monotonic()
    snippet_groups = (group_by_content(unique_results) if len(unique_results) > max_results
                      else [[result] for result in unique_results])
    unique_results = [group[0] for group in snippet_groups]
    content_dedup_count = len(unique_results)
    step_timings['step3_content_dedup'] = time.monotonic() - step3_t0

    if fetch_pages and len(submitted_fetch_keys) < fetch_candidate_budget:
        eligible = [r for r in unique_results
                    if _url_dedup_key(r['url']) in fetch_eligible_keys]
        if eligible:
            ranked_candidates = rerank_by_bm25(
                query, eligible, min(fetch_candidate_budget, len(eligible)))
            _submit_selected(ranked_candidates)

    kept_urls = {r['url'] for r in unique_results}

    # ══ Step 4: Wait for fetch futures (already running) ══
    step4_t0 = time.monotonic()
    with _lock:
        pending_futs = set(fetch_futs.keys())
    provider_content_count = sum(1 for r in unique_results if r.get('full_content')
                                 and r['url'] in kept_urls and _counts_toward_target(r, query))

    _race_to_n_hit = False
    if pending_futs:
        logger.info('[Fetch] Waiting for %d in-flight fetches (started %.1fs ago), target_ok=%d',
                    len(pending_futs), time.monotonic() - (first_fetch_submitted_at or pipeline_t0),
                    target_ok)
        # Count of snippet representatives that came back with content.
        # Maintained incrementally as each future completes — the previous
        # implementation re-scanned all of unique_results on every completion
        # (O(n²)) which both wasted work and was easy to get wrong.
        # Provider-crawled bodies already count as fetched representatives.
        kept_ok = provider_content_count
        observed_futures = set()
        def record_completed_fetch(future):
            nonlocal kept_ok
            if future in observed_futures or future.cancelled():
                return
            observed_futures.add(future)
            try:
                result_dict, content, fetch_elapsed = future.result()
                url = result_dict['url']
                ok = bool(content and len(content) > 50)
                chars = len(content) if content else 0
                url_timings.append((url, fetch_elapsed, ok, chars))
                if ok:
                    result_dict['full_content'] = content
                    if url in kept_urls and _counts_toward_target(result_dict, query):
                        kept_ok += 1
                if fetch_elapsed > 5:
                    logger.info('[Fetch] SLOW url=%.80s  %.1fs  ok=%s chars=%d',
                                url, fetch_elapsed, ok, chars)
            except Exception as e:
                logger.warning('[Fetch] fetch thread error: %s', e, exc_info=True)
        # The wait ceiling is the SMALLER of the legacy 90s fetch cap and the
        # remaining wall-clock budget — so the deadline bounds this loop even
        # when Race-to-N can never exit (kept_ok < target_ok on a niche query).
        _left = _budget_left()
        _wait_ceiling = 90.0 if _left is None else max(0.0, min(90.0, _left))
        fetch_waiters = pending_futs
        if kept_ok >= target_ok:
            _race_to_n_hit = True
            fetch_waiters = ()
            logger.info('[Fetch] Race-to-N: %d provider-crawled pages meet target_ok=%d, '
                        'cancelling %d fetches', kept_ok, target_ok, len(pending_futs))
            for f in pending_futs:
                f.cancel()
        try:
            for fut in as_completed(fetch_waiters, timeout=_wait_ceiling):
                record_completed_fetch(fut)

                # ── Deadline check: force-return partial results ──
                if _budget_left() is not None and _budget_left() <= 0:
                    _deadline_hit = True
                    remaining = [f for f in pending_futs if not f.done()]
                    logger.warning('[Fetch] DEADLINE hit (%ds budget) after %.1fs — '
                                   'force-returning %d fetched page(s), cancelling %d in-flight. query=%r',
                                   _deadline_secs,
                                   time.monotonic() - (first_fetch_submitted_at or step4_t0),
                                   kept_ok, len(remaining), query[:60])
                    for f in remaining:
                        f.cancel()
                    break

                if kept_ok >= target_ok:
                    _race_to_n_hit = True
                    remaining = [f for f in pending_futs if not f.done()]
                    if remaining:
                        elapsed_so_far = time.monotonic() - (first_fetch_submitted_at or step4_t0)
                        logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, '
                                    'cancelling %d slow fetches',
                                    kept_ok, len(pending_futs), elapsed_so_far, len(remaining))
                        for f in remaining:
                            f.cancel()
                        break
        except TimeoutError:
            # as_completed hit its ceiling. If that ceiling WAS the deadline
            # (budget <= the legacy 90s), record it as a deadline hit so the
            # downstream stages short-circuit and the diag marker is set.
            if _budget_left() is not None and _budget_left() <= 0:
                _deadline_hit = True
                logger.warning('[Fetch] DEADLINE hit (%ds budget) at fetch-wait ceiling — '
                               'force-returning partial results. query=%r', _deadline_secs, query[:60])
            else:
                logger.warning('[Fetch] as_completed timeout (%.0fs)', _wait_ceiling, exc_info=True)

    # On a deadline or Race-to-N exit, do NOT join still-running fetch threads:
    # shutdown(wait=True) would erase the latency benefit of the early return.
    fetch_pool.shutdown(wait=not (_deadline_hit or _race_to_n_hit), cancel_futures=True)

    if pending_futs and (_deadline_hit or _race_to_n_hit):
        # Consume already settled receipts once; never join a running loser or
        # start more work after the stop decision. Completion order must not
        # silently erase an alternative that was fetched within this call.
        for future in pending_futs:
            if future.done():
                record_completed_fetch(future)

    # Snippet similarity cannot invalidate independently received page bodies.
    # Keep the existing representative ordering for snippet-only candidates;
    # let actual content dedup/relevance/ranking assess fetched alternatives.
    unique_results.extend(row for group in snippet_groups for row in group[1:]
                          if row.get('full_content'))
    fetched_raw_count = sum(1 for r in unique_results if r.get('full_content'))
    step_timings['step4_page_fetch'] = time.monotonic() - step4_t0

    # Different URLs can be mirrors or syndicated copies. Collapse them before
    # the LLM gate so one article never consumes several model calls / slots.
    step4c_t0 = time.monotonic()
    unique_results = dedup_by_full_content(unique_results)
    fetch_count = sum(1 for r in unique_results if r.get('full_content'))
    full_content_dedup_count = fetched_raw_count - fetch_count
    step_timings['step4c_full_content_dedup'] = time.monotonic() - step4c_t0

    # Reassess article evidence before any link expansion. Fetch success by
    # itself must not let weak SERPs amplify their navigation/footer links.
    for result in unique_results:
        annotate_relevance(result, query)

    # ── Step 4b: One-hop link-following (depth) — opt-in ──
    _do_deepen = (bool(getattr(config, 'deepen_enabled', False))
                  or is_deepen_enabled()) if deepen is None else deepen
    if _deadline_hit and _do_deepen:
        logger.info('[Search] step4b deepen skipped — deadline hit')
        _do_deepen = False
    if _do_deepen and fetch_pages and fetch_count:
        step4b_t0 = time.monotonic()
        try:
            left = _budget_left()
            deeper = deepen_results(query, unique_results,
                                    max_chars=max_chars, pdf_max_chars=pdf_max_chars,
                                    deadline_ts=None if left is None else time.monotonic() + max(0, left))
            if _budget_left() is not None and _budget_left() <= 0:
                _deadline_hit = True
        except Exception as e:
            logger.error('[Search] deepen stage failed: %s', e, exc_info=True)
            deeper = []
        for dr in deeper:
            if candidates.admit_deep_result(dr):
                unique_results.append(dr)
                kept_urls.add(dr['url'])
        step_timings['step4b_deepen'] = time.monotonic() - step4b_t0
        if deeper:
            logger.info('[Search] Deepen added %d pages in %.1fs',
                        len(deeper), step_timings['step4b_deepen'])

    if first_fetch_submitted_at:
        overlap_duration = step_timings['step1_engines'] - (first_fetch_submitted_at - pipeline_t0)
        if overlap_duration > 0.5:
            logger.info('[Search] Pipeline overlap saved ~%.1fs '
                        '(fetches started at +%.1fs, engines finished at +%.1fs)',
                        overlap_duration,
                        first_fetch_submitted_at - pipeline_t0,
                        step_timings['step1_engines'])

    # ── Step 5: LLM relevance gate (or opt-in rewrite/cleaning mode) ──
    step5_t0 = time.monotonic()
    irrelevant_urls: set[str] = set()
    _filter_on = config.filter_enabled and config.has_llm()
    if _deadline_hit:
        logger.info('[Search] step5 LLM-filter skipped — deadline hit (serving unfiltered partial results)')
    elif not filter_pages:
        logger.debug('[Search] step5 skipped — caller passed filter_pages=False')
    elif not _filter_on:
        logger.debug('[Search] step5 skipped — filter disabled or no LLM configured')
    else:
        to_filter = [(r['url'], r['full_content']) for r in unique_results
                     if r.get('full_content')]
        if to_filter:
            logger.info('[Search] LLM-filtering %d/%d fetched pages, query=%r user_question=%r',
                        len(to_filter), len(unique_results), query[:80], user_question[:80])
            # min_chars deliberately NOT overridden: config.filter_min_chars
            # (default 3000) lets short pages skip the LLM entirely. The old
            # zero-min_chars override here forced EVERY page through the
            # filter and was a major step-5 latency source.
            left = _budget_left()
            filter_timeout = config.filter_timeout if left is None else max(0.001, min(config.filter_timeout, left))
            filtered = filter_web_contents_batch(to_filter, query=query,
                                                 user_question=user_question,
                                                 timeout=filter_timeout,
                                                 config=config)
            if _budget_left() is not None and _budget_left() <= 0:
                _deadline_hit = True
            for r in unique_results:
                if r['url'] in filtered:
                    val = filtered[r['url']]
                    if val == IRRELEVANT_SENTINEL:
                        irrelevant_urls.add(r['url'])
                        r['full_content'] = ''
                        logger.debug('[Search] IRRELEVANT dropped: %s', r['url'][:100])
                    else:
                        r['full_content'] = val
            if irrelevant_urls:
                logger.info('[Search] Dropped %d/%d irrelevant pages',
                            len(irrelevant_urls), len(to_filter))

    step_timings['step5_llm_filter'] = time.monotonic() - step5_t0

    relevant = [r for r in unique_results if r['url'] not in irrelevant_urls]
    # Preserve uncertain candidates without presenting them as useful evidence.
    # Fetched content may establish relevance even when SERP words do not.
    for result in relevant:
        annotate_relevance(result, query)
    unverified = [r for r in relevant if not is_established_evidence(r)]
    unverified_urls = {r['url'] for r in unverified}
    relevant = [r for r in relevant if r['url'] not in unverified_urls]


    # ── Step 5b: Deprioritize results without full content ──
    has_content = [r for r in relevant if r.get('full_content')]
    no_content  = [r for r in relevant if not r.get('full_content')]
    relevant = has_content + no_content

    # ── Step 6: Shared evidence/prior/diversity ranking → top-N ──
    step6_t0 = time.monotonic()
    relevant, unverified, local_rerank_diag, _deadline_hit = rank_search_candidates(
        query, relevant, unverified, max_results, config=config, rerank=rerank,
        deadline_hit=_deadline_hit, budget_left=_budget_left)
    final_count = min(len(relevant), max_results)
    step_timings['step6_bm25_rerank'] = time.monotonic() - step6_t0

    pipeline_total = time.monotonic() - pipeline_t0
    step_timings['total'] = pipeline_total

    timing_parts = []
    for step_name in ['step1_engines', 'step2_url_dedup', 'step3_content_dedup',
                      'step4_page_fetch', 'step5_llm_filter', 'step6_bm25_rerank']:
        elapsed = step_timings.get(step_name, 0)
        timing_parts.append(f'{step_name}={elapsed:.1f}s')
    timing_str = ', '.join(timing_parts)

    logger.info('[Search] Pipeline: %d raw -> %d url-dedup -> %d content-dedup -> '
                '%d fetched -> -%d mirrored -> -%d irrelevant -> %d relevant -> %d reranked  '
                'TOTAL=%.1fs  [%s]  query=%r',
                len(all_results), url_dedup_count, content_dedup_count,
                fetched_raw_count, full_content_dedup_count,
                len(irrelevant_urls), len(relevant),
                final_count, pipeline_total, timing_str, query[:60])

    if url_timings:
        url_timings.sort(key=lambda x: -x[1])
        slow_summary = '  '.join(
            f'[{"ok" if ok else "fail"}]{url[:50]}={et:.1f}s'
            for url, et, ok, _chars in url_timings[:8]
        )
        logger.info('[Fetch] Timing breakdown (slowest first): %s', slow_summary)

    if step_timings.get('step4_page_fetch', 0) > 15:
        logger.warning('[Search] SLOW step4_page_fetch=%.1fs (>15s threshold). query=%r',
                       step_timings['step4_page_fetch'], query[:60])
    if step_timings.get('step5_llm_filter', 0) > 20:
        logger.warning('[Search] SLOW step5_llm_filter=%.1fs (>20s threshold). query=%r',
                       step_timings['step5_llm_filter'], query[:60])
    if pipeline_total > 30:
        logger.warning('[Search] SLOW PIPELINE total=%.1fs (>30s threshold) — breakdown: %s  query=%r',
                       pipeline_total, timing_str, query[:60])

    final_results = SearchResultList(relevant[:max_results])
    # Match the existing diagnostic preview. Related identities share one
    # max_results allowance across primary entries and this uncertainty view.
    unverified_preview = unverified[:3]
    excluded_related_urls = irrelevant_urls | {
        url for row in unique_results if row['url'] in irrelevant_urls
        for url in row.get('duplicate_urls') or []}
    attach_similar_snippets(list(final_results) + unverified_preview, snippet_groups,
                            max_results, excluded_urls=excluded_related_urls)
    final_results._unverified_candidates = unverified
    final_results._engine_breakdown = engine_breakdown
    final_results._deadline_hit = _deadline_hit
    final_results._local_rerank = local_rerank_diag
    if _deadline_hit:
        logger.warning('[Search] returned PARTIAL results (%d) — wall-clock deadline '
                       '(%ds) fired, TOTAL=%.1fs. query=%r',
                       len(final_results), _deadline_secs, pipeline_total, query[:60])

    attach_search_diagnostics(
        final_results, query=query, engines_run=engines_run, engine_counts=engine_counts,
        engine_errors=engine_errors, engine_empty=engine_empty,
        engine_unavailable=engine_unavailable, engine_warnings=engine_warnings,
        deadline_hit=_deadline_hit, deadline_secs=_deadline_secs,
        constraint_scopes=constraint_scopes, constraint_rejected=candidates.constraint_rejected,
        unverified=unverified, unverified_preview=unverified_preview,
        irrelevant_urls=irrelevant_urls, fallback_attempts=fallback_attempts)
    return final_results

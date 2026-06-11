# HOT_PATH
"""Search-related tool handlers: web_search, fetch_url."""

from __future__ import annotations

from urllib.parse import urlparse

import lib as _lib
from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from tofu_search import fetch_page_content, perform_web_search
from tofu_search.search import format_search_for_tool_response
from tofu_search.search.vertical import (detect_vertical_intent, search_vertical,
                                          search_vertical_domain, list_domains)
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Helpers: single-query search and single-URL fetch
# ══════════════════════════════════════════════════════════

def resolve_vertical(query: str, vertical: str = 'auto'):
    """Resolve the vertical search plan for a query.

    Args:
        query: User-facing search query.
        vertical: One of 'auto' / 'off' / a domain name from
            :func:`tofu_search.search.vertical.list_domains`.

    Returns:
        A zero-arg callable that, when invoked, returns either a domain-level
        record (``{'domain', 'sources', 'items', 'content'}``) or a legacy
        type-level record (``{'domain', 'type', 'content', 'source'}``), or
        ``None`` if no vertical applies.
    """
    v = (vertical or 'auto').strip().lower()
    if v == 'off':
        return None
    if v == 'auto':
        intent = detect_vertical_intent(query)
        if not intent:
            return None
        t, identifier, params = intent
        logger.info('[Search] Vertical auto-intent: type=%s ident=%s for query=%r',
                    t, identifier, query[:60])
        return lambda: search_vertical(t, identifier, params)
    if v in list_domains():
        logger.info('[Search] Vertical explicit domain=%s for query=%r', v, query[:60])
        return lambda: search_vertical_domain(v, query)
    logger.warning('[Search] Unknown vertical=%r — falling back to auto', vertical)
    intent = detect_vertical_intent(query)
    if not intent:
        return None
    t, identifier, params = intent
    return lambda: search_vertical(t, identifier, params)


def _web_search_one(query: str, user_question: str, freshness: str = '',
                    vertical: str = 'auto'):
    """Run one web search — returns (results_list, search_diag, engine_breakdown, vertical_result).

    Vertical domain search (when ``vertical`` resolves) runs concurrently
    with the main web pipeline so it adds zero latency. ``vertical='auto'``
    keeps the legacy phrase-detection path; an explicit domain forces a
    fan-out across every sub-source in that domain.
    """
    from concurrent.futures import ThreadPoolExecutor as _TPE

    vertical_result = None
    vertical_future = None
    _vertical_pool = None
    plan = resolve_vertical(query, vertical)
    if plan is not None:
        _vertical_pool = _TPE(max_workers=1)
        vertical_future = _vertical_pool.submit(plan)

    try:
        results = perform_web_search(query, user_question=user_question, freshness=freshness)
    except Exception as e:
        logger.error('[Executor] web_search failed for query=%r: %s', query, e, exc_info=True)
        results = []
        if vertical_future:
            try:
                vertical_result = vertical_future.result(timeout=10)
            except Exception as ve:
                logger.warning('[Search] Vertical query also failed: %s', ve)
            if _vertical_pool:
                _vertical_pool.shutdown(wait=False)
        return (
            results,
            {
                'reason': 'exception',
                'reason_detail': 'Search failed due to an internal error: %s' % str(e)[:200],
                'engine_errors': {}, 'engine_empty': [], 'engine_ok': [],
            },
            None,
            vertical_result,
        )

    if vertical_future:
        try:
            vertical_result = vertical_future.result(timeout=5)
        except Exception as ve:
            logger.warning('[Search] Vertical query failed: %s', ve)
        if _vertical_pool:
            _vertical_pool.shutdown(wait=False)

    return (
        results,
        getattr(results, '_search_diag', None),
        getattr(results, '_engine_breakdown', None),
        vertical_result,
    )


def _fetch_url_one(target_url: str, user_question: str, fetch_reason: str = ''):
    """Fetch one URL; apply content filter; return a dict with all display fields.

    Returns:
        {
          'url': str, 'page_content': str | None, 'is_pdf': bool,
          'raw_chars': int, 'filtered_chars': int, 'error_msg': str | None,
        }
    """
    scheme = urlparse(target_url).scheme.lower()
    if scheme and scheme not in ('http', 'https', ''):
        logger.warning('[Fetch] Rejected non-HTTP scheme=%r: %s', scheme, target_url[:120])
        return {
            'url': target_url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': f'Rejected: {scheme}:// scheme (use read_files for local paths)',
        }

    try:
        page_content = fetch_page_content(
            target_url,
            max_chars=_lib.FETCH_MAX_CHARS_DIRECT,
            pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF,
        )
    except Exception as e:
        logger.error('[Executor] fetch_url failed for url=%s: %s', target_url, e, exc_info=True)
        page_content = None

    is_pdf = (target_url.lower().rstrip('/').endswith('.pdf')
              or (page_content and page_content.startswith('[Page ')))
    raw_chars = len(page_content) if page_content else 0

    if page_content and not is_pdf:
        from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL
        filtered = filter_web_content(
            page_content, url=target_url,
            query=fetch_reason, user_question=user_question,
        )
        if filtered == IRRELEVANT_SENTINEL:
            logger.info('[Executor] fetch_url IRRELEVANT: %s', target_url[:100])
            page_content = None
        else:
            page_content = filtered

    filtered_chars = len(page_content) if page_content else 0
    return {
        'url': target_url, 'page_content': page_content,
        'is_pdf': is_pdf, 'raw_chars': raw_chars,
        'filtered_chars': filtered_chars, 'error_msg': None,
    }


def _format_fetch_display(item, _short_url) -> dict:
    """Build the display dict (frontend result row) for one fetch item."""
    target_url = item['url']
    if item['error_msg']:
        return {
            'title': f'Rejected: {_short_url(target_url)}',
            'snippet': item['error_msg'], 'url': target_url,
            'source': 'N/A', 'fetched': False, 'fetchedChars': 0,
        }
    filtered_chars = item['filtered_chars']
    raw_chars = item['raw_chars']
    page_content = item['page_content']
    is_pdf = item['is_pdf']
    return {
        'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(target_url)}',
        'snippet': (
            f'{filtered_chars:,} chars'
            + (f' (filtered from {raw_chars:,})' if filtered_chars < raw_chars else '')
        ) if page_content else 'Failed',
        'url': target_url,
        'source': 'PDF' if is_pdf else 'Direct Fetch',
        'fetched': bool(page_content),
        'fetchedChars': filtered_chars,
    }


def _format_search_display_for_results(results) -> list[dict]:
    """Strip ``full_content`` from raw search results for frontend display."""
    display_results = []
    for r in results:
        dr = {k: v for k, v in r.items() if k != 'full_content'}
        if r.get('full_content'):
            dr['fetched'] = True
            dr['fetchedChars'] = len(r['full_content'])
        display_results.append(dr)
    return display_results


def _vertical_to_sse_payload(vertical_result) -> dict | None:
    """Normalize a vertical record into the structured field the frontend renders.

    Domain-level records already carry ``items`` + ``sources``; legacy type-
    level records get wrapped into the same shape so the frontend has a
    single rendering branch.
    """
    if not vertical_result or not isinstance(vertical_result, dict):
        return None
    domain = vertical_result.get('domain') or 'vertical'
    if 'items' in vertical_result and 'sources' in vertical_result:
        return {
            'domain': domain,
            'sources': vertical_result.get('sources', []),
            'items': vertical_result.get('items', []),
        }
    sub_type = vertical_result.get('type', '')
    source = vertical_result.get('source', sub_type)
    items = []
    head = vertical_result.get('content', '').splitlines()[:1]
    title = head[0].lstrip('# ').strip() if head else source or sub_type or 'Result'
    items.append({
        'title': title,
        'snippet': '',
        'url': '',
        'type': sub_type,
        'source': source,
    })
    return {
        'domain': domain,
        'sources': [{'type': sub_type, 'source': source,
                     'identifier': vertical_result.get('identifier', '')}],
        'items': items,
    }


def _vertical_header_for_llm(vertical_result) -> str:
    """Render the markdown block prepended to the tool response for the LLM."""
    if not vertical_result:
        return ''
    if 'sources' in vertical_result and isinstance(vertical_result.get('sources'), list):
        names = [s.get('source') or s.get('type') or '?' for s in vertical_result['sources']]
        label = ' + '.join(names)
        return (f'═══ Vertical Search ({vertical_result.get("domain", "vertical")}: {label}) ═══\n\n'
                f'{vertical_result.get("content", "")}\n\n'
                f'═══ Web Search Results ═══\n\n')
    return (f'═══ Vertical Search Result ({vertical_result.get("source", "vertical")}) ═══\n\n'
            f'{vertical_result.get("content", "")}\n\n'
            f'═══ Web Search Results ═══\n\n')


# ══════════════════════════════════════════════════════════
#  web_search — single + batch
# ══════════════════════════════════════════════════════════

@tool_registry.handler('web_search', category='search',
                       description='Perform a web search and return formatted results')
def _handle_web_search(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # ★ Batch mode: if 'queries' array is present, run all searches concurrently
    queries = fn_args.get('queries')
    if queries and isinstance(queries, list):
        return _handle_web_search_batch(task, tc, fn_name, tc_id, fn_args, queries, rn, round_entry, cfg, project_path, project_enabled, all_tools)

    import time as _time
    handler_t0 = _time.time()
    query = fn_args.get('query', '')
    freshness = fn_args.get('freshness', '')
    vertical_param = fn_args.get('vertical', 'auto')
    user_question = task.get('lastUserQuery', '')

    # ── Fast-path: short-circuit empty/whitespace queries before hitting
    # the orchestrator (5 engines + retry + browser fallback ≈ 4s of wasted work).
    if not query or not query.strip():
        logger.debug('[Search] web_search short-circuit: empty query')
        tool_content = (
            'Error: "query" must be a non-empty string. '
            'Pass the search terms in the `query` field (or `queries` for batch mode).'
        )
        round_entry['results'] = []
        round_entry['status'] = 'done'
        append_event(task, {'type': 'tool_result', 'roundNum': rn, 'query': query, 'results': []})
        return tc_id, tool_content, False

    results, search_diag, engine_breakdown, vertical_result = _web_search_one(
        query, user_question, freshness, vertical=vertical_param)
    display_results = _format_search_display_for_results(results)

    round_entry['results'] = display_results
    round_entry['status'] = 'done'
    event_payload = {'type': 'tool_result', 'roundNum': rn, 'query': query, 'results': display_results}
    if engine_breakdown:
        round_entry['engineBreakdown'] = engine_breakdown
        event_payload['engineBreakdown'] = engine_breakdown
    if not display_results and search_diag:
        round_entry['searchDiag'] = search_diag
        event_payload['searchDiag'] = search_diag

    vertical_payload = _vertical_to_sse_payload(vertical_result)
    if vertical_payload:
        round_entry['vertical'] = vertical_payload
        event_payload['vertical'] = vertical_payload
    append_event(task, event_payload)

    tool_content = format_search_for_tool_response(results, search_diag=search_diag)
    if vertical_result:
        tool_content = _vertical_header_for_llm(vertical_result) + tool_content

    handler_elapsed = _time.time() - handler_t0
    _vlabel = (vertical_payload.get('domain') if vertical_payload else 'none')
    logger.info('[Search] web_search handler TOTAL: %.1fs  query=%r  results=%d  content_chars=%d  vertical=%s',
                handler_elapsed, query[:60], len(display_results),
                sum(r.get('fetchedChars', 0) for r in display_results),
                _vlabel)
    if handler_elapsed > 30:
        logger.warning('[Search] ⚠ web_search handler SLOW: %.1fs (>30s)  query=%r',
                       handler_elapsed, query[:60])
    return tc_id, tool_content, True


def _handle_web_search_batch(task, tc, fn_name, tc_id, fn_args, queries, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle batch web_search: run multiple queries concurrently.

    Concatenates results for all queries into one tool response, with
    per-query headers. Each query's display_results are merged into the
    round_entry's results list for frontend rendering.
    """
    import time as _time

    handler_t0 = _time.time()
    user_question = task.get('lastUserQuery', '')
    freshness = fn_args.get('freshness', '')
    batch_vertical = fn_args.get('vertical', 'auto')
    MAX_BATCH = 5

    query_specs = []  # list of (query_str, freshness_str, vertical_str)
    for spec in queries[:MAX_BATCH]:
        if isinstance(spec, dict):
            q = spec.get('query', '')
            f = spec.get('freshness', '') or freshness
            v = spec.get('vertical') or batch_vertical
            if isinstance(q, str) and q.strip():
                query_specs.append((q.strip(), f, v))
        elif isinstance(spec, str) and spec.strip():
            query_specs.append((spec.strip(), freshness, batch_vertical))
    if not query_specs:
        tool_content = 'Error: "queries" must contain at least one {query} entry.'
        _finalize_tool_round(task, rn, round_entry, [{'type': 'error', 'content': tool_content}])
        return tc_id, tool_content, False

    query_list = [q for q, _, _ in query_specs]
    n = len(query_list)

    def _worker(spec):
        q, f, v = spec
        results, search_diag, engine_breakdown, vertical_result = _web_search_one(
            q, user_question, f, vertical=v)
        formatted = format_search_for_tool_response(results, search_diag=search_diag)
        if vertical_result:
            formatted = _vertical_header_for_llm(vertical_result) + formatted
        return (q, results, search_diag, engine_breakdown, formatted, vertical_result)

    ordered = run_batch_concurrent(query_specs, _worker, max_workers=5, tag='Search')

    all_display_results = []
    all_formatted = []
    all_verticals = []
    for idx, item in enumerate(ordered):
        q = query_list[idx]
        if item is None:
            all_formatted.append(f'Search failed for "{q}": internal error (see logs)')
            continue
        _q, results, _diag, _breakdown, formatted, vertical_result = item
        display_results = _format_search_display_for_results(results)
        # Tag each result with its source query so the frontend can group
        # results under per-query subheaders (batch mode flattens them).
        for dr in display_results:
            dr['_q'] = q
        all_display_results.extend(display_results)
        if vertical_result:
            v_payload = _vertical_to_sse_payload(vertical_result)
            if v_payload:
                v_payload = dict(v_payload)
                v_payload['query'] = q
                all_verticals.append(v_payload)
        if n > 1:
            all_formatted.append(f'=== Search: {q} ===\n{formatted}')
        else:
            all_formatted.append(formatted)

    # Finalize the round with all results combined
    round_entry['results'] = all_display_results
    round_entry['status'] = 'done'
    round_entry['_batchQueries'] = query_list
    event_payload = {
        'type': 'tool_result',
        'roundNum': rn,
        'query': f'🔍 {n} searches',
        'results': all_display_results,
        '_batchQueries': query_list,
    }
    if all_verticals:
        round_entry['verticals'] = all_verticals
        event_payload['verticals'] = all_verticals
    append_event(task, event_payload)

    tool_content = '\n\n'.join(all_formatted)
    handler_elapsed = _time.time() - handler_t0
    logger.info('[Search] web_search BATCH: %d queries in %.1fs  total_results=%d  content_chars=%d',
                n, handler_elapsed, len(all_display_results), len(tool_content))
    return tc_id, tool_content, True


# ══════════════════════════════════════════════════════════
#  fetch_url — single + batch
# ══════════════════════════════════════════════════════════

@tool_registry.handler('fetch_url', category='search',
                       description='Fetch and extract content from a URL')
def _handle_fetch_url(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # ★ Batch mode: if 'urls' array is present, fetch all concurrently
    urls = fn_args.get('urls')
    if urls and isinstance(urls, list):
        return _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls, rn, round_entry, cfg, project_path, project_enabled, all_tools)

    target_url = fn_args.get('url', '')
    user_question = task.get('lastUserQuery', '')
    fetch_reason = fn_args.get('reason', '')

    # ── Guard: reject non-HTTP schemes (file://, ftp://, etc.) ──
    scheme = urlparse(target_url).scheme.lower()
    if scheme and scheme not in ('http', 'https', ''):
        # Strip file:// prefix to extract the local path for the error message
        local_path = target_url
        if scheme == 'file':
            local_path = target_url.split('file://', 1)[-1]
        logger.warning('[Fetch] Rejected non-HTTP URL scheme=%r: %s', scheme, target_url[:120])
        tool_content = (
            f'fetch_url only supports http:// and https:// URLs (got {scheme}://). '
            f'For local files, use read_files with path="{local_path}" '
            f'(read_files supports both project-relative and absolute paths).'
        )
        dr = {
            'title': f'Rejected: {scheme}:// scheme',
            'snippet': 'Use read_files for local paths',
            'url': target_url, 'source': 'N/A',
            'fetched': False, 'fetchedChars': 0,
        }
        _finalize_tool_round(task, rn, round_entry, [dr], query_override=f'📄 {target_url}')
        return tc_id, tool_content, False

    item = _fetch_url_one(target_url, user_question, fetch_reason=fetch_reason)

    from lib.tasks_pkg.tool_display import _short_url
    dr = _format_fetch_display(item, _short_url)
    _finalize_tool_round(task, rn, round_entry, [dr], query_override=f'📄 {target_url}')

    page_content = item['page_content']
    filtered_chars = item['filtered_chars']
    tool_content = (f"Content from {target_url} ({filtered_chars:,} chars):\n\n{page_content}"
                    if page_content else f"Failed to fetch {target_url}.")
    return tc_id, tool_content, True


def _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls_specs, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle batch fetch_url: fetch multiple URLs concurrently.

    Concatenates results for all URLs into one tool response, with
    per-URL headers. Each URL's display_result is added to the
    round_entry's results list for frontend rendering.
    """
    import time as _time

    handler_t0 = _time.time()
    user_question = task.get('lastUserQuery', '')
    MAX_BATCH = 10

    url_list = []
    for spec in urls_specs[:MAX_BATCH]:
        if isinstance(spec, dict) and spec.get('url'):
            url_list.append(spec['url'])
        elif isinstance(spec, str) and spec.strip():
            url_list.append(spec.strip())
    if not url_list:
        tool_content = 'Error: "urls" must contain at least one {url} entry.'
        _finalize_tool_round(task, rn, round_entry, [{'type': 'error', 'content': tool_content}])
        return tc_id, tool_content, False

    n = len(url_list)

    def _worker(target_url):
        return _fetch_url_one(target_url, user_question, fetch_reason='')

    ordered = run_batch_concurrent(url_list, _worker, max_workers=8, tag='Fetch')

    from lib.tasks_pkg.tool_display import _short_url
    all_display_results = []
    all_parts = []
    total_chars = 0
    BATCH_CHAR_BUDGET = 300_000

    for idx, item in enumerate(ordered):
        target_url = url_list[idx]
        if item is None:
            # Synthesize a failure item so the display/text remains aligned
            item = {
                'url': target_url, 'page_content': None, 'is_pdf': False,
                'raw_chars': 0, 'filtered_chars': 0,
                'error_msg': 'internal fetch error (see logs)',
            }

        dr = _format_fetch_display(item, _short_url)
        all_display_results.append(dr)

        # Build text content for LLM
        page_content = item['page_content']
        filtered_chars = item['filtered_chars']
        error_msg = item['error_msg']
        if page_content:
            part = f"Content from {target_url} ({filtered_chars:,} chars):\n\n{page_content}"
        else:
            part = f"Failed to fetch {target_url}." + (f' ({error_msg})' if error_msg else '')

        if total_chars + len(part) > BATCH_CHAR_BUDGET:
            remaining = BATCH_CHAR_BUDGET - total_chars
            if remaining > 200:
                part = part[:remaining] + '\n… [truncated — batch budget exceeded]'
            else:
                all_parts.append(f'… [{n - len(all_parts)} more URLs skipped — batch budget exceeded]')
                break
        total_chars += len(part)
        all_parts.append(part)

    # Finalize the round
    _finalize_tool_round(task, rn, round_entry, all_display_results,
                         query_override=f'📄 {n} URLs')
    tool_content = '\n\n'.join(all_parts)
    handler_elapsed = _time.time() - handler_t0
    fetched_ok = sum(1 for dr in all_display_results if dr.get('fetched'))
    logger.info('[Fetch] fetch_url BATCH: %d URLs (%d OK) in %.1fs  content_chars=%d',
                n, fetched_ok, handler_elapsed, len(tool_content))
    return tc_id, tool_content, True


# Lazy import for content filter (used in _fetch_url_one)
from tofu_search.fetch.content_filter import filter_web_content  # noqa: E402

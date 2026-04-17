# HOT_PATH
"""Search-related tool handlers: tool_search, web_search, fetch_url."""

from __future__ import annotations

from urllib.parse import urlparse

import lib as _lib
from lib.fetch import fetch_page_content
from lib.log import get_logger
from lib.search import format_search_for_tool_response, perform_web_search
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


@tool_registry.handler('tool_search', category='meta',
                       description='Search for deferred tools by keyword')
def _handle_tool_search(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle tool_search calls — discover and activate deferred tools."""
    from lib.tools.deferral import format_search_results, search_deferred_tools
    query = fn_args.get('query', '')
    deferred = task.get('_deferred_tools', [])

    if not deferred:
        tool_content = 'No deferred tools available. All tools are already loaded.'
        if round_entry is not None:
            round_entry['status'] = 'done'
        append_event(task, {'type': 'tool_result', 'roundNum': rn, 'tool': fn_name})
        return tc_id, tool_content, False

    matched = search_deferred_tools(query, deferred)

    # Activate matched tools: add them to the task's tool list
    if matched and all_tools is not None:
        matched_names = {t['function']['name'] for t in matched}
        existing_names = {t.get('function', {}).get('name', '') for t in all_tools}
        for tool_def in matched:
            if tool_def['function']['name'] not in existing_names:
                all_tools.append(tool_def)
        # Remove activated tools from deferred list
        task['_deferred_tools'] = [
            t for t in deferred
            if t.get('function', {}).get('name', '') not in matched_names
        ]
        logger.info('[ToolSearch] Activated %d deferred tools, %d remaining deferred',
                    len(matched), len(task['_deferred_tools']))

    tool_content = format_search_results(matched)
    if round_entry is not None:
        round_entry['status'] = 'done'
    append_event(task, {'type': 'tool_result', 'roundNum': rn, 'tool': fn_name})
    return tc_id, tool_content, False


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
    user_question = task.get('lastUserQuery', '')
    search_diag = None
    engine_breakdown = None
    try:
        results = perform_web_search(query, user_question=user_question)
        search_diag = getattr(results, '_search_diag', None)
        engine_breakdown = getattr(results, '_engine_breakdown', None)
    except Exception as e:
        logger.error('[Executor] web_search failed for query=%r: %s', query, e, exc_info=True)
        results = []
        search_diag = {
            'reason': 'exception',
            'reason_detail': 'Search failed due to an internal error: %s' % str(e)[:200],
            'engine_errors': {}, 'engine_empty': [], 'engine_ok': [],
        }
    display_results = []
    for r in results:
        dr = {k: v for k, v in r.items() if k != 'full_content'}
        if r.get('full_content'):
            dr['fetched'] = True
            dr['fetchedChars'] = len(r['full_content'])
        display_results.append(dr)
    round_entry['results'] = display_results
    round_entry['status'] = 'done'
    event_payload = {'type': 'tool_result', 'roundNum': rn, 'query': query, 'results': display_results}
    if engine_breakdown:
        round_entry['engineBreakdown'] = engine_breakdown
        event_payload['engineBreakdown'] = engine_breakdown
    if not display_results and search_diag:
        round_entry['searchDiag'] = search_diag
        event_payload['searchDiag'] = search_diag
    append_event(task, event_payload)
    tool_content = format_search_for_tool_response(results, search_diag=search_diag)
    handler_elapsed = _time.time() - handler_t0
    logger.info('[Search] web_search handler TOTAL: %.1fs  query=%r  results=%d  content_chars=%d',
                handler_elapsed, query[:60], len(display_results),
                sum(r.get('fetchedChars', 0) for r in display_results))
    if handler_elapsed > 30:
        logger.warning('[Search] ⚠ web_search handler SLOW: %.1fs (>30s)  query=%r',
                       handler_elapsed, query[:60])
    return tc_id, tool_content, True


@tool_registry.handler('fetch_url', category='search',
                       description='Fetch and extract content from a URL')
def _handle_fetch_url(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # ★ Batch mode: if 'urls' array is present, fetch all concurrently
    urls = fn_args.get('urls')
    if urls and isinstance(urls, list):
        return _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls, rn, round_entry, cfg, project_path, project_enabled, all_tools)

    target_url = fn_args.get('url', '')

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

    try:
        page_content = fetch_page_content(target_url, max_chars=_lib.FETCH_MAX_CHARS_DIRECT, pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF)
    except Exception as e:
        logger.error('[Executor] fetch_url failed for url=%s: %s', target_url, e, exc_info=True)
        page_content = None
    is_pdf = target_url.lower().rstrip('/').endswith('.pdf') or (page_content and page_content.startswith('[Page '))
    raw_chars = len(page_content) if page_content else 0
    if page_content and not is_pdf:
        user_question = task.get('lastUserQuery', '')
        fetch_reason = fn_args.get('reason', '')
        from lib.fetch.content_filter import IRRELEVANT_SENTINEL
        page_content = filter_web_content(
            page_content, url=target_url,
            query=fetch_reason, user_question=user_question,
        )
        if page_content == IRRELEVANT_SENTINEL:
            logger.info('[Executor] fetch_url IRRELEVANT: %s', target_url[:100])
            page_content = None
    filtered_chars = len(page_content) if page_content else 0
    from lib.tasks_pkg.tool_display import _short_url
    dr = {
        'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(target_url)}',
        'snippet': (f'{filtered_chars:,} chars' + (f' (filtered from {raw_chars:,})' if filtered_chars < raw_chars else '')) if page_content else 'Failed',
        'url': target_url, 'source': 'PDF' if is_pdf else 'Direct Fetch',
        'fetched': bool(page_content), 'fetchedChars': filtered_chars,
    }
    _finalize_tool_round(task, rn, round_entry, [dr], query_override=f'📄 {target_url}')
    tool_content = (f"Content from {target_url} ({filtered_chars:,} chars):\n\n{page_content}"
                    if page_content else f"Failed to fetch {target_url}.")
    return tc_id, tool_content, True


# ══════════════════════════════════════════════════════════
#  Batch handlers — web_search and fetch_url
# ══════════════════════════════════════════════════════════

def _handle_web_search_batch(task, tc, fn_name, tc_id, fn_args, queries, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle batch web_search: run multiple queries concurrently.

    Concatenates results for all queries into one tool response, with
    per-query headers. Each query's display_results are merged into the
    round_entry's results list for frontend rendering.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    handler_t0 = _time.time()
    user_question = task.get('lastUserQuery', '')
    MAX_BATCH = 5

    query_list = []
    for spec in queries[:MAX_BATCH]:
        if isinstance(spec, dict) and spec.get('query'):
            query_list.append(spec['query'])
        elif isinstance(spec, str) and spec.strip():
            query_list.append(spec.strip())
    if not query_list:
        tool_content = 'Error: "queries" must contain at least one {query} entry.'
        _finalize_tool_round(task, rn, round_entry, [{'type': 'error', 'content': tool_content}])
        return tc_id, tool_content, False

    n = len(query_list)

    def _search_one(q):
        """Run a single web search, returns (query, results, search_diag, engine_breakdown, formatted)."""
        search_diag = None
        engine_breakdown = None
        try:
            results = perform_web_search(q, user_question=user_question)
            search_diag = getattr(results, '_search_diag', None)
            engine_breakdown = getattr(results, '_engine_breakdown', None)
        except Exception as e:
            logger.error('[Executor] web_search batch failed for query=%r: %s', q, e, exc_info=True)
            results = []
            search_diag = {
                'reason': 'exception',
                'reason_detail': 'Search failed: %s' % str(e)[:200],
                'engine_errors': {}, 'engine_empty': [], 'engine_ok': [],
            }
        formatted = format_search_for_tool_response(results, search_diag=search_diag)
        return q, results, search_diag, engine_breakdown, formatted

    # Run all queries concurrently
    all_display_results = []
    all_formatted = []
    with ThreadPoolExecutor(max_workers=min(n, 5)) as pool:
        # Preserve order: map results back by query
        futures = {pool.submit(_search_one, q): i for i, q in enumerate(query_list)}
        ordered = [None] * n
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                ordered[idx] = fut.result()
            except Exception as e:
                q = query_list[idx]
                logger.error('[Search] batch web_search future failed for query=%r: %s', q, e, exc_info=True)
                ordered[idx] = (q, [], None, None, f'Search failed for "{q}": {e}')

    for item in ordered:
        if item is None:
            continue
        q, results, search_diag, engine_breakdown, formatted = item
        display_results = []
        for r in results:
            dr = {k: v for k, v in r.items() if k != 'full_content'}
            if r.get('full_content'):
                dr['fetched'] = True
                dr['fetchedChars'] = len(r['full_content'])
            display_results.append(dr)

        all_display_results.extend(display_results)
        # Add query header to formatted output for multi-query clarity
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
    append_event(task, event_payload)

    tool_content = '\n\n'.join(all_formatted)
    handler_elapsed = _time.time() - handler_t0
    logger.info('[Search] web_search BATCH: %d queries in %.1fs  total_results=%d  content_chars=%d',
                n, handler_elapsed, len(all_display_results), len(tool_content))
    return tc_id, tool_content, True


def _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls_specs, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle batch fetch_url: fetch multiple URLs concurrently.

    Concatenates results for all URLs into one tool response, with
    per-URL headers. Each URL's display_result is added to the
    round_entry's results list for frontend rendering.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    def _fetch_one(target_url):
        """Fetch a single URL, returns (url, page_content, is_pdf, raw_chars, filtered_chars, error_msg)."""
        from urllib.parse import urlparse as _urlparse
        scheme = _urlparse(target_url).scheme.lower()
        if scheme and scheme not in ('http', 'https', ''):
            logger.warning('[Fetch] Batch: rejected non-HTTP scheme=%r: %s', scheme, target_url[:120])
            return target_url, None, False, 0, 0, f'Rejected: {scheme}:// scheme (use read_files for local paths)'

        try:
            page_content = fetch_page_content(target_url, max_chars=_lib.FETCH_MAX_CHARS_DIRECT, pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF)
        except Exception as e:
            logger.error('[Executor] fetch_url batch failed for url=%s: %s', target_url, e, exc_info=True)
            return target_url, None, False, 0, 0, None

        is_pdf = target_url.lower().rstrip('/').endswith('.pdf') or (page_content and page_content.startswith('[Page '))
        raw_chars = len(page_content) if page_content else 0

        if page_content and not is_pdf:
            from lib.fetch.content_filter import IRRELEVANT_SENTINEL
            page_content = filter_web_content(
                page_content, url=target_url,
                query='', user_question=user_question,
            )
            if page_content == IRRELEVANT_SENTINEL:
                logger.info('[Executor] fetch_url batch IRRELEVANT: %s', target_url[:100])
                page_content = None

        filtered_chars = len(page_content) if page_content else 0
        return target_url, page_content, is_pdf, raw_chars, filtered_chars, None

    # Run all fetches concurrently
    from lib.tasks_pkg.tool_display import _short_url
    all_display_results = []
    all_parts = []
    with ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
        futures = {pool.submit(_fetch_one, url): i for i, url in enumerate(url_list)}
        ordered = [None] * n
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                ordered[idx] = fut.result()
            except Exception as e:
                url = url_list[idx]
                logger.error('[Fetch] batch fetch_url future failed for url=%s: %s', url, e, exc_info=True)
                ordered[idx] = (url, None, False, 0, 0, str(e))

    total_chars = 0
    BATCH_CHAR_BUDGET = 300_000
    for item in ordered:
        if item is None:
            continue
        target_url, page_content, is_pdf, raw_chars, filtered_chars, error_msg = item

        # Build display result
        if error_msg:
            dr = {
                'title': f'Rejected: {_short_url(target_url)}',
                'snippet': error_msg, 'url': target_url,
                'source': 'N/A', 'fetched': False, 'fetchedChars': 0,
            }
        else:
            dr = {
                'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(target_url)}',
                'snippet': (
                    f'{filtered_chars:,} chars' + (f' (filtered from {raw_chars:,})' if filtered_chars < raw_chars else '')
                ) if page_content else 'Failed',
                'url': target_url,
                'source': 'PDF' if is_pdf else 'Direct Fetch',
                'fetched': bool(page_content),
                'fetchedChars': filtered_chars,
            }
        all_display_results.append(dr)

        # Build text content for LLM
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


# Lazy import for content filter (used in fetch_url)
from lib.fetch.content_filter import filter_web_content  # noqa: E402

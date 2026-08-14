# HOT_PATH
"""web_search / fetch_url tool-handler orchestrators (single + batch).

MONKEYPATCH PARITY: the orchestrators call ``_web_search_one`` /
``_fetch_url_one`` THROUGH the package facade (``lib.tasks_pkg.handlers.search``)
at call time — NOT via a submodule-local import — so tests that patch
``lib.tasks_pkg.handlers.search._web_search_one`` /
``lib.tasks_pkg.handlers.search._fetch_url_one`` steer them exactly as before
the package split.
"""

from __future__ import annotations

import threading
from urllib.parse import urlparse

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from tofu_search.search import format_search_for_tool_response
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
from lib.tasks_pkg.manager import append_event

from lib.tasks_pkg.handlers.search._display import (
    _format_fetch_display,
    _format_search_display_for_results,
    _vertical_header_for_llm,
    _vertical_to_sse_payload,
)

logger = get_logger(__name__)


def _batch_progress_reporter(task, rn, round_entry, total, label_of):
    """Build an ``on_item`` callback that emits per-item batch progress.

    ★ WHY (pt_67ffc2b7). A batch call — ``web_search(queries=[a,b,c])`` or
    ``fetch_url(urls=[...])`` — is ONE tool round with ONE ``round_entry``, so
    its terminal ``tool_result`` is the round's ONLY observable transition. With
    a 2s query beside a 40s one, the user watches an undifferentiated spinner
    for 40s and cannot tell whether every query is slow or just one is. This
    reports each item AT ITS OWN completion instant.

    Deliberately a ``tool_progress`` and NOT a partial ``tool_result``: a
    result settles the round (``status='done'``), which would stop the spinner
    while N-1 queries are still running — trading a late-truth for an
    early-lie.

    The payload does NOT use ``chunk``: that field is run_command's live
    terminal buffer (``_handleToolProgress`` appends it to ``_partialOutput``,
    which ``tool_result`` later replaces wholesale), so routing search progress
    through it would print queries into a terminal pane and then lose them.

    Args:
        task: Live task dict (events appended).
        rn: The batch round number.
        round_entry: The batch round entry (for toolCallId + clocks).
        total: Total item count in the batch.
        label_of: ``(index, item) -> str`` producing the item's display label.

    Returns:
        An ``on_item(index, item, result, ok)`` callable for
        :func:`run_batch_concurrent`.
    """
    _done = {'n': 0}
    _lock = threading.Lock()

    def _on_item(idx, item, _result, ok):
        with _lock:
            _done['n'] += 1
            n_done = _done['n']
        try:
            label = label_of(idx, item)
        except Exception as e:
            logger.debug('[Search] batch label failed at idx=%d: %s', idx, e)
            label = str(idx + 1)
        try:
            _evt = build_event(
                EventType.TOOL_PROGRESS,
                roundNum=rn,
                toolCallId=(round_entry or {}).get('toolCallId', ''),
                toolName=(round_entry or {}).get('toolName') or '',
                detail='%d/%d · %s' % (n_done, total, label),
                batchItem=label,
                batchDone=n_done,
                batchTotal=total,
                batchOk=bool(ok),
            )
            _t_start = (round_entry or {}).get('tStart')
            if _t_start is not None:
                _evt['tStart'] = _t_start
            append_event(task, _evt)
        except Exception as e:
            logger.warning('[Search] batch progress emit failed at idx=%d '
                           '(non-fatal): %s', idx, e)

    return _on_item


# ══════════════════════════════════════════════════════════
#  web_search — single + batch
# ══════════════════════════════════════════════════════════

@tool_registry.handler('web_search', category='search',
                       description='Perform a web search and return formatted results')
def _handle_web_search(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # Resolve the search primitive THROUGH the package facade so a
    # package-level monkeypatch (patch('...search._web_search_one', ...)) steers it.
    from lib.tasks_pkg.handlers import search as _facade

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

    from lib.search_bridge import bind_search_browser
    with bind_search_browser(
            user_id=task.get('_userId', '') or '',
            client_id=cfg.get('browserClientId') or ''):
        results, search_diag, engine_breakdown, vertical_result = _facade._web_search_one(
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

    tool_content = format_search_for_tool_response(results, search_diag=search_diag, query=query)
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
    # Resolve the search primitive THROUGH the package facade (monkeypatch parity).
    from lib.tasks_pkg.handlers import search as _facade

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
        from lib.search_bridge import bind_search_browser
        with bind_search_browser(
                user_id=task.get('_userId', '') or '',
                client_id=cfg.get('browserClientId') or ''):
            results, search_diag, engine_breakdown, vertical_result = _facade._web_search_one(
                q, user_question, f, vertical=v)
        formatted = format_search_for_tool_response(results, search_diag=search_diag, query=q)
        if vertical_result:
            formatted = _vertical_header_for_llm(vertical_result) + formatted
        return (q, results, search_diag, engine_breakdown, formatted, vertical_result)

    ordered = run_batch_concurrent(query_specs, _worker, max_workers=5, tag='Search',
                                   abort=lambda: bool(task.get('aborted')),
                                   on_item=_batch_progress_reporter(
                                       task, rn, round_entry, n,
                                       lambda idx, _spec: query_list[idx]))

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
    # Resolve the fetch primitive THROUGH the package facade (monkeypatch parity).
    from lib.tasks_pkg.handlers import search as _facade

    # ★ Batch mode: if 'urls' array is present, fetch all concurrently
    urls = fn_args.get('urls')
    if urls and isinstance(urls, list):
        return _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls, rn, round_entry, cfg, project_path, project_enabled, all_tools)

    target_url = (fn_args.get('url') or '').strip()
    user_question = task.get('lastUserQuery', '')
    fetch_reason = fn_args.get('reason', '')

    # ── Guard: no target URL. Models sometimes emit a placeholder call like
    #    fetch_url({"reason": "placeholder", "urls": []}) — an empty `urls`
    #    array is falsy so it lands here with url=''. Reject it clearly
    #    instead of trying to fetch the empty string (which produced the
    #    confusing "Failed to fetch ." rows). ──
    if not target_url:
        logger.warning('[Fetch] fetch_url called with no URL: args=%.200s',
                       str(fn_args)[:200])
        tool_content = (
            'Error: fetch_url requires a non-empty "url" (or a "urls" array '
            'with at least one entry). No URL was provided, so nothing was '
            'fetched. Pass the page URL you want to read.'
        )
        dr = {
            'title': 'No URL provided',
            'snippet': 'fetch_url was called without a url — nothing to fetch',
            'url': '', 'source': 'N/A',
            'fetched': False, 'fetchedChars': 0,
        }
        _finalize_tool_round(task, rn, round_entry, [dr], query_override='fetch_url')
        return tc_id, tool_content, False

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

    from lib.search_bridge import bind_search_browser
    with bind_search_browser(
            user_id=task.get('_userId', '') or '',
            client_id=cfg.get('browserClientId') or ''):
        item = _facade._fetch_url_one(
            target_url, user_question, fetch_reason=fetch_reason)

    from lib.tasks_pkg.tool_display import _short_url
    dr = _format_fetch_display(item, _short_url)
    _finalize_tool_round(task, rn, round_entry, [dr], query_override=f'📄 {target_url}')

    page_content = item['page_content']
    filtered_chars = item['filtered_chars']
    # A failure MUST carry its typed reason (soft_blocked / irrelevant / …).
    # A bare "Failed to fetch" tells the model nothing, so it retries sibling
    # hosts of the same dead origin — the exact loop this reason text kills.
    error_msg = item.get('error_msg')
    tool_content = (f"Content from {target_url} ({filtered_chars:,} chars):\n\n{page_content}"
                    if page_content
                    else f"Failed to fetch {target_url}."
                         + (f' ({error_msg})' if error_msg else ''))
    return tc_id, tool_content, True


def _handle_fetch_url_batch(task, tc, fn_name, tc_id, fn_args, urls_specs, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle batch fetch_url: fetch multiple URLs concurrently.

    Concatenates results for all URLs into one tool response, with
    per-URL headers. Each URL's display_result is added to the
    round_entry's results list for frontend rendering.
    """
    # Resolve the fetch primitive THROUGH the package facade (monkeypatch parity).
    from lib.tasks_pkg.handlers import search as _facade

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
        from lib.search_bridge import bind_search_browser
        with bind_search_browser(
                user_id=task.get('_userId', '') or '',
                client_id=cfg.get('browserClientId') or ''):
            return _facade._fetch_url_one(
                target_url, user_question, fetch_reason='')

    ordered = run_batch_concurrent(url_list, _worker, max_workers=8, tag='Fetch',
                                   abort=lambda: bool(task.get('aborted')),
                                   on_item=_batch_progress_reporter(
                                       task, rn, round_entry, n,
                                       lambda idx, _u: url_list[idx]))

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

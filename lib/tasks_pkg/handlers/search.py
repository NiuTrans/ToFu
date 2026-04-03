# HOT_PATH
"""Search-related tool handlers: tool_search, web_search, fetch_url."""

from __future__ import annotations

import json
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
    query = fn_args.get('query', '')
    user_question = task.get('lastUserQuery', '')
    search_diag = None
    try:
        results = perform_web_search(query, user_question=user_question)
        search_diag = getattr(results, '_search_diag', None)
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
    if not display_results and search_diag:
        round_entry['searchDiag'] = search_diag
        event_payload['searchDiag'] = search_diag
    append_event(task, event_payload)
    tool_content = format_search_for_tool_response(results, search_diag=search_diag)
    return tc_id, tool_content, True


@tool_registry.handler('fetch_url', category='search',
                       description='Fetch and extract content from a URL')
def _handle_fetch_url(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    target_url = fn_args.get('url', '')
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
    dr = {
        'title': f'{"PDF" if is_pdf else "Page"}: {urlparse(target_url).netloc}',
        'snippet': (f'{filtered_chars:,} chars' + (f' (filtered from {raw_chars:,})' if filtered_chars < raw_chars else '')) if page_content else 'Failed',
        'url': target_url, 'source': 'PDF' if is_pdf else 'Direct Fetch',
        'fetched': bool(page_content), 'fetchedChars': filtered_chars,
    }
    _finalize_tool_round(task, rn, round_entry, [dr], query_override=f'📄 {target_url}')
    tool_content = (f"Content from {target_url} ({filtered_chars:,} chars):\n\n{page_content}"
                    if page_content else f"Failed to fetch {target_url}.")
    return tc_id, tool_content, True


# Lazy import for content filter (used in fetch_url)
from lib.fetch.content_filter import filter_web_content  # noqa: E402

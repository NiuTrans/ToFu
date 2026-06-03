"""Tool execution for the paper-report agent.

Wraps ``perform_web_search`` and ``fetch_page_content`` with the chat-mode
display schema expected by the frontend's ``renderToolRoundsHTML``.
"""

import json

import lib as _lib
from lib.fetch import fetch_page_content
from lib.log import get_logger
from lib.search import format_search_for_tool_response, perform_web_search
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent

logger = get_logger(__name__)


def _execute_report_tool(name, args_str, user_question=''):
    """Execute a tool call from the report agent.

    Returns:
        tuple: (tool_content_str, display_results, search_diag)
            - tool_content_str: Formatted text for the LLM.
            - display_results: List of dicts for the frontend (same schema as
              chat mode's tool_result event).
            - search_diag: Diagnostic dict when search returns 0 results, else None.
    """
    try:
        args = json.loads(args_str) if args_str else {}
    except json.JSONDecodeError as e:
        logger.warning('[Paper:Report:Tool] Bad JSON args for %s: %s', name, e)
        return f'Error: invalid arguments JSON — {e}', [], None

    if name == 'web_search':
        queries = args.get('queries', [])
        if not queries:
            q = args.get('query', '')
            if q:
                queries = [{'query': q}]
        if not queries:
            return 'Error: no query provided', [], None

        query_list = []
        for qobj in queries[:5]:
            q = qobj.get('query', '') if isinstance(qobj, dict) else str(qobj)
            if q and q.strip():
                query_list.append(q.strip())
        if not query_list:
            return 'Error: no valid query provided', [], None

        def _search_one(q):
            logger.info('[Paper:Report:Tool] web_search query=%r', q[:100])
            results = perform_web_search(q, user_question=user_question)
            search_diag = getattr(results, '_search_diag', None)
            formatted = format_search_for_tool_response(results, search_diag=search_diag)
            # Build display results (strip full_content for frontend)
            display = []
            for r in results:
                dr = {k: v for k, v in r.items() if k != 'full_content'}
                if r.get('full_content'):
                    dr['fetched'] = True
                    dr['fetchedChars'] = len(r['full_content'])
                display.append(dr)
            return (formatted, display, search_diag)

        ordered = run_batch_concurrent(query_list, _search_one, max_workers=2, tag='PaperSearch')

        all_formatted = []
        all_display = []
        last_diag = None
        for idx, item in enumerate(ordered):
            q = query_list[idx]
            if item is None:
                all_formatted.append(f'Search for "{q}" failed: internal error')
            else:
                formatted, display, diag = item
                all_display.extend(display)
                if diag:
                    last_diag = diag
                if len(query_list) > 1:
                    all_formatted.append(f'=== Search: {q} ===\n{formatted}')
                else:
                    all_formatted.append(formatted)

        tool_content = '\n\n'.join(all_formatted)
        # Only propagate diag if we ended up with 0 display results
        final_diag = last_diag if not all_display else None
        return tool_content, all_display, final_diag

    elif name == 'fetch_url':
        urls = args.get('urls', [])
        if not urls:
            u = args.get('url', '')
            if u:
                urls = [{'url': u}]
        if not urls:
            return 'Error: no url provided', [], None

        url_list = []
        for uobj in urls[:5]:
            u = uobj.get('url', '') if isinstance(uobj, dict) else str(uobj)
            if u and u.strip():
                url_list.append(u.strip())
        if not url_list:
            return 'Error: no valid url provided', [], None

        def _fetch_one(u):
            logger.info('[Paper:Report:Tool] fetch_url url=%.100s', u)
            content = fetch_page_content(
                u,
                max_chars=_lib.FETCH_MAX_CHARS_DIRECT,
                pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF,
            )
            is_pdf = (u.lower().rstrip('/').endswith('.pdf')
                      or (content and content.startswith('[Page ')))
            if content:
                text = f"Content from {u} ({len(content):,} chars):\n\n{content}"
            else:
                text = f"Failed to fetch {u}."
            # Short host+path for display
            try:
                from urllib.parse import urlparse
                p = urlparse(u)
                short = (p.netloc or '') + (p.path or '')[:40]
            except ValueError as e:
                logger.debug('[Paper] urlparse failed for %r: %s', u[:80], e)
                short = u[:50]
            display = {
                'title': f'{"PDF" if is_pdf else "Page"}: {short}',
                'snippet': (f'{len(content):,} chars' if content
                            else 'Failed to fetch'),
                'url': u,
                'source': 'PDF' if is_pdf else 'Direct Fetch',
                'fetched': bool(content),
                'fetchedChars': len(content) if content else 0,
            }
            return (text, display)

        ordered = run_batch_concurrent(url_list, _fetch_one, max_workers=3, tag='PaperFetch')

        all_parts = []
        all_display = []
        for idx, item in enumerate(ordered):
            if item is None:
                all_parts.append(f'Fetch {url_list[idx]} failed: internal error')
                all_display.append({
                    'title': f'Page: {url_list[idx][:50]}',
                    'snippet': 'Internal error',
                    'url': url_list[idx],
                    'source': 'Direct Fetch',
                    'fetched': False, 'fetchedChars': 0,
                })
            else:
                text, display = item
                all_parts.append(text)
                all_display.append(display)
        return '\n\n---\n\n'.join(all_parts), all_display, None

    else:
        return f'Unknown tool: {name}', [], None

# HOT_PATH
"""Tool summary generation (mechanical, zero LLM calls) + user-URL prefetch."""

from __future__ import annotations

import json
from typing import Any

import lib as _lib  # module ref for hot-reload

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.protocols import FetchService
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

# NOTE: Do NOT re-export _lib.FETCH_* as module-level copies here.
# Module-level copies become stale after reload_config() — always read
# from _lib.<VAR> at call time to pick up hot-reloaded values.


# ── Tool summary generation (mechanical, zero LLM calls) ──

def _generate_tool_summary(
    messages: list[dict[str, Any]],
    model: str,
    task: dict[str, Any],
) -> str | None:
    """Lightweight mechanical summary: tool name + key args only.
    No result excerpts — the model already saw them and wrote its reply.
    Purpose: tell the model *what actions were taken* so it won't repeat them.
    Zero LLM calls, < 1ms.
    """
    pfx = f'[Task {task["id"][:8]}]'

    lines = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                fn = tc.get('function', {})
                name = fn.get('name', '?')
                args_raw = fn.get('arguments', '')
                try:
                    args_obj = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception as e:
                    logger.debug('[Executor] tool args JSON parse failed for %s: %s (err=%s)', name, str(args_raw)[:80], e, exc_info=True)
                    args_obj = args_raw
                # Compact arg display: key=value, values truncated at 60 chars
                if isinstance(args_obj, dict):
                    parts = []
                    for k, v in args_obj.items():
                        sv = v if isinstance(v, str) else repr(v)
                        if len(sv) > 60:
                            sv = sv[:57] + '...'
                        parts.append(f'{k}={sv}')
                    brief = ', '.join(parts)
                else:
                    brief = str(args_obj)[:100]
                lines.append(f'- {name}({brief})')
            # Skip past tool result messages
            i += 1
            while i < len(messages) and messages[i].get('role') == 'tool':
                i += 1
        else:
            i += 1

    if not lines:
        return None

    summary = '\n'.join(lines)
    logger.debug('%s Tool summary: %d calls, %d chars', pfx, len(lines), len(summary))
    return summary


# ── Prefetch URLs from user messages ──

def _prefetch_user_urls(
    messages: list[dict[str, Any]],
    task: dict[str, Any],
    *,
    fetch_service: FetchService | None = None,
) -> list[tuple[str, str]]:
    """Extract URLs from the latest user message and pre-fetch their content.

    Parameters
    ----------
    messages : list[dict]
        Conversation message list.
    task : dict
        Live task dict — mutated (toolRounds appended, events emitted).
    fetch_service : FetchService, optional
        Optional :class:`~lib.protocols.FetchService` for dependency injection.
        When provided, ``fetch_service.fetch_urls()`` is used instead of the
        concrete ``tofu_search.fetch_urls`` import.  Pass a mock for testing.
        ``None`` (default) falls back to the concrete import.

    Returns
    -------
    list[tuple[str, str]]
        List of ``(url, fetched_content)`` pairs for successfully fetched URLs.
    """
    # tofu_search is imported HERE, not at module level, so a failure inside it
    # (it pulls trafilatura → lxml → libicuuc, which on 2026-07-31 raised a
    # GLIBCXX linkage ImportError eight times) degrades URL prefetch instead of
    # killing the whole server. This module sits on the boot chain via
    # routes/paper.py → lib/paper → handlers → executor, so a module-level
    # import here is a whole-process hazard for one optional capability.
    from tofu_search import extract_urls_from_text, fetch_urls
    from tofu_search.fetch.content_filter import filter_web_contents_batch

    last_text = ''
    for msg in reversed(messages):
        if msg.get('role') != 'user': continue
        c = msg.get('content', '')
        if isinstance(c, list):
            last_text = ' '.join(p.get('text','') for p in c if isinstance(p,dict) and p.get('type')=='text')
        elif isinstance(c, str): last_text = c
        break

    urls = extract_urls_from_text(last_text)
    if not urls: return []
    logger.debug('[Task %s] Pre-fetching %d URL(s)', task['id'][:8], len(urls))
    round_entries = []
    for url in urls:
        rn = len(task['toolRounds']) + 1
        entry = {'roundNum': rn, 'query': f'📄 {url}', 'results': None, 'status': 'searching', 'toolName': 'fetch_url'}
        task['toolRounds'].append(entry)
        round_entries.append((url, entry, rn))
        append_event(task, build_event(EventType.TOOL_START, roundNum=rn, query=f'Fetching {url[:80]}', toolName='fetch_url'))
    # Dispatch through protocol or concrete import
    _fetch_urls = fetch_service.fetch_urls if fetch_service is not None else fetch_urls
    fetched = _fetch_urls(urls, max_chars=_lib.FETCH_MAX_CHARS_DIRECT, pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF, timeout=_lib.FETCH_TIMEOUT)
    # ── LLM content filter for pre-fetched URLs ──
    to_filter = [(url, text) for url, text in fetched.items()
                 if text and len(text) > 1500
                 and not (url.lower().rstrip('/').endswith('.pdf') or text.startswith('[Page '))]
    if to_filter:
        user_query = last_text[:500]   # use user message as query context
        logger.info('[Prefetch] LLM-filtering %d/%d fetched pages, query=%r',
                    len(to_filter), len(fetched), user_query[:80])
        filtered = filter_web_contents_batch(to_filter, query=user_query)
        for url in filtered:
            fetched[url] = filtered[url]
    else:
        logger.debug('[Prefetch] no pages to LLM-filter (%d fetched, all short/pdf/empty)',
                     len(fetched))
    from lib.tasks_pkg.tool_display import _short_url
    for url, entry, rn in round_entries:
        content = fetched.get(url)
        is_pdf = url.lower().rstrip('/').endswith('.pdf') or (content and content.startswith('[Page '))
        entry['results'] = [{'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(url)}',
            'snippet': f'{len(content):,} chars extracted' if content else 'Failed to fetch',
            'url': url, 'source': 'PDF' if is_pdf else 'Direct Fetch',
            'fetched': bool(content), 'fetchedChars': len(content) if content else 0}]
        entry['status'] = 'done'
        append_event(task, build_event(EventType.TOOL_RESULT, roundNum=rn, query=f'📄 {url}', results=entry['results']))
    return [(url, fetched[url]) for url in urls if url in fetched]

# HOT_PATH
"""Tool execution — unified dispatch for all tool types + tool summary generation."""

from __future__ import annotations

import json
from typing import Any

import lib as _lib  # module ref for hot-reload
from lib.fetch import extract_urls_from_text, fetch_urls
from lib.fetch.content_filter import filter_web_contents_batch
from lib.log import get_logger
from lib.protocols import FetchService, ToolHandler
from lib.swarm.tools import SWARM_TOOL_NAMES  # noqa: F401 — re-exported for tool_dispatch/tool_display
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

# NOTE: Do NOT re-export _lib.FETCH_* as module-level copies here.
# Module-level copies become stale after reload_config() — always read
# from _lib.<VAR> at call time to pick up hot-reloaded values.

# ══════════════════════════════════════════════════════════
#  ToolRegistry — formal registry pattern for tool dispatch
# ══════════════════════════════════════════════════════════

class ToolRegistry:
    """Central registry for tool handlers with metadata.

    Supports three registration modes:
    - **exact**: a single tool name → handler (fastest lookup).
    - **set-based**: a ``frozenset`` of tool names → handler (checked in order).
    - **special**: a key like ``'__code_exec__'`` matched via ``round_entry``
      rather than ``fn_name``.

    All registration methods have corresponding decorator forms to
    co-locate handler definitions with their registration::

        registry = ToolRegistry()

        @registry.handler('web_search', category='search',
                          description='Perform a web search via API')
        def _handle_web_search(task, tc, fn_name, ...):
            ...

        @registry.tool_set(BROWSER_TOOL_NAMES, category='browser',
                           description='Execute a browser automation tool')
        def _handle_browser_tool(task, tc, fn_name, ...):
            ...

        @registry.special('__code_exec__', category='code',
                           description='Execute a shell command')
        def _handle_code_exec(task, tc, fn_name, ...):
            ...

        # Lookup at dispatch time
        handler = registry.lookup(fn_name, round_entry)
    """

    def __init__(self) -> None:
        self._exact: dict[str, ToolHandler] = {}          # name → handler
        self._sets: list[tuple[frozenset, ToolHandler]] = []  # (name_set, handler)
        self._metadata: dict[str, dict[str, str]] = {}    # name → {category, description}
        self._special: dict[str, ToolHandler] = {}        # key → handler (e.g. __code_exec__)

    # ── Registration ──────────────────────────────────────

    def register(self, names, handler: ToolHandler, *, category: str = '', description: str = ''):
        """Register *handler* for one or more exact tool names.

        Parameters
        ----------
        names : str | set | frozenset | list
            Tool name(s) to register.
        handler : ToolHandler
            Handler function satisfying the :class:`~lib.protocols.ToolHandler` protocol.
        category : str
            Logical grouping (e.g. ``'search'``, ``'browser'``).
        description : str
            Human-readable description of what the handler does.
        """
        if isinstance(names, str):
            names = {names}
        for name in names:
            self._exact[name] = handler
            self._metadata[name] = {'category': category, 'description': description}

    def register_set(self, name_set, handler: ToolHandler, *, category: str = '', description: str = ''):
        """Register *handler* for a set of tool names (checked in order).

        Unlike ``register()``, set-based entries are checked sequentially
        after exact matches, preserving priority ordering.
        """
        self._sets.append((frozenset(name_set), handler))
        meta = {'category': category, 'description': description}
        for name in name_set:
            self._metadata.setdefault(name, meta)

    def register_special(self, key: str, handler: ToolHandler, *, category: str = '', description: str = ''):
        """Register a handler for a special dispatch key (e.g. ``'__code_exec__'``).

        Special handlers are matched via ``round_entry`` metadata rather
        than ``fn_name`` directly.
        """
        self._special[key] = handler
        self._metadata[key] = {'category': category, 'description': description}

    def handler(self, names, *, category='', description=''):
        """Decorator form of :meth:`register`.

        Example::

            @registry.handler('web_search', category='search',
                              description='Web search via API')
            def _handle_web_search(task, tc, fn_name, ...):
                ...
        """
        def decorator(fn):
            self.register(names, fn, category=category, description=description)
            return fn
        return decorator

    def tool(self, name: str, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register` for a single tool name.

        Example::

            @registry.tool('web_search', category='search',
                           description='Perform a web search via API')
            def _handle_web_search(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register(name, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register(name, fn, category=category, description=description)
            return fn
        return decorator

    def special(self, key: str, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register_special`.

        Example::

            @registry.special('__code_exec__', category='code',
                               description='Execute a shell command')
            def _handle_code_exec(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register_special(key, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register_special(key, fn, category=category, description=description)
            return fn
        return decorator

    def tool_set(self, name_set, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register_set`.

        Co-locates the registration with the handler definition, eliminating
        the need for a separate imperative ``register_set()`` call.

        Example::

            @registry.tool_set(BROWSER_TOOL_NAMES, category='browser',
                               description='Execute a browser automation tool')
            def _handle_browser_tool(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register_set(name_set, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register_set(name_set, fn, category=category, description=description)
            return fn
        return decorator

    # ── Lookup ────────────────────────────────────────────

    def lookup(self, fn_name: str, round_entry: dict[str, Any] | None = None) -> ToolHandler | None:
        """Find the handler for *fn_name*.

        Lookup order:
        1. Exact-name match (O(1) dict lookup).
        2. Special ``code_exec`` check via ``round_entry['toolName']``.
        3. Set-based match (linear scan, first match wins).
        4. ``None`` if no handler found.
        """
        # 1. Exact
        h = self._exact.get(fn_name)
        if h is not None:
            return h

        # 2. Special: code_exec identified by round_entry, not fn_name
        if round_entry and round_entry.get('toolName') == 'code_exec':
            h = self._special.get('__code_exec__')
            if h is not None:
                return h

        # 3. Set-based
        for name_set, handler in self._sets:
            if fn_name in name_set:
                return handler

        return None

    # ── Introspection ─────────────────────────────────────

    def list_tools(self):
        """Return a list of ``(name, category, description)`` for all registered tools."""
        seen = set()
        result = []
        # Exact registrations first
        for name in self._exact:
            if name not in seen:
                meta = self._metadata.get(name, {})
                result.append((name, meta.get('category', ''), meta.get('description', '')))
                seen.add(name)
        # Special registrations
        for key in self._special:
            if key not in seen:
                meta = self._metadata.get(key, {})
                result.append((key, meta.get('category', ''), meta.get('description', '')))
                seen.add(key)
        # Set-based registrations
        for name_set, _ in self._sets:
            for name in sorted(name_set):
                if name not in seen:
                    meta = self._metadata.get(name, {})
                    result.append((name, meta.get('category', ''), meta.get('description', '')))
                    seen.add(name)
        return result

    def __contains__(self, fn_name):
        """Support ``fn_name in registry`` syntax."""
        return self.lookup(fn_name) is not None

    def __repr__(self):
        n_exact = len(self._exact)
        n_sets = sum(len(s) for s, _ in self._sets)
        n_special = len(self._special)
        return f'<ToolRegistry exact={n_exact} set_names={n_sets} special={n_special}>'


# Module-level singleton — all tool handlers register here.
tool_registry = ToolRegistry()


# ══════════════════════════════════════════════════════════
#  Shared tool-handler helpers — DRY finalization & meta
# ══════════════════════════════════════════════════════════

def _finalize_tool_round(
    task: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    results: list,
    *,
    query_override: str = '',
    extra_event_fields: dict[str, Any] | None = None,
) -> None:
    """Finalize a tool round: set results & status, emit the SSE event.

    This replaces the 3-line boilerplate repeated in every tool handler::

        round_entry['results'] = results
        round_entry['status'] = 'done'
        append_event(task, {'type': 'tool_result', ...})

    Parameters
    ----------
    task : dict
        Live task dict — event is appended.
    rn : int
        Round number for the event.
    round_entry : dict
        The search-round entry dict to finalize.
    results : list
        List of result meta dicts (usually ``[meta]``).
    query_override : str, optional
        If provided, overrides ``round_entry['query']`` in the event.
    extra_event_fields : dict, optional
        Additional fields to merge into the SSE event payload
        (e.g. ``{'engineBreakdown': ...}``).
    """
    round_entry['results'] = results
    round_entry['status'] = 'done'
    event = {
        'type': 'tool_result',
        'roundNum': rn,
        'query': query_override or round_entry['query'],
        'results': results,
    }
    if extra_event_fields:
        event.update(extra_event_fields)
    append_event(task, event)


def _build_simple_meta(
    fn_name: str,
    tool_content,
    *,
    source: str,
    icon: str = '',
    badge: str = '',
    title: str = '',
    snippet: str = '',
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard tool result meta dict.

    Handles the common pattern where handlers build near-identical dicts
    with ``toolName``, ``title``, ``snippet``, ``source``, ``fetched``,
    ``fetchedChars``, and ``badge``.  Any extra keys can be merged via
    *extra*.

    Parameters
    ----------
    fn_name : str
        Tool function name.
    tool_content : str | Any
        Raw tool output — used for ``fetchedChars`` and default snippet.
    source : str
        Source label (e.g. ``'Scheduler'``, ``'Swarm'``).
    icon : str
        Emoji prefix for the default title and badge.
    badge : str
        Badge text (defaults to *icon* if not provided).
    title : str
        Override title (defaults to ``'{icon} {fn_name}'``).
    snippet : str
        Override snippet (defaults to first 120 chars of *tool_content*).
    extra : dict, optional
        Additional keys merged into the meta dict.
    """
    content_str = tool_content if isinstance(tool_content, str) else str(tool_content)
    meta = {
        'toolName': fn_name,
        'title': title or (f'{icon} {fn_name}' if icon else fn_name),
        'snippet': snippet or content_str[:120].replace('\n', ' '),
        'source': source,
        'fetched': True,
        'fetchedChars': len(content_str),
        'badge': badge or icon,
    }
    if extra:
        meta.update(extra)
    return meta


# ── Content-ref resolver — resolve tool_round references to actual content ──

def _resolve_content_ref(
    task: dict[str, Any],
    content_ref: dict[str, Any],
) -> str | None:
    """Resolve a ``content_ref`` to actual text from a previous tool round.

    Looks up the referenced ``tool_round`` number in ``task['toolRounds']``
    and returns the ``toolContent`` stored there.  Supports optional
    ``start``/``end`` for substring extraction.

    Parameters
    ----------
    task : dict
        Live task dict with ``toolRounds`` list.
    content_ref : dict
        Reference dict with keys: ``tool_round`` (required), ``start`` and
        ``end`` (optional character indices).

    Returns
    -------
    str or None
        The resolved content string, or ``None`` if the round was not found
        or has no toolContent.
    """
    round_num = content_ref.get('tool_round')
    if round_num is None:
        logger.warning('[content_ref] Missing tool_round in content_ref: %s', content_ref)
        return None

    for sr in task.get('toolRounds', []):
        if sr.get('roundNum') == round_num:
            content = sr.get('toolContent', '')
            if not content:
                logger.warning('[content_ref] tool_round=%d found but toolContent is empty', round_num)
                return None
            start = content_ref.get('start')
            end = content_ref.get('end')
            if start is not None or end is not None:
                content = content[start:end]
                logger.info('[content_ref] Resolved tool_round=%d with slice [%s:%s] → %d chars',
                            round_num, start, end, len(content))
            else:
                logger.info('[content_ref] Resolved tool_round=%d → %d chars (full content)',
                            round_num, len(content))
            return content

    logger.warning('[content_ref] tool_round=%d not found in %d toolRounds',
                   round_num, len(task.get('toolRounds', [])))
    return None


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
        concrete ``lib.fetch.fetch_urls`` import.  Pass a mock for testing.
        ``None`` (default) falls back to the concrete import.

    Returns
    -------
    list[tuple[str, str]]
        List of ``(url, fetched_content)`` pairs for successfully fetched URLs.
    """
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
        append_event(task, {'type': 'tool_start', 'roundNum': rn, 'query': f'Fetching {url[:80]}', 'toolName': 'fetch_url'})
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
        append_event(task, {'type': 'tool_result', 'roundNum': rn, 'query': f'📄 {url}', 'results': entry['results']})
    return [(url, fetched[url]) for url in urls if url in fetched]


# ══════════════════════════════════════════════════════════
#  Tool handlers — extracted to lib/tasks_pkg/handlers/
#  Importing the handlers package triggers @tool_registry registration.
# ══════════════════════════════════════════════════════════
import lib.tasks_pkg.handlers  # noqa: F401, E402 — triggers all handler registrations


def _execute_tool_one(
    task: dict[str, Any],
    tc: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    all_tools: list[dict] | None = None,
) -> tuple[str, str, bool]:
    """Execute a single tool call.  Returns (tc_id, tool_content_str, is_search).
    Also updates round_entry & emits tool_result events as a side-effect.

    Dispatch is handled by :data:`tool_registry` — a :class:`ToolRegistry`
    singleton that supports exact-name, special, and set-based lookup.
    """
    # ★ Abort check: skip execution if user already clicked Stop
    if task.get('aborted'):
        logger.info('[Executor] Skipping tool %s (tc_id=%s) — task aborted', fn_name, tc_id[:8])
        return tc_id, 'Task aborted by user.', False

    # ★ Per-client browser routing: propagate client_id to worker threads
    #   (ThreadPoolExecutor threads don't inherit the parent's thread-locals)
    _browser_cid = cfg.get('browserClientId')
    if _browser_cid:
        from lib.browser import _set_active_client
        _set_active_client(_browser_cid)

    handler = tool_registry.lookup(fn_name, round_entry)
    if handler is not None:
        # ★ Universal exception safety net: any uncaught exception inside
        # a tool handler (unexpected arg shape, downstream bug, I/O failure…)
        # is converted into an error tool-result returned to the LLM, so the
        # model can see what went wrong and retry with corrected parameters
        # instead of the whole task aborting with no result for this round.
        try:
            return handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                           cfg, project_path, project_enabled, all_tools)
        except Exception as e:
            _arg_preview = ''
            try:
                _arg_preview = json.dumps(fn_args, ensure_ascii=False)[:300]
            except Exception as _dump_err:
                logger.debug('[Executor] fn_args dump failed for %s: %s',
                             fn_name, _dump_err)
                _arg_preview = repr(fn_args)[:300]
            logger.error(
                '[Executor] Tool handler %s raised %s (tc_id=%s args=%.300s) — '
                'returning error to LLM so it can retry',
                fn_name, type(e).__name__, tc_id[:8], _arg_preview,
                exc_info=True,
            )
            err_msg = (
                f'Error: tool "{fn_name}" execution failed with '
                f'{type(e).__name__}: {e}. Check the parameter schema '
                f'(types, required fields) and retry with corrected arguments. '
                f'Arguments received: {_arg_preview}'
            )
            # Finalize the round so the UI doesn't show a dangling
            # "searching…" tool forever.
            if round_entry is not None and round_entry.get('status') != 'done':
                try:
                    _finalize_tool_round(
                        task, rn, round_entry,
                        [{'type': 'error', 'content': err_msg,
                          'toolName': fn_name}],
                        query_override=round_entry.get('query', fn_name),
                    )
                except Exception as _fin_err:
                    logger.debug('[Executor] _finalize_tool_round on error path '
                                 'failed for %s: %s', fn_name, _fin_err)
            return tc_id, err_msg, False

    # ── unknown tool ──
    logger.warning('[Executor] Unknown tool requested: %s', fn_name)
    tool_content = (
        f'Error: unknown tool "{fn_name}". This tool is not registered. '
        f'Verify the tool name against the available tool list and retry.'
    )
    return tc_id, tool_content, False

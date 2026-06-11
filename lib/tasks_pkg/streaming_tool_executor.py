"""Streaming Tool Executor — start executing read-only tools while the model streams.

Inspired by Claude Code's ``StreamingToolExecutor`` (``tools/StreamingToolExecutor.ts``).
When the model emits multiple tool calls in one response, read-only tools
(``read_files``, ``grep_search``, ``find_files``, ``list_dir``, ``web_search``,
``fetch_url``) begin executing as soon as their arguments
finish streaming, rather than waiting for the complete response.

Write tools and approval-gated tools are NOT pre-executed — they are deferred
to the normal serial dispatch in ``tool_dispatch.py``.

Architecture
------------
1. The orchestrator creates a ``StreamingToolAccumulator`` before each LLM call.
2. The ``on_tool_call_ready`` callback is passed through
   ``stream_llm_response`` → ``dispatch_stream`` → ``stream_chat`` →
   ``_stream_chat_once``.
3. Each time a tool call's arguments finish during SSE streaming, the callback
   fires immediately.
4. **NEW**: The callback also immediately emits ``tool_start`` SSE events so
   the frontend can show "Searching…" / "Running…" UI without waiting for the
   entire LLM response to finish streaming.
5. If the tool is read-only and concurrency-safe, it is submitted to a thread
   pool for immediate execution — **while the model is still generating the
   next tool call**.
6. After the stream completes, the orchestrator calls ``inject_into_cache()``
   to harvest results.  Already-done results are collected immediately;
   still-running futures are **waited on** (not cancelled), since they are
   already in-progress and would be executed serially otherwise — waiting
   is strictly faster than cancelling + re-executing from scratch.
7. The results are stored in the task's ``_tool_result_cache`` dict, keyed
   exactly like ``tool_dispatch._make_cache_key``.  When
   ``execute_tool_pipeline`` runs, it finds pre-computed results in the
   dedup cache and skips re-execution.
"""

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor

from lib.log import get_logger

logger = get_logger(__name__)


class _ContentWithDisplayResults(str):
    """String subclass that carries display_results metadata.

    Used by ``_execute_one`` for web_search to pass both the formatted
    LLM content (as a string) and the display results for the frontend,
    through the existing cache pipeline that expects string content.

    Attributes:
        display_results: List of result dicts for frontend rendering.
        search_diag: Optional diagnostic dict when search returns 0 results.
        engine_breakdown: Optional dict mapping engine tag → list of raw URLs.
    """
    def __new__(cls, content: str, display_results: list):
        instance = super().__new__(cls, content)
        instance.display_results = display_results
        instance.search_diag = None
        instance.engine_breakdown = None
        instance.vertical = None
        return instance

# ── Read-only tools safe to pre-execute during streaming ──
# These must have NO side effects (idempotent) and be concurrency-safe.
_STREAMABLE_TOOLS = frozenset({
    'read_files', 'grep_search', 'find_files', 'list_dir',
    'web_search', 'fetch_url',
})

# ── Internal tool prefixes to skip (proxy artifacts, not real tools) ──
_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')


class StreamingToolAccumulator:
    """Accumulates tool calls during streaming and pre-executes read-only ones.

    Also emits ``tool_start`` SSE events immediately as each tool call is
    parsed from the stream, so the frontend shows the tool status without
    waiting for the entire LLM response to finish.

    Usage::

        acc = StreamingToolAccumulator(
            task, project_path,
            tool_round_num=tool_round_num,
            round_num=round_num,
            project_enabled=project_enabled,
        )
        msg, finish, usage = stream_llm_response(
            task, body, tag='R1',
            on_tool_call_ready=acc.on_tool_call_ready,
        )
        # Read back the updated tool_round_num
        tool_round_num = acc.tool_round_num
        # Inject completed results into dedup cache
        hit_count = acc.inject_into_cache(task)
        # Now parse_tool_calls will skip re-emitting for already-announced tools
        parsed_tcs, tool_round_num = parse_tool_calls(
            assistant_msg, task, round_num, tool_round_num, project_enabled,
            early_announced=acc.announced_tc_map,
        )

    Args:
        task: Live task dict.
        project_path: Base path for project tools (may be None).
        tool_round_num: Current tool round counter (will be incremented).
        round_num: Current orchestrator loop round (for llmRound tagging).
        project_enabled: Whether project-mode is active.
    """

    def __init__(self, task: dict, project_path: str | None,
                 tool_round_num: int = 0, round_num: int = 0,
                 project_enabled: bool = False):
        self._task = task
        self._project_path = project_path
        self._tool_round_num = tool_round_num
        self._round_num = round_num
        self._project_enabled = project_enabled
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix='stream-tool')
        # tc_id → (future, fn_name, fn_args, submit_time)
        self._futures: dict[str, tuple[Future, str, dict, float]] = {}
        self._submitted_count = 0
        self._tid = task['id'][:8]
        # tc_id → (rn, round_entry) for tools already announced via tool_start
        self._announced: dict[str, tuple[int, dict]] = {}
        self._first_announced = True  # for assistantContent tagging

    @property
    def tool_round_num(self) -> int:
        """Current tool_round_num (updated as tools are announced)."""
        return self._tool_round_num

    @property
    def announced_tc_map(self) -> dict[str, tuple[int, dict]]:
        """Map of tc_id → (roundNum, round_entry) for already-announced tools."""
        return dict(self._announced)

    def on_tool_call_ready(self, tool_call: dict):
        """Callback fired when a tool call's arguments finish streaming.

        Called from ``_stream_chat_once`` in the SSE delta processing loop.

        1. Emits a ``tool_start`` SSE event for ALL tools immediately
           (so the frontend shows "Searching…" / "Running…" right away).
        2. Submits read-only, concurrency-safe tools for pre-execution.
        """
        fn_name = tool_call.get('function', {}).get('name', '')
        tc_id = tool_call.get('id', '')
        fn_args_raw = tool_call.get('function', {}).get('arguments', '')

        if not fn_name or not tc_id:
            return

        # Skip internal/spurious tool names (proxy artifacts)
        if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
            return

        # Don't announce if task is aborted
        if self._task.get('aborted'):
            return

        # Note: we do NOT filter empty-args tool calls here.  During streaming
        # we can't tell phantom calls (model started a slot, never sent args)
        # from legitimate no-arg tools.  The post-stream
        # filter in lib/llm/stream.py handles phantom detection using same-name
        # comparison.  A stray tool_start event for a phantom is harmless — it
        # just won't get a matching tool_done.

        # ── Parse arguments ──
        try:
            fn_args = json.loads(fn_args_raw) if fn_args_raw.strip() else {}
        except (json.JSONDecodeError, TypeError) as _e_audit:
            # Can't parse → still emit tool_start with empty args for UI feedback
            logger.debug('[streaming_tool_executor] on_tool_call_ready caught %s: %s', type(_e_audit).__name__, _e_audit)
            fn_args = {}

        # ── Emit tool_start SSE event immediately ──
        try:
            self._emit_tool_start(fn_name, fn_args, tc_id, fn_args_raw or '{}')
        except Exception as e:
            logger.debug('[%s] StreamingToolExec: tool_start emission failed '
                         'for %s: %s', self._tid, fn_name, e)

        # ── Pre-execute read-only tools ──
        if fn_name in _STREAMABLE_TOOLS and fn_args:
            self._submitted_count += 1
            t0 = time.time()
            logger.info('[%s] StreamingToolExec: pre-executing %s (tc_id=%s) '
                        'while model streams',
                        self._tid, fn_name, tc_id[:8])

            future = self._pool.submit(
                self._execute_one, fn_name, fn_args
            )
            self._futures[tc_id] = (future, fn_name, fn_args, t0)

    def _emit_tool_start(self, fn_name: str, fn_args: dict, tc_id: str,
                         tc_args_str: str):
        """Emit a tool_start SSE event + append round entry to task.

        Uses the same ``_build_tool_round_entry`` as ``parse_tool_calls``
        to ensure consistent roundNum assignment and display formatting.

        Requires ``task['toolRounds']`` and ``task['events_lock']`` to exist.
        Silently skips if the task doesn't have these (e.g. in unit tests).
        """
        # Guard: skip if task is not fully initialised (e.g. unit tests)
        if 'toolRounds' not in self._task:
            return

        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.tool_display import _build_tool_round_entry

        self._tool_round_num, round_entry, event_payload = _build_tool_round_entry(
            fn_name, fn_args, tc_id, tc_args_str,
            self._tool_round_num, self._project_enabled,
            conv_id=self._task.get('convId') or self._task.get('id'),
        )
        rn = round_entry['roundNum']

        # Tag with LLM round (same as parse_tool_calls does)
        round_entry['llmRound'] = self._round_num
        event_payload['llmRound'] = self._round_num

        # Append to task's toolRounds and emit SSE event
        self._task['toolRounds'].append(round_entry)
        append_event(self._task, event_payload)

        # Track as announced
        self._announced[tc_id] = (rn, round_entry)

        logger.info('[%s] StreamingToolExec: early tool_start emitted for '
                    '%s (tc_id=%s, rn=%d) — UI shows activity immediately',
                    self._tid, fn_name, tc_id[:8], rn)

    def _execute_one(self, fn_name: str, fn_args: dict) -> str:
        """Execute a single read-only tool call in a background thread.

        Uses the same underlying tool functions as the normal pipeline
        but without the event/round_entry overhead.

        Returns:
            Tool result content as string.
        """
        # ★ Abort check: skip execution if user already clicked Stop
        if self._task.get('aborted'):
            logger.info('[%s] StreamingToolExec: skipping %s — task aborted',
                        self._tid, fn_name)
            return 'Task aborted by user.'

        try:
            if fn_name in ('read_files', 'grep_search', 'find_files',
                           'list_dir'):
                from lib.project_mod.tools import execute_tool
                # ★ Pass conv_id so namespaced paths resolve against this
                #   conversation's root registry (prevents concurrent-task
                #   clobber — see lib/project_mod/config.py::set_conv_roots).
                _conv_id = self._task.get('convId') or self._task.get('id') or ''
                return execute_tool(fn_name, fn_args,
                                    self._project_path or '.',
                                    conv_id=_conv_id)

            elif fn_name == 'web_search':
                from tofu_search import perform_web_search
                from tofu_search.search import format_search_for_tool_response
                from lib.tasks_pkg.handlers.search import (
                    resolve_vertical, _vertical_to_sse_payload, _vertical_header_for_llm,
                )
                # ★ Batch mode: run concurrent searches (lightweight, no SSE events)
                queries = fn_args.get('queries')
                batch_vertical = fn_args.get('vertical', 'auto')
                if queries and isinstance(queries, list):
                    from concurrent.futures import ThreadPoolExecutor as _TP, as_completed as _ac
                    user_q = self._task.get('lastUserQuery', '')
                    batch_freshness = fn_args.get('freshness', '')
                    specs = []
                    for s in queries[:5]:
                        if isinstance(s, dict) and s.get('query'):
                            specs.append((s['query'],
                                          s.get('vertical') or batch_vertical,
                                          s.get('freshness', '') or batch_freshness))
                        elif isinstance(s, str) and s.strip():
                            specs.append((s.strip(), batch_vertical, batch_freshness))

                    def _do_search(spec):
                        q, v, f = spec
                        plan = resolve_vertical(q, v)
                        v_fut = None
                        if plan is not None:
                            from concurrent.futures import ThreadPoolExecutor as _TP1
                            _vp = _TP1(max_workers=1)
                            v_fut = _vp.submit(plan)
                        r = perform_web_search(q, user_question=user_q, freshness=f)
                        sd = getattr(r, '_search_diag', None)
                        fmt = format_search_for_tool_response(r, search_diag=sd)
                        v_rec = None
                        if v_fut is not None:
                            try:
                                v_rec = v_fut.result(timeout=5)
                            except Exception as ve:
                                logger.warning('[%s] StreamingToolExec: vertical(%s) failed: %s',
                                               self._tid, v, ve)
                        if v_rec:
                            fmt = _vertical_header_for_llm(v_rec) + fmt
                        return q, r, fmt, v_rec

                    parts = [None] * len(specs)
                    results_per_q = [None] * len(specs)
                    verticals = []
                    with _TP(max_workers=min(len(specs), 5)) as pool:
                        futs = {pool.submit(_do_search, sp): i for i, sp in enumerate(specs)}
                        for f in _ac(futs):
                            idx = futs[f]
                            try:
                                q, r, fmt, v_rec = f.result()
                                parts[idx] = f'=== Search: {q} ===\n{fmt}' if len(specs) > 1 else fmt
                                results_per_q[idx] = r
                                if v_rec:
                                    payload = _vertical_to_sse_payload(v_rec)
                                    if payload:
                                        payload = dict(payload)
                                        payload['query'] = q
                                        verticals.append(payload)
                            except Exception as e:
                                logger.debug('[%s] StreamingToolExec: batch search query %r failed: %s',
                                             self._tid, specs[idx][0][:80], e)
                                parts[idx] = f'Search failed for "{specs[idx][0]}": {e}'
                                results_per_q[idx] = []
                    all_display_results = []
                    for idx, r in enumerate(results_per_q):
                        if not r:
                            continue
                        src_q = specs[idx][0] if idx < len(specs) else ''
                        for item in r:
                            dr = {k: v for k, v in item.items() if k != 'full_content'}
                            if item.get('full_content'):
                                dr['fetched'] = True
                                dr['fetchedChars'] = len(item['full_content'])
                            # Tag with source query for per-query grouping in UI
                            dr['_q'] = src_q
                            all_display_results.append(dr)
                    formatted = _ContentWithDisplayResults(
                        '\n\n'.join(p for p in parts if p),
                        all_display_results,
                    )
                    if verticals:
                        formatted.vertical = {'batch': verticals}
                    return formatted

                # ── Single query ──
                query = fn_args.get('query', '')
                vertical_param = fn_args.get('vertical', 'auto')
                freshness = fn_args.get('freshness', '')
                user_question = self._task.get('lastUserQuery', '')
                plan = resolve_vertical(query, vertical_param)
                v_fut = None
                if plan is not None:
                    from concurrent.futures import ThreadPoolExecutor as _TP1
                    _vp = _TP1(max_workers=1)
                    v_fut = _vp.submit(plan)
                results = perform_web_search(query,
                                             user_question=user_question,
                                             freshness=freshness)
                search_diag = getattr(results, '_search_diag', None)
                engine_breakdown = getattr(results, '_engine_breakdown', None)
                formatted_text = format_search_for_tool_response(results,
                                                                 search_diag=search_diag)
                v_rec = None
                if v_fut is not None:
                    try:
                        v_rec = v_fut.result(timeout=5)
                    except Exception as ve:
                        logger.warning('[%s] StreamingToolExec: vertical(%s) failed: %s',
                                       self._tid, vertical_param, ve)
                if v_rec:
                    formatted_text = _vertical_header_for_llm(v_rec) + formatted_text
                display_results = []
                for r in results:
                    dr = {k: v for k, v in r.items() if k != 'full_content'}
                    if r.get('full_content'):
                        dr['fetched'] = True
                        dr['fetchedChars'] = len(r['full_content'])
                    display_results.append(dr)
                formatted = _ContentWithDisplayResults(formatted_text, display_results)
                if not display_results and search_diag:
                    formatted.search_diag = search_diag
                if engine_breakdown:
                    formatted.engine_breakdown = engine_breakdown
                if v_rec:
                    formatted.vertical = _vertical_to_sse_payload(v_rec)
                return formatted

            elif fn_name == 'fetch_url':
                from tofu_search import fetch_page_content
                # ★ Batch mode: run concurrent fetches (lightweight, no SSE events)
                urls = fn_args.get('urls')
                if urls and isinstance(urls, list):
                    from concurrent.futures import ThreadPoolExecutor as _TP, as_completed as _ac
                    import lib as _lib_ref
                    from lib.tasks_pkg.tool_display import _short_url
                    url_list = [
                        (s.get('url') if isinstance(s, dict) else s)
                        for s in urls[:10]
                        if (isinstance(s, dict) and s.get('url')) or (isinstance(s, str) and s.strip())
                    ]
                    def _do_fetch(u):
                        c = fetch_page_content(
                            u, max_chars=_lib_ref.FETCH_MAX_CHARS_DIRECT,
                            pdf_max_chars=_lib_ref.FETCH_MAX_CHARS_PDF,
                        )
                        is_pdf = (u.lower().rstrip('/').endswith('.pdf')
                                  or (c and c.startswith('[Page ')))
                        return c, is_pdf
                    parts = [None] * len(url_list)
                    display_results = [None] * len(url_list)
                    with _TP(max_workers=min(len(url_list), 8)) as pool:
                        futs = {pool.submit(_do_fetch, u): i for i, u in enumerate(url_list)}
                        for f in _ac(futs):
                            idx = futs[f]
                            u = url_list[idx]
                            try:
                                c, is_pdf = f.result()
                                if c:
                                    parts[idx] = f"Content from {u} ({len(c):,} chars):\n\n{c}"
                                    display_results[idx] = {
                                        'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(u)}',
                                        'snippet': f'{len(c):,} chars',
                                        'url': u,
                                        'source': 'PDF' if is_pdf else 'Direct Fetch',
                                        'fetched': True,
                                        'fetchedChars': len(c),
                                    }
                                else:
                                    parts[idx] = f"Failed to fetch {u}."
                                    display_results[idx] = {
                                        'title': f'Page: {_short_url(u)}',
                                        'snippet': 'Failed',
                                        'url': u,
                                        'source': 'Direct Fetch',
                                        'fetched': False,
                                        'fetchedChars': 0,
                                    }
                            except Exception as e:
                                logger.debug('[%s] StreamingToolExec: batch fetch %r failed: %s',
                                             self._tid, u, e)
                                parts[idx] = f"Failed to fetch {u}: {e}"
                                display_results[idx] = {
                                    'title': f'Page: {_short_url(u)}',
                                    'snippet': f'Error: {str(e)[:120]}',
                                    'url': u,
                                    'source': 'Direct Fetch',
                                    'fetched': False,
                                    'fetchedChars': 0,
                                }
                    formatted = _ContentWithDisplayResults(
                        '\n\n'.join(p for p in parts if p),
                        [d for d in display_results if d is not None],
                    )
                    return formatted
                url = fn_args.get('url', '')
                import lib as _lib_ref
                content = fetch_page_content(
                    url,
                    max_chars=_lib_ref.FETCH_MAX_CHARS_DIRECT,
                    pdf_max_chars=_lib_ref.FETCH_MAX_CHARS_PDF,
                )
                if content:
                    return (f"Content from {url} "
                            f"({len(content):,} chars):\n\n{content}")
                return f"Failed to fetch {url}."

            return ''

        except Exception as e:
            # UnknownWorkspaceRootError is already logged ONCE as WARNING
            # at the raise site (lib/project_mod/tools.py) and will be
            # re-logged at INFO by executor._execute_tool_one after the
            # normal-pipeline fallback. Keep it at INFO here too so we
            # don't triple-log the same event in error.log.
            try:
                from lib.project_mod.config import UnknownWorkspaceRootError
                if isinstance(e, UnknownWorkspaceRootError):
                    logger.info(
                        '[%s] StreamingToolExec: pre-exec of %s hit '
                        'unknown workspace root (recoverable, returned '
                        'to LLM): %s', self._tid, fn_name, e)
                    raise
            except ImportError as _imp:
                logger.debug('[%s] UnknownWorkspaceRootError import '
                             'failed: %s', self._tid, _imp)
            logger.warning('[%s] StreamingToolExec: pre-exec of %s failed: %s',
                           self._tid, fn_name, e)
            raise

    @staticmethod
    def _normalize_image_result(content):
        """Convert image dict results to __screenshot__ protocol.

        read_files returns ``{'__batch_images__': {idx: screenshot_dict}, '_text_content': ...}``
        for image files.  The handler in ``handlers/project.py`` normally converts
        this to a ``__screenshot__`` dict, but the streaming executor bypasses handlers.

        Without this conversion, ``str(content)`` on the batch dict would dump
        800K+ of base64 text into the cache, which then gets injected as plain text
        into the conversation context (blowing up the token count).

        Returns:
            The original content (if not an image dict), or the extracted
            ``__screenshot__`` dict (preserving _text_fallback).
        """
        if not isinstance(content, dict):
            return content
        # Single __screenshot__ — already in the right format
        if content.get('__screenshot__'):
            return content
        # __batch_images__ — extract first image, attach text fallback
        if content.get('__batch_images__'):
            images = content['__batch_images__']
            text = content.get('_text_content', '')
            if images:
                first_img = next(iter(images.values()))
                if isinstance(first_img, dict) and first_img.get('__screenshot__'):
                    if text and not first_img.get('_text_fallback'):
                        first_img['_text_fallback'] = text
                    return first_img
        return content

    def _prepare_cache_value(self, content, fn_name):
        """Prepare a tool result for cache storage.

        Handles image dicts by preserving them as-is (not stringifying)
        so the post-phase can detect ``__screenshot__`` and convert to
        ``image_url`` blocks instead of dumping base64 as plain text.

        Returns:
            (cache_content, content_len_for_log)
        """
        # Normalize image results from read_files
        content = self._normalize_image_result(content)
        if isinstance(content, dict) and content.get('__screenshot__'):
            # Log compressed size instead of len(dict) which would be key count
            sz = content.get('compressedSize', 0)
            return content, sz
        content_str = str(content) if not isinstance(content, str) else content
        return content_str, len(content_str)

    def inject_into_cache(self, task: dict) -> int:
        """Inject pre-execution results into the dedup cache.

        Waits for ALL submitted futures to complete (with a timeout),
        since these tools would be executed serially by the normal pipeline
        anyway — waiting for already-running work is strictly faster than
        cancelling and re-executing from scratch.

        Returns:
            Count of successfully injected results.
        """
        if '_tool_result_cache' not in task:
            task['_tool_result_cache'] = {}
        cache = task['_tool_result_cache']

        from lib.tasks_pkg.tool_dispatch import _make_cache_key

        injected = 0
        # First pass: collect already-done futures immediately
        pending = []
        for tc_id, (future, fn_name, fn_args, t0) in self._futures.items():
            if future.done() and not future.cancelled():
                try:
                    content = future.result(timeout=0)
                    elapsed = time.time() - t0
                    is_search = fn_name in ('web_search',)
                    cache_key = _make_cache_key(fn_name, fn_args)
                    # Extract display_results + engine_breakdown + vertical (web_search)
                    _disp = getattr(content, 'display_results', None)
                    _eng_bkdn = getattr(content, 'engine_breakdown', None)
                    _vert = getattr(content, 'vertical', None)
                    cache_val, content_len = self._prepare_cache_value(content, fn_name)
                    cache[cache_key] = (cache_val, is_search, 'prefetch', _disp, _eng_bkdn, _vert)
                    injected += 1
                    logger.info('[%s] StreamingToolExec: injected %s into '
                                'dedup cache (%.1fs, %d chars%s)',
                                self._tid, fn_name, elapsed, content_len,
                                ', %d display_results' % len(_disp) if _disp else '')
                except Exception as e:
                    logger.debug('[%s] StreamingToolExec: %s pre-exec failed, '
                                 'deferring to normal pipeline: %s',
                                 self._tid, fn_name, e)
            elif not future.done() and not future.cancelled():
                pending.append((tc_id, future, fn_name, fn_args, t0))

        # Second pass: wait for still-running futures — they're already
        # in-progress and would be executed serially anyway, so waiting
        # is always faster than cancelling + re-executing.
        # BUT: if user aborted, cancel remaining futures immediately.
        if pending and task.get('aborted'):
            logger.info('[%s] StreamingToolExec: task aborted — cancelling %d '
                        'pending tool(s): %s',
                        self._tid, len(pending),
                        ', '.join(fn for _, _, fn, _, _ in pending))
            for tc_id, future, fn_name, fn_args, t0 in pending:
                future.cancel()
        elif pending:
            logger.info('[%s] StreamingToolExec: waiting for %d still-running '
                        'tool(s): %s',
                        self._tid, len(pending),
                        ', '.join(fn for _, _, fn, _, _ in pending))
            for tc_id, future, fn_name, fn_args, t0 in pending:
                # Check abort between each future wait
                if task.get('aborted'):
                    logger.info('[%s] StreamingToolExec: abort detected while '
                                'waiting — cancelling remaining', self._tid)
                    future.cancel()
                    continue
                try:
                    # ★ Timeout should match the underlying tool's I/O
                    #   timeout (cross-DC multiplier adjusts for slow
                    #   FUSE/NFS mounts — see lib.cross_dc).  The old
                    #   hard-coded 60s threw away in-flight rg work on
                    #   slow mounts, only for the serial pipeline to
                    #   then re-run the same rg from scratch → wasted
                    #   60s + a fresh full scan.  We now align, so the
                    #   pre-execution's result gets injected.
                    #
                    #   Grace window: _run_grep_subprocess kills the
                    #   subprocess *at* io_timeout and then spends up to
                    #   ~5s collecting partial output.  If we wait for
                    #   exactly io_timeout we race the kill-and-collect
                    #   phase and abandon the in-flight result, only for
                    #   the serial pipeline to re-execute the same query
                    #   for another full io_timeout.  Add a 10s slack so
                    #   the partial-results banner gets cached instead.
                    _wait_timeout = 60
                    if fn_name in ('grep_search', 'read_files',
                                   'find_files', 'list_dir'):
                        try:
                            from lib.project_mod.read_tools import _get_io_timeout
                            _wait_timeout = _get_io_timeout(
                                self._project_path or '.', default=60) + 10
                        except Exception as _e:
                            logger.debug('[%s] StreamingToolExec: cross-DC '
                                         'timeout probe unavailable: %s',
                                         self._tid, _e)
                    content = future.result(timeout=_wait_timeout)
                    elapsed = time.time() - t0
                    is_search = fn_name in ('web_search',)
                    cache_key = _make_cache_key(fn_name, fn_args)
                    _disp = getattr(content, 'display_results', None)
                    _eng_bkdn = getattr(content, 'engine_breakdown', None)
                    _vert = getattr(content, 'vertical', None)
                    cache_val, content_len = self._prepare_cache_value(content, fn_name)
                    cache[cache_key] = (cache_val, is_search, 'prefetch', _disp, _eng_bkdn, _vert)
                    injected += 1
                    logger.info('[%s] StreamingToolExec: waited and injected '
                                '%s into dedup cache (%.1fs, %d chars%s)',
                                self._tid, fn_name, elapsed, content_len,
                                ', %d display_results' % len(_disp) if _disp else '')
                except TimeoutError:
                    logger.warning('[%s] StreamingToolExec: %s timed out after '
                                   '%ds, deferring to normal pipeline',
                                   self._tid, fn_name, _wait_timeout)
                except Exception as e:
                    logger.debug('[%s] StreamingToolExec: %s pre-exec failed, '
                                 'deferring to normal pipeline: %s',
                                 self._tid, fn_name, e)

        # Shutdown thread pool — cancel futures on abort, wait otherwise
        _aborted = task.get('aborted', False)
        self._pool.shutdown(wait=not _aborted, cancel_futures=_aborted)

        _total = self._submitted_count
        if _total > 0:
            logger.info('[%s] StreamingToolExec summary: %d submitted, '
                        '%d pre-computed and injected into cache',
                        self._tid, _total, injected)
        return injected

    @property
    def submitted_count(self) -> int:
        """Number of tools submitted for pre-execution."""
        return self._submitted_count

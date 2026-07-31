"""Tool execution for the paper-report agent — a THIN ADAPTER over chat's seams.

Two execution families, both owned by chat and reused here:

* ``web_search`` / ``fetch_url`` — chat's helpers (``_web_search_one`` /
  ``_fetch_url_one`` from ``lib.tasks_pkg.handlers.search``) so read-mode tool
  rounds emit the EXACT same display schema the frontend's
  ``renderToolRoundsHTML`` expects — vertical cards, engine-source breakdown,
  filtered-vs-raw char counts, File-Asset staging labels, rejected-scheme rows.
  Never re-implement the search/fetch call here: a parallel implementation
  silently drops whatever fields the chat helper computes.
* everything else (read_files / code_exec / memory / todo / scheduler / …) —
  routed through the SHARED single-tool dispatch
  (``lib.tasks_pkg.executor._execute_tool_one``), so the exact handlers chat
  runs serve the paper engines too. ``_execute_report_tool`` only translates
  paper's 5-tuple result contract + event/display schema; per-tool branches
  must NEVER grow here (charter: fix the chassis, don't patch the caller).

Also here: ``make_paper_exec_shim`` (the task-dict shim the shared dispatch
expects, with the explicit unattended auto-approval policy), ``cap_tool_result``
(the honest bounding contract — chat's spill-to-disk where read_files can page
the result back, an explicit TRUNCATED marker where it cannot), and
``paper_effective_tool_name`` (the no-project run_command → code_exec flip).
"""

import json

from lib.log import get_logger
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
from lib.tasks_pkg.handlers.search import (
    _fetch_url_one,
    _format_fetch_display,
    _format_search_display_for_results,
    _vertical_header_for_llm,
    _vertical_to_sse_payload,
    _web_search_one,
)
# Reuse chat's canonical seams — DON'T reimplement them here:
#   • parse_and_repair_tool_args → JSON-decode + schema repair (the
#     bare-string-`queries` → single-element-array fix lives in ONE place,
#     so it covers chat AND the paper agents at once).
#   • tool_round_label → the exact string/dict-safe label chat renders,
#     incl. the multi-line batch form and empty-list guards.
from lib.tasks_pkg.tool_display import _short_url
from lib.tasks_pkg.tool_display import tool_round_label as display_query_for  # noqa: F401 — re-exported for the report/QA engines
from lib.tool_input_repair import parse_and_repair_tool_args  # noqa: F401 — re-exported for the report/QA engines
from tofu_search.search import format_search_for_tool_response

logger = get_logger(__name__)

# Re-exported canonical helpers (chat's seams) the paper engines import from
# this module. Listed here so the re-export is intentional, not dead code.
__all__ = ['_execute_report_tool', 'parse_and_repair_tool_args', 'display_query_for',
           'make_research_tool_executor', 'make_paper_exec_shim', 'cap_tool_result',
           'paper_effective_tool_name']


def paper_effective_tool_name(fn_name):
    """The dispatch/display tool name for a call in a paper (project-less) engine.

    Mirrors chat's tool_display override (``_build_tool_round_entry``): with no
    project attached, ``run_command`` IS the standalone code_exec tool (its
    schema is a deepcopy of the project run_command schema), and the registry
    keys its special ``__code_exec__`` handler + the frontend's terminal-block
    rendering off ``round_entry['toolName'] == 'code_exec'`` — not
    ``'run_command'``. Without this flip a paper ``run_command`` call would
    fall through to the PROJECT handler and die with "No project path".
    """
    from lib.tools import CODE_EXEC_TOOL_NAMES
    if fn_name in CODE_EXEC_TOOL_NAMES:
        return 'code_exec'
    return fn_name


def make_paper_exec_shim(*, task_id, conv_id='', abort=None, cfg=None):
    """Build the shim task dict the SHARED dispatch (``_execute_tool_one``)
    expects, for a headless paper engine run.

    Approval policy (EXPLICIT — do not change silently): chat's write-approval
    gate lives in the batch pipeline and fires ONLY for ATTENDED tasks (a
    human must be present to answer); unattended / headless chat tasks
    auto-apply. Paper engines are unattended by construction — there is no
    human to prompt — so they inherit chat's own unattended semantics:
    write-partition tools (memory CRUD, scheduler create/manage, MCP tools)
    execute without a prompt, and EVERY such call is recorded via
    ``audit_log('paper_tool_auto_approve', …)`` in ``_execute_shared_tool``.
    Never route a paper engine through the attended pipeline (a background
    task must never block on a click that cannot happen), and never strip the
    audit trail (it is the visible record of this policy).

    ``_suppressEvents`` follows the swarm sub-agent precedent
    (``lib/tasks_pkg/manager/_events.py:append_event``): the inner handler's
    ``_finalize_tool_round`` / progress events never leak onto a stream — the
    paper engines emit their OWN ``tool_start`` / ``tool_done`` events from
    the finalized ``round_entry``.
    """
    return {
        'id': task_id,
        'convId': conv_id,
        '_suppressEvents': True,
        # Mirrored from the engine's abort predicate on EVERY call by
        # _execute_shared_tool (the shared dispatch reads task['aborted']).
        'aborted': bool(abort and abort()),
        '_abort': abort,
        '_cfg': dict(cfg or {}),
    }


def cap_tool_result(content, tool_name, tool_use_id='', *, conv_id='',
                    can_read=False):
    """Bound a tool result before it enters the message history — honestly.

    Paper engines ride ``run_agent_loop``, which has NO compaction layer, and
    historically HARD-TRUNCATED every tool result at 30k chars — silently
    dropping the tail (the model then hallucinated the missing content). Chat's
    Layer-0 (``compaction/_budget``) instead spills oversized results to disk
    with a preview + path the model pages back via read_files. This helper
    gives each engine family the honest version of that contract:

    * ``can_read=True`` (report / Q&A — full tool set incl. read_files): spill
      via the SAME ``_persist_to_disk`` chat uses; the returned summary carries
      real file paths the model CAN open (read_files is in its tool set).
    * ``can_read=False`` (insight / recommend / ideate — research-only set, no
      read_files): truncate with an EXPLICIT marker naming the dropped size,
      so the model knows the content is incomplete instead of guessing.
    """
    from lib.tasks_pkg.compaction._constants import (
        _DEFAULT_TOOL_RESULT_MAX,
        TOOL_RESULT_MAX_CHARS,
    )
    if not isinstance(content, str):
        return content
    cap = TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX)
    # A 0 table value means BUDGET-EXEMPT (e.g. read_files — chat's L0 never
    # caps it, see _BUDGET_EXEMPT_TOOLS): pass through whole, never spill.
    if not cap or len(content) <= cap:
        return content
    if can_read:
        from lib.tasks_pkg.compaction._persist import _persist_to_disk
        return _persist_to_disk(content, tool_name, tool_use_id, conv_id)
    logger.info('[Paper:Tool] %s result %d chars over %d budget — explicit '
                'truncation (engine has no read_files)', tool_name,
                len(content), cap)
    return (
        f'{content[:cap]}\n\n'
        f'[TRUNCATED — the tool result was {len(content):,} chars, over the '
        f'{cap:,}-char budget for "{tool_name}". The tail was dropped and is '
        f'NOT recoverable in this engine (no read_files tool). Do NOT '
        f'fabricate the missing content — narrow the query and retry if you '
        f'need it.]'
    )


def _execute_shared_tool(name, args, shim, round_entry, abort):
    """Route one non-search tool call through chat's SHARED single-tool dispatch.

    Returns the same 5-tuple ``_execute_report_tool`` produces for search
    tools; the display payload is whatever the chat handler finalized onto
    ``round_entry['results']`` (a ``_build_simple_meta`` / project-meta list —
    the exact shape ``renderToolRoundsHTML`` already renders for chat).
    """
    from lib.log import audit_log
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS

    # Refresh the abort mirror — the shared dispatch reads task['aborted'].
    shim['aborted'] = bool(abort and abort())

    tc_id = (round_entry or {}).get('toolCallId', '')
    # Unattended auto-approval — explicit + audited (see make_paper_exec_shim).
    # MCP tools get chat's conservative default-write classification.
    if name in _WRITE_TOOLS or name.startswith('mcp__'):
        audit_log('paper_tool_auto_approve', tool=name,
                  task_id=shim.get('id', ''),
                  reason='unattended_headless_engine')
        logger.info('[Paper:Tool] auto-approved write-partition tool %s '
                    '(unattended engine, task=%s)', name,
                    str(shim.get('id', ''))[:8])

    tc = {'id': tc_id,
          'function': {'name': name,
                       'arguments': json.dumps(args, ensure_ascii=False)}}
    try:
        _tc_id, content, _is_search = _execute_tool_one(
            shim, tc, name, tc_id, args,
            (round_entry or {}).get('roundNum', 0),
            round_entry if round_entry is not None
            else {'query': name, 'toolCallId': tc_id},
            shim.get('_cfg') or {}, None, False)
    except Exception as e:
        logger.error('[Paper:Tool] shared dispatch of %s failed: %s',
                     name, e, exc_info=True)
        return (f'Error: tool "{name}" execution failed: {e}',
                [], None, None, None)

    # Image reads come back as a __screenshot__ DICT — the paper message
    # channel is text-only, so degrade to the text fallback with an explicit
    # note instead of crashing on dict slicing downstream.
    if isinstance(content, dict):
        if content.get('__screenshot__'):
            fallback = content.get('_text_fallback') or ''
            content = (
                (fallback + '\n\n' if fallback else '')
                + f"[Image loaded: {content.get('filename', '?')} — the paper "
                  "channel is text-only, so the image itself is not attached. "
                  "Work from the fallback text above or the file path.]")
        else:
            content = json.dumps(content, ensure_ascii=False)

    display = list((round_entry or {}).get('results') or [])
    return content, display, None, None, None


def _execute_report_tool(name, args_str, user_question='', abort=None,
                         force_vertical=None, exec_shim=None, round_entry=None):
    """Execute a tool call from the report agent.

    Args:
        name: tool name (``web_search`` / ``fetch_url``).
        args_str: raw tool-call arguments (JSON string, schema-repaired here).
        user_question: short context string for search relevance filtering.
        abort: optional ``() -> bool`` predicate. When it trips, queued
            (not-yet-started) items in a batch short-circuit instead of firing
            — so a Stop pressed while a report is mid-search does not spray the
            remaining batched queries/fetches. Threaded down to
            ``run_batch_concurrent``.
        force_vertical: optional vertical domain (e.g. ``'academic'``) that
            OVERRIDES whatever vertical the model chose for every web_search
            query in this call — including ``'auto'`` / ``'off'`` / a wrong
            domain. Default ``None`` leaves the model's choice untouched, so
            the shared report / QA / insight callers are byte-identical. The
            describe-to-recommend engine passes ``'academic'`` so a known-title
            lookup always consults the arXiv / Semantic Scholar JSON APIs
            (whose uptime is independent of the HTML-engine fleet and its
            per-engine circuit breakers), rather than hoping the model asks.
        exec_shim: optional shim task dict from ``make_paper_exec_shim``. When
            provided, ANY tool name beyond web_search / fetch_url is routed
            through chat's shared single-tool dispatch (full-set engines:
            report + Q&A). When absent (research-only engines), unknown names
            keep the legacy ``Unknown tool`` reply — a hallucinated tool name
            can never escape into the shared dispatch.
        round_entry: the caller's chat-shaped round entry dict. The shared
            handler finalizes it in place (``results`` + ``status``), and the
            adapter returns those results as the display payload.

    Returns:
        tuple: (tool_content_str, display_results, search_diag, engine_breakdown, verticals)
            - tool_content_str: Formatted text for the LLM.
            - display_results: List of dicts for the frontend (same schema as
              chat mode's tool_result event). In batch search each dict is
              tagged with ``_q`` (its source query) for per-query grouping.
            - search_diag: Diagnostic dict when search returns 0 results, else None.
            - engine_breakdown: Per-engine raw URL breakdown for a single-query
              web_search (mirrors chat); None for batch / fetch_url.
            - verticals: List of vertical-search payloads (HF Papers / arXiv /
              Semantic Scholar / …) for the frontend's vertical card, or None.
    """
    # JSON-decode + schema repair in one place (mirrors the chat dispatcher).
    # This coerces a schema-violating ``queries``/``urls`` string into a
    # single-element array BEFORE the per-item loops below, so a bare string
    # is never iterated character-by-character.
    args, _repair_log = parse_and_repair_tool_args(name, args_str)

    if name == 'web_search':
        freshness = args.get('freshness', '')
        batch_vertical = args.get('vertical', 'auto')
        queries = args.get('queries', [])
        # Defensive: if repair could not normalize ``queries`` into a list
        # (e.g. tool schema unavailable), treat any non-list as a single
        # query rather than iterating it. Never iterate a raw string.
        if queries and not isinstance(queries, list):
            queries = [queries]
        if not queries:
            q = args.get('query', '')
            if q:
                queries = [{'query': q}]
        if not queries:
            return 'Error: no query provided', [], None, None, None

        # Build (query, freshness, vertical) specs — mirrors chat's batch path.
        query_specs = []
        for qobj in queries[:5]:
            if isinstance(qobj, dict):
                q = qobj.get('query', '')
                f = qobj.get('freshness', '') or freshness
                v = qobj.get('vertical') or batch_vertical
            elif isinstance(qobj, str):
                q, f, v = qobj, freshness, batch_vertical
            else:
                continue
            # A forced vertical (e.g. the recommend engine's 'academic') wins
            # over the model's choice — the robust JSON-API path is guaranteed
            # by code, not left to the model's discretion.
            if force_vertical:
                v = force_vertical
            if q and q.strip():
                query_specs.append((q.strip(), f, v))
        if not query_specs:
            return 'Error: no valid query provided', [], None, None, None

        query_list = [q for q, _, _ in query_specs]
        single = len(query_specs) == 1

        def _search_one(spec):
            q, f, v = spec
            logger.info('[Paper:Report:Tool] web_search query=%r', q[:100])
            # Reuse chat's helper so vertical search runs CONCURRENTLY (zero
            # added latency) and we get the engine breakdown for free.
            results, search_diag, engine_breakdown, vertical_result = _web_search_one(
                q, user_question, f, vertical=v)
            formatted = format_search_for_tool_response(results, search_diag=search_diag, query=q)
            if vertical_result:
                formatted = _vertical_header_for_llm(vertical_result) + formatted
            display = _format_search_display_for_results(results)
            return (formatted, display, search_diag, engine_breakdown, vertical_result)

        ordered = run_batch_concurrent(query_specs, _search_one, max_workers=2, tag='PaperSearch', abort=abort)

        all_formatted = []
        all_display = []
        all_verticals = []
        last_diag = None
        engine_breakdown_out = None
        for idx, item in enumerate(ordered):
            q = query_list[idx]
            if item is None:
                all_formatted.append(f'Search for "{q}" failed: internal error')
                continue
            formatted, display, diag, engine_breakdown, vertical_result = item
            # Tag each result with its source query so the frontend can group
            # batch results under per-query subheaders (chat parity).
            for dr in display:
                dr['_q'] = q
            all_display.extend(display)
            if diag:
                last_diag = diag
            # engine_breakdown only renders for a single-query search (chat
            # behaviour); a batch flattens results so a per-query breakdown
            # has no place to attach.
            if single and engine_breakdown:
                engine_breakdown_out = engine_breakdown
            v_payload = _vertical_to_sse_payload(vertical_result)
            if v_payload:
                v_payload = dict(v_payload)
                v_payload['query'] = q
                all_verticals.append(v_payload)
            if not single:
                all_formatted.append(f'=== Search: {q} ===\n{formatted}')
            else:
                all_formatted.append(formatted)

        tool_content = '\n\n'.join(all_formatted)
        # Only propagate diag if we ended up with 0 display results
        final_diag = last_diag if not all_display else None
        return (tool_content, all_display, final_diag,
                engine_breakdown_out, all_verticals or None)

    elif name == 'fetch_url':
        urls = args.get('urls', [])
        # Defensive: a non-list ``urls`` (string that repair could not
        # normalize) becomes a single entry — never iterated per-character.
        if urls and not isinstance(urls, list):
            urls = [urls]
        if not urls:
            u = args.get('url', '')
            if u:
                urls = [{'url': u}]
        if not urls:
            return 'Error: no url provided', [], None, None, None

        url_list = []
        for uobj in urls[:5]:
            if isinstance(uobj, dict):
                u = uobj.get('url', '')
            elif isinstance(uobj, str):
                u = uobj
            else:
                continue
            if u and u.strip():
                url_list.append(u.strip())
        if not url_list:
            return 'Error: no valid url provided', [], None, None, None

        def _fetch_one(u):
            logger.info('[Paper:Report:Tool] fetch_url url=%.100s', u)
            # Reuse chat's helper so binary-asset staging, content filtering,
            # rejected-scheme handling and filtered-vs-raw char counts all
            # match chat exactly.
            return _fetch_url_one(u, user_question, fetch_reason='')

        ordered = run_batch_concurrent(url_list, _fetch_one, max_workers=3, tag='PaperFetch', abort=abort)

        all_parts = []
        all_display = []
        for idx, item in enumerate(ordered):
            u = url_list[idx]
            if item is None:
                # Synthesize a failure item so display/text stay aligned —
                # same shape chat's batch handler uses.
                item = {
                    'url': u, 'page_content': None, 'is_pdf': False,
                    'raw_chars': 0, 'filtered_chars': 0,
                    'error_msg': 'internal fetch error (see logs)',
                }
            all_display.append(_format_fetch_display(item, _short_url))
            page_content = item['page_content']
            filtered_chars = item['filtered_chars']
            error_msg = item.get('error_msg')
            if page_content:
                all_parts.append(
                    f"Content from {u} ({filtered_chars:,} chars):\n\n{page_content}")
            else:
                all_parts.append(
                    f"Failed to fetch {u}." + (f' ({error_msg})' if error_msg else ''))
        return '\n\n---\n\n'.join(all_parts), all_display, None, None, None

    else:
        # ── Full-set branch: every non-search tool goes through chat's SHARED
        #    dispatch — never grow parallel per-tool branches here. Engines
        #    without a shim (research-only set) keep the legacy Unknown-tool
        #    reply so a hallucinated name stops at the adapter.
        if exec_shim is None:
            return f'Unknown tool: {name}', [], None, None, None
        return _execute_shared_tool(name, args, exec_shim, round_entry, abort)



def make_research_tool_executor(messages, *, user_question, abort_signal,
                                execute_report_tool=None,
                                on_tool_event=None, log_prefix='[Paper]',
                                force_vertical=None):
    """Build the ``run_agent_loop`` ``execute_tool(rnd, tc)`` closure shared by
    the paper insight + recommend research agents.

    The two engines' per-tool-call handling was line-identical except for three
    axes — the log prefix, whether web_search is forced onto a vertical, and
    which ``_execute_report_tool`` binding is used — so all three are
    parameters here. The closure:
      1. parse+schema-repairs the tool args (``parse_and_repair_tool_args``),
      2. fires an ``on_tool_event`` ``tool_start`` (round-numbered),
      3. runs ``execute_report_tool`` (passing ``force_vertical`` through),
      4. fires ``tool_done`` with results + optional engineBreakdown/verticals,
      5. appends the ``role:'tool'`` message (30k-char capped) to ``messages``.

    ``execute_report_tool`` MUST be the caller's OWN facade-resolved binding
    (e.g. ``lib.paper.insight_engine._execute_report_tool``) so a test patching
    that engine's attribute still steers the executor exactly as the former
    inline closure did; it defaults to this module's ``_execute_report_tool``.
    ``messages`` is mutated in place (the loop appends the tool turn), matching
    the former inline closures exactly. A private per-executor round counter
    numbers the tool-events independently of the loop's round index (parity
    with the original ``_round_counter`` closure state).
    """
    _exec_report = execute_report_tool or _execute_report_tool
    _round_counter = {'n': 0}

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')
        # Parse + schema-repair once so the display label and the executor see
        # the same normalized shape (a bare-string queries/urls → array).
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)
        _round_counter['n'] += 1
        rn = _round_counter['n']
        display_query = display_query_for(fn_name, fn_args)

        if on_tool_event:
            on_tool_event({
                'type': 'tool_start', 'roundNum': rn, 'toolName': fn_name,
                'query': display_query, 'toolCallId': tc_id,
            })

        import time as _time
        tool_t0 = _time.time()
        # Only pass force_vertical when set, so the insight path's call is
        # byte-identical to its former inline closure (which passed no such
        # kwarg); the recommend path passes its 'academic' vertical.
        _extra = {'force_vertical': force_vertical} if force_vertical else {}
        result, display_results, search_diag, engine_breakdown, verticals = _exec_report(
            fn_name, fn_args_raw, user_question=user_question,
            abort=abort_signal.is_set, **_extra)
        tool_elapsed = _time.time() - tool_t0
        logger.info('%s:Tool %s → %d chars in %.1fs',
                    log_prefix, fn_name, len(result), tool_elapsed)

        if on_tool_event:
            done_ev = {
                'type': 'tool_done', 'roundNum': rn, 'toolName': fn_name,
                'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
                'results': display_results,
            }
            if engine_breakdown:
                done_ev['engineBreakdown'] = engine_breakdown
            if verticals:
                done_ev['verticals'] = verticals
            on_tool_event(done_ev)

        messages.append({
            'role': 'tool', 'tool_call_id': tc_id,
            # Research-only engines have no read_files — bound HONESTLY (an
            # explicit TRUNCATED marker) instead of the old silent 30k slice.
            'content': cap_tool_result(result, fn_name, tc_id, can_read=False),
        })

    return _execute_tool

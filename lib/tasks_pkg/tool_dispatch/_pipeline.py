# HOT_PATH
"""Tool-execution pipeline — approval → parallel dispatch → result-append.

The public entry-point is :func:`execute_tool_pipeline`, the big orchestrator
extracted from the inner loop of ``orchestrator.run_task``.  Also houses the
``_append_screenshot_message`` multimodal-result helper.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.model_info import model_supports_vision
from lib.tasks_pkg.compaction import (
    budget_tool_result,
    clamp_tool_result_text,
    enforce_round_aggregate_budget,
    mark_empty_result,
)
from lib.tasks_pkg.executor import _execute_tool_one, _finalize_tool_round
from lib.tasks_pkg.manager import _strip_base64_for_snapshot, append_event
from lib.tasks_pkg.tool_hooks import run_post_hooks, run_pre_hooks

from lib.tasks_pkg.tool_dispatch._approval import _handle_approval
from lib.tasks_pkg.tool_dispatch._flags import (
    _build_cache_hit_meta,
    _invalidate_project_cache,
    _make_cache_key,
    _safe_count_tokens,
    _task_partitions,
    _unpack_cache_entry,
)
from lib.tasks_pkg.tool_dispatch._heartbeat import (
    _SERIAL_BLOCKING_TOOLS,
    _execute_tool_one_pooled,
    _start_tool_heartbeat,
)

logger = get_logger(__name__)


def execute_tool_pipeline(
    task: dict[str, Any],
    parsed_tcs: list[tuple],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    tool_list: list[dict] | None,
    messages: list[dict[str, Any]],
    all_search_results_text: list[str],
    round_num: int,
    model: str,
) -> bool:
    """Run the full tool-execution pipeline: approval → parallel dispatch → result append.

    Returns
    -------
    bool
        True if a tool-execution timeout occurred during this round.

    Handles three phases:

    1. **Error short-circuit** — tool calls with JSON parse errors get an
       error result returned to the LLM without execution.
    2. **Serial approval** — write operations (``write_file``, ``apply_diff``)
       and server-kill commands that require user approval are executed one
       at a time, blocking until the user approves or rejects.
    3. **Parallel execution** — all remaining tool calls run concurrently
       in a :class:`~concurrent.futures.ThreadPoolExecutor`.

    After execution, tool result messages are appended to *messages* in the
    original tool-call order, and ``tool_complete`` events are emitted.

    Parameters
    ----------
    task : dict
        Live task dict — mutated (events appended, toolRounds updated).
    parsed_tcs : list[tuple]
        7-tuples from :func:`parse_tool_calls`.
    cfg : dict
        Task configuration dict (``autoApply``, etc.).
    project_path : str
        Filesystem path to the project root.
    project_enabled : bool
        Whether project-mode tools are active.
    tool_list : list | None
        Full tool definitions (passed through to ``_execute_tool_one``).
    messages : list[dict]
        Conversation message list — tool result messages appended in-place.
    all_search_results_text : list[str]
        Accumulator for search result text — appended in-place.
    round_num : int
        Current zero-based loop round (for snapshot labels and logging).
    model : str
        Current model identifier (for logging).
    """
    tid = task['id'][:8]
    # Attendance-aware auto-apply default. A task is "attended" only when a
    # human is watching a UI that can answer the write-approval prompt (set by
    # the interactive chat routes). Headless / autonomous tasks (agent/run,
    # scheduler, autopilot) are unattended: they MUST auto-apply or they would
    # block on an approval nobody can answer. When the caller omits autoApply we
    # therefore default attended→Manual (False) and unattended→auto (True).
    _attended = bool(task.get('_attended'))
    auto_apply = cfg.get('autoApply')
    if auto_apply is None:
        auto_apply = not _attended
    tool_results = {}  # tc_id → (tool_content, is_search)
    _pipeline_timed_out = False
    # Per-task write/idempotent partitions (base UNION custom env flags).
    _write_tools, _idempotent_tools = _task_partitions(task)

    # ══════════════════════════════════════════
    #  Pre-phase: Serial write-approval tools
    # ══════════════════════════════════════════
    # ── Per-task dedup cache for idempotent tools ──
    # Stored on the task dict so it's scoped to one task execution.
    if '_tool_result_cache' not in task:
        task['_tool_result_cache'] = {}
    _cache = task['_tool_result_cache']

    parallel_items = []
    for item in parsed_tcs:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _parse_err = item

        # JSON parse failure / hallucinated-tool rejection → return error to
        # LLM, skip execution.
        if _parse_err:
            if round_entry:
                _rejected = round_entry.get('_rejected')
                _err_meta = {'type': 'error', 'content': _parse_err,
                             'toolName': fn_name}
                if _rejected:
                    # Keep the distinct 'rejected' status (don't let
                    # _finalize_tool_round flip it to 'done') and carry the
                    # descriptor onto the result meta + event so the frontend
                    # styles it as a rejected hallucination, with suggestions.
                    _err_meta['rejected'] = _rejected
                    round_entry['results'] = [_err_meta]
                    round_entry['status'] = 'rejected'
                    append_event(task, build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        query=round_entry.get('query', fn_name),
                        results=[_err_meta],
                        status='rejected',
                        _rejected=_rejected,
                    ))
                else:
                    _finalize_tool_round(
                        task, rn, round_entry,
                        [_err_meta],
                        query_override=round_entry.get('query', fn_name),
                    )
            tool_results[tc_id] = (_parse_err, False)
            continue

        # ── Dedup check for idempotent tools ──
        if fn_name in _idempotent_tools:
            cache_key = _make_cache_key(fn_name, fn_args)
            cached = _cache.get(cache_key)
            if cached is not None:
                cached_content, cached_is_search, cached_source, cached_display, cached_engine_bkdn, cached_vertical = \
                    _unpack_cache_entry(cached)
                is_prefetch = cached_source == 'prefetch'
                # Compute content length for logging without materializing
                # a massive str() for screenshot dicts (which contain base64)
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    _log_len = cached_content.get('compressedSize', 0)
                    _log_suffix = ' (image)'
                else:
                    _log_len = len(str(cached_content))
                    _log_suffix = ''
                logger.info(
                    '[Task %s] conv=%s %s HIT: %s with same args at round %d — '
                    'returning %s result (%d chars%s) instead of re-executing',
                    tid, task.get('convId', ''),
                    'PREFETCH' if is_prefetch else 'DEDUP',
                    fn_name, round_num,
                    'prefetched' if is_prefetch else 'cached',
                    _log_len, _log_suffix,
                )
                # Preserve __screenshot__ dicts as-is so the post-phase
                # can detect them and convert to image_url blocks.
                # Converting to str() would dump 800K+ of base64 text
                # directly into the context, blowing up the token count.
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    dedup_content = cached_content  # keep as dict
                else:
                    dedup_content = cached_content if isinstance(cached_content, str) else str(cached_content)
                # Update round_entry to show cached/prefetched status
                if round_entry:
                    # Use stored display_results for web_search / fetch_url if available
                    # — this preserves per-result rows (titles, URLs, snippets) in the UI
                    # instead of collapsing to a single generic meta row.
                    if cached_display and fn_name in ('web_search', 'fetch_url'):
                        extra = {}
                        if cached_engine_bkdn:
                            round_entry['engineBreakdown'] = cached_engine_bkdn
                            extra['engineBreakdown'] = cached_engine_bkdn
                        if cached_vertical:
                            # Batch web_search carries multiple verticals. The
                            # streaming prefetch path wraps them as
                            # {'batch': [...]}, and the dedup path may hand us a
                            # bare list. Both must land in the plural `verticals`
                            # field — the frontend renders that as an array;
                            # `vertical` (singular) expects one {domain, items}
                            # dict and would silently drop a list/wrapper
                            # (showing the bare "vertical: auto" badge with no card).
                            if isinstance(cached_vertical, dict) and 'batch' in cached_vertical:
                                _verts = cached_vertical.get('batch') or []
                            elif isinstance(cached_vertical, list):
                                _verts = cached_vertical
                            else:
                                _verts = None
                            if _verts is not None:
                                round_entry['verticals'] = _verts
                                extra['verticals'] = _verts
                            else:
                                round_entry['vertical'] = cached_vertical
                                extra['vertical'] = cached_vertical
                        _finalize_tool_round(
                            task, rn, round_entry, cached_display,
                            query_override=round_entry.get('query', fn_name),
                            extra_event_fields=extra or None,
                        )
                    else:
                        _meta = _build_cache_hit_meta(
                            fn_name, fn_args, cached_content, is_prefetch,
                            cached_display=cached_display,
                        )
                        _finalize_tool_round(
                            task, rn, round_entry, [_meta],
                            query_override=round_entry.get('query', fn_name),
                        )
                tool_results[tc_id] = (dedup_content, cached_is_search)
                continue

        # ── Write-approval gate (Manual mode) ──
        # The approval set is DERIVED from the per-task write partition
        # (_write_tools), so every state-mutating tool — project writes,
        # run_command, memory mutators, MCP write tools, and custom write
        # tools — is approval-eligible from a single source of truth (no second
        # hand-maintained list). Gating only ever happens for an ATTENDED task
        # (a human can answer); unattended / headless tasks never block here.
        needs_approval = (
            fn_name in _write_tools
            and _attended and not auto_apply and not task['aborted']
            and not (round_entry and round_entry.get('toolName') == 'code_exec')
        )
        # run_command is in the write partition for concurrency safety, but
        # read-only invocations (grep/ls/cat/git status/…) must NOT prompt —
        # only commands that could mutate the filesystem require approval.
        if needs_approval and fn_name == 'run_command':
            from lib.project_mod.tools import _is_destructive_command
            needs_approval = _is_destructive_command(fn_args.get('command', ''))

        if needs_approval:
            approved, reject_content = _handle_approval(
                task, fn_name, fn_args, rn, round_entry, project_path, round_num, model)
            if not approved:
                tool_results[tc_id] = (reject_content, False)
                continue
            # Approved → fall through to normal dispatch. The item is in
            # _write_tools, so the serial write-tool phase below executes it via
            # _execute_tool_one and invalidates the project cache afterwards.
            #   (one execution path for project / run_command / memory / MCP /
            #    custom write tools.)

        # ── Abort check: skip remaining tools if user clicked Stop ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping tool %s (tc_id=%s) — task aborted', tid, fn_name, tc_id[:8])
            tool_results[tc_id] = ('Task aborted by user.', False)
            continue

        # ── Serial-dispatch for long-blocking tools ──
        _serial_cfg = _SERIAL_BLOCKING_TOOLS.get(fn_name)
        if _serial_cfg and _serial_cfg['match'](fn_args) and not task['aborted']:
            _reason = _serial_cfg['reason']
            logger.info('[Task %s] %s dispatched serially (%s) at round %d',
                        tid, fn_name, _reason, round_num)
            # Inject extra args (e.g. _parent_task) if configured
            _inject_fn = _serial_cfg.get('inject')
            if _inject_fn:
                fn_args.update(_inject_fn(task, rn))
            tc_id_ret, tool_content, is_search = _execute_tool_one(
                task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled,
                all_tools=tool_list,
            )
            tool_results[tc_id_ret] = (tool_content, is_search)
            logger.info('[Task %s] %s serial dispatch completed at round %d '
                        '(result_len=%d)', tid, fn_name, round_num, len(str(tool_content)))
            continue

        # ── Pre-tool hooks: validate/block/modify before execution ──
        # Inspired by Claude Code's PreToolUse hooks.
        _hook_result = run_pre_hooks(fn_name, fn_args, task)
        if _hook_result and _hook_result.action == 'block':
            logger.info('[Task %s] Pre-hook BLOCKED tool %s: %s',
                        tid, fn_name, _hook_result.message)
            _blocked_content = f'Tool blocked by pre-execution hook: {_hook_result.message}'
            # Surface the hook's recovery guidance to the model so a block is
            # an ACTIONABLE redirect (what was refused + how to proceed safely)
            # rather than a dead-end the loop can't recover from.
            _recovery = getattr(_hook_result, 'additional_context', '') or ''
            if _recovery:
                _blocked_content = f'{_blocked_content}\n\n{_recovery}'
            tool_results[tc_id] = (_blocked_content, False)
            # ★ Settle the round NOW. Without this the round stays in its
            #   'searching' start-state forever (no result, no terminal
            #   status): the live UI shows a permanent "Running…" spinner and
            #   the persisted round only gets swept to 'aborted' by the
            #   task-end dangling sweep — so an EARLY blocked tool renders as
            #   still-running even after the loop has advanced dozens of rounds
            #   past it. Emit a terminal 'rejected' result exactly like the
            #   parse-error / hallucinated-tool branch above.
            if round_entry is not None:
                _block_meta = {
                    'type': 'error',
                    'content': _blocked_content,
                    'toolName': fn_name,
                    'source': 'Blocked',
                    'snippet': _hook_result.message,
                    'badge': 'blocked',
                }
                # For run_command / code_exec, shape the meta so the frontend's
                # purpose-built "not run" terminal card renders it (⊘ blocked +
                # inline reason) — that renderer keys on meta.command / notRun.
                # A generic error meta would fall through to a plain error line.
                if fn_name in ('run_command', 'code_exec'):
                    _block_meta['command'] = fn_args.get('command') or round_entry.get('query') or ''
                    _block_meta['notRun'] = True
                    _block_meta['exitCode'] = 'not-run'
                    _block_meta['reason'] = _blocked_content
                round_entry['results'] = [_block_meta]
                round_entry['status'] = 'rejected'
                round_entry['toolContent'] = _blocked_content
                try:
                    append_event(task, build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        query=round_entry.get('query', fn_name),
                        results=[_block_meta],
                        status='rejected',
                    ))
                except Exception as _blk_ev:
                    logger.warning(
                        '[Task %s] tool_result (pre-hook block) emit failed for '
                        'tool=%s round=%s (non-fatal): %s',
                        tid, fn_name, rn, _blk_ev, exc_info=True)
            continue

        parallel_items.append(item)

    # ══════════════════════════════════════════
    #  Write-tool serial phase (concurrency safety)
    #  Inspired by Claude Code's isConcurrencySafe partitioning:
    #  write tools run serially to prevent filesystem race conditions.
    # ══════════════════════════════════════════
    _serial_write_items = [
        item for item in parallel_items if item[1] in _write_tools
    ]
    parallel_items = [
        item for item in parallel_items if item[1] not in _write_tools
    ]
    for item in _serial_write_items:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _pe = item
        if task.get('aborted'):
            logger.info('[Task %s] Skipping serial write tool %s — task aborted', tid, fn_name)
            tool_results[tc_id] = ('Task aborted by user.', False)
            continue
        logger.debug('[Task %s] Serial write dispatch: %s at round %d', tid, fn_name, round_num)
        tc_id_ret, tool_content, is_search = _execute_tool_one(
            task, tc, fn_name, tc_id, fn_args, rn, round_entry,
            cfg, project_path, project_enabled,
            all_tools=tool_list,
        )
        tool_results[tc_id_ret] = (tool_content, is_search)
        _invalidate_project_cache(_cache, trigger=fn_name)

    # ══════════════════════════════════════════
    #  Main phase: Parallel execution (read-only tools)
    # ══════════════════════════════════════════
    if parallel_items:
        # ── Abort check before spawning parallel pool ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping %d parallel tools — task aborted', tid, len(parallel_items))
            for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items:
                tool_results[tc_id] = ('Task aborted by user.', False)
            parallel_items = []  # skip the pool entirely

    if parallel_items:
        max_parallel = int(os.environ.get('TOOL_MAX_PARALLEL_WORKERS', '16'))
        max_workers = min(max_parallel, len(parallel_items))
        pool = ThreadPoolExecutor(max_workers=max_workers)
        _timed_out = False
        # ── Item 3: long-tool heartbeat ──────────────────────────────────
        # A single blocking tool (a slow web_search on dead hosts, a hung MCP
        # call, a stalled browser action) emits NO delta while it runs, so the
        # SSE stream goes silent — a buffering proxy idle-times-out, and BOTH
        # reaper liveness clocks would go stale, risking a false reap of a
        # genuinely-alive-but-slow tool. This daemon ticker fires every
        # TOOL_HEARTBEAT_INTERVAL seconds while the pool wait blocks: it (a)
        # refreshes ``_dispatch_heartbeat`` (positive-liveness clock) and (b)
        # emits a ``tool_progress`` for each still-active round — which bumps
        # ``_t_last_event`` via append_event AND keeps the stream non-silent so
        # the UI shows "Searching… (Ns)". Fast tools finish before the first
        # tick, so they never emit a heartbeat.
        _hb_stop, _hb_thread = _start_tool_heartbeat(task, parallel_items, tid)
        try:
            futures = {
                pool.submit(
                    _execute_tool_one_pooled, task,
                    tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled,
                    all_tools=tool_list,
                ): (tc_id, fn_name)
                for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items
            }
            tool_timeout = int(os.environ.get('TOOL_PARALLEL_TIMEOUT', '300'))
            try:
                for fut in as_completed(futures, timeout=tool_timeout):
                    # ── Abort check during parallel execution: cancel remaining futures ──
                    if task.get('aborted'):
                        logger.info('[Task %s] Abort detected during parallel tool execution — cancelling remaining', tid)
                        for pending_fut, (pending_id, pending_fn) in futures.items():
                            if not pending_fut.done():
                                pending_fut.cancel()
                                if pending_id not in tool_results:
                                    tool_results[pending_id] = ('Task aborted by user.', False)
                        break
                    fut_tc_id, fut_fn_name = futures[fut]
                    try:
                        ret_tc_id, tool_content, is_search = fut.result()
                        tool_results[ret_tc_id] = (tool_content, is_search)
                        # ── Populate dedup cache for idempotent tools ──
                        if fut_fn_name in _idempotent_tools:
                            # Find the matching fn_args from parallel_items
                            for _pi in parallel_items:
                                if _pi[2] == ret_tc_id:  # tc_id match
                                    _pi_cache_key = _make_cache_key(fut_fn_name, _pi[3])
                                    # For web_search / fetch_url, also cache display_results
                                    # + engineBreakdown from the round_entry for later cache hits —
                                    # this keeps the rich per-result UI even on dedup replay
                                    # (e.g. batch "3 URLs" stays as 3 rows, not 1 generic row).
                                    _pi_display = None
                                    _pi_eng_bkdn = None
                                    _pi_vert = None
                                    if fut_fn_name in ('web_search', 'fetch_url'):
                                        _pi_re = _pi[5]  # round_entry
                                        if _pi_re and _pi_re.get('results'):
                                            _pi_display = _pi_re['results']
                                        if _pi_re:
                                            _pi_eng_bkdn = _pi_re.get('engineBreakdown')
                                            _pi_vert = _pi_re.get('vertical') or _pi_re.get('verticals')
                                    elif fut_fn_name in ('read_files', 'inspect_image'):
                                        # Preserve the FULLY-MERGED inline render
                                        # descriptors (images + SVG source URIs) so a
                                        # dedup replay renders identically to the fresh
                                        # read. SVG content caches as a plain str, so
                                        # its out-of-band imageDataUris would otherwise
                                        # be lost on the second identical read.
                                        _pi_re = _pi[5]  # round_entry
                                        _pi_res = (_pi_re or {}).get('results') or []
                                        if (_pi_res and isinstance(_pi_res[0], dict)
                                                and _pi_res[0].get('imageDataUris')):
                                            _pi_display = _pi_res[0]['imageDataUris']
                                    _cache[_pi_cache_key] = (tool_content, is_search, 'dedup', _pi_display, _pi_eng_bkdn, _pi_vert)
                                    break
                        # ── Invalidate project cache after write/exec ops ──
                        elif fut_fn_name in ('write_file', 'apply_diff', 'apply_diffs',
                                             'insert_content', 'insert_contents',
                                             'create_project',
                                             'code_exec', 'bash_exec', 'run_command'):
                            _invalidate_project_cache(_cache, trigger=fut_fn_name)
                    except Exception as e:
                        # UnknownWorkspaceRootError is the LLM's fault
                        # (bad root prefix); it's already logged at WARNING
                        # at the raise site + INFO by executor.  Do not
                        # re-log as ERROR with traceback here — just record
                        # the error for the LLM and move on.
                        _is_unknown_root = False
                        try:
                            from lib.project_mod.config import UnknownWorkspaceRootError
                            _is_unknown_root = isinstance(e, UnknownWorkspaceRootError)
                        except ImportError as _imp:
                            logger.debug('[Task %s] UnknownWorkspaceRootError '
                                         'import failed: %s', tid, _imp)
                        if _is_unknown_root:
                            logger.info(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) '
                                'recoverable workspace-root error '
                                'returned to LLM at round %d: %s',
                                tid, task.get('convId', ''),
                                fut_fn_name, fut_tc_id, round_num, e)
                        else:
                            logger.error(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) execution failed at round %d model=%s',
                                tid, task.get('convId', ''), fut_fn_name, fut_tc_id, round_num, model, exc_info=True)

                        tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
            except TimeoutError:
                _timed_out = True
                _pipeline_timed_out = True
                _n_pending = sum(1 for f in futures if not f.done())
                logger.error(
                    '[Task %s] conv=%s Tool parallel execution timeout at round %d (%d tools pending) model=%s',
                    tid, task.get('convId', ''), round_num, _n_pending, model,
                    exc_info=True)

                # Harvest results from futures that completed but weren't
                # yielded by as_completed before the TimeoutError was raised.
                # Without this, completed-but-unyielded results are silently
                # lost and fall through to 'Unknown tool' in the post-phase.
                for fut, (fut_tc_id, fut_fn_name) in futures.items():
                    if fut.done():
                        if fut_tc_id not in tool_results:
                            try:
                                ret_tc_id, tool_content, is_search = fut.result()
                                tool_results[ret_tc_id] = (tool_content, is_search)
                                logger.info(
                                    '[Task %s] conv=%s Recovered completed tool %s (tc_id=%s) after timeout',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id)
                            except Exception as e:
                                logger.warning(
                                    '[Task %s] conv=%s Tool %s (tc_id=%s) completed with error after timeout: %s',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id, e)
                                tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
                    else:
                        fut.cancel()
                        tool_results[fut_tc_id] = (f'Tool execution timed out: {fut_fn_name}', False)
        finally:
            # Stop the heartbeat ticker first so it can't emit after the round
            # settles (it checks round status, but stop the loop deterministically).
            _hb_stop.set()
            # On timeout use wait=False + cancel_futures=True to avoid
            # blocking indefinitely on still-running tool threads.
            # On normal completion wait=True is fine (all futures done).
            pool.shutdown(wait=not _timed_out, cancel_futures=_timed_out)

    # ══════════════════════════════════════════
    #  Post-phase: Add tool messages in original order
    # ══════════════════════════════════════════
    _round_results_for_budget: list[tuple[str, str, str]] = []  # (tc_id, content, tool_name)
    for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parsed_tcs:
        if tc_id in tool_results:
            tool_content, is_search = tool_results[tc_id]
        else:
            # SHOULD-NOT-HAPPEN: every dispatch branch above is expected to
            # populate tool_results[tc_id]. If a tc_id reaches here unfilled,
            # a dispatch branch silently skipped writing its result — the model
            # would get a misleading "Unknown tool" with no trace of the real
            # cause. Log it so the silent-skip is diagnosable (§2 zero-silent-failure).
            logger.warning(
                '[Task %s] conv=%s tool result missing for tool=%s tc_id=%s '
                'round=%d — no dispatch branch populated it; returning '
                'Unknown-tool fallback to LLM (should not happen)',
                tid, task.get('convId', '') if task else '',
                fn_name, tc_id, round_num)
            tool_content, is_search = (f'Unknown tool: {fn_name}', False)
        if is_search:
            all_search_results_text.append(tool_content)

        # Convert screenshot dict → image_url content block for vision models
        if isinstance(tool_content, dict) and tool_content.get('__screenshot__'):
            _active_model = task.get('model', '') if task else ''
            if _active_model and not model_supports_vision(_active_model):
                # Text-only model: never build an image_url block (build_body
                # would strip it later and leave a misleading "analyze it
                # visually" text). Instead, return a truthful tool result AT
                # the tool-call site so the model knows the image is unreadable
                # and stops re-rendering / re-reading images.
                tc_content_str = (
                    '[Image not shown — the current model (%s) has no vision '
                    'support, so this image cannot be analyzed. Do not retry '
                    'reading images; rely on text, code, and test output '
                    'instead.]' % _active_model)
                messages.append({'role': 'tool', 'tool_call_id': tc_id,
                                 'content': tc_content_str})
                logger.info(
                    '[Task %s] conv=%s text-only model %s — image tool result '
                    'for tc=%s replaced with no-vision placeholder',
                    tid, task.get('convId', '') if task else '', _active_model, tc_id)
                if round_entry:
                    round_entry['toolContent'] = tc_content_str
                try:
                    append_event(task, build_event(
                        EventType.TOOL_COMPLETE,
                        roundNum=rn,
                        toolCallId=tc_id,
                        toolName=fn_name,
                        toolContent=tc_content_str,
                    ))
                except Exception as e:
                    logger.warning(
                        '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
                        tid, fn_name, round_num, e, exc_info=True)
                continue
            _append_screenshot_message(messages, tc_id, tool_content)
            # Emit tool_complete for screenshot with text fallback
            try:
                tc_content_str = tool_content.get('_text_fallback', '') or 'Image captured.'
                if round_entry:
                    round_entry['toolContent'] = tc_content_str
                append_event(task, build_event(
                    EventType.TOOL_COMPLETE,
                    roundNum=rn,
                    toolCallId=tc_id,
                    toolName=fn_name,
                    toolContent=tc_content_str,
                ))
            except Exception as e:
                logger.warning(
                    '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
                    tid, fn_name, round_num, e, exc_info=True)
        else:
            # ★ Post-tool hooks: modify/enrich result after execution.
            # Inspired by Claude Code's PostToolUse hooks.
            if isinstance(tool_content, str):
                tool_content = run_post_hooks(fn_name, fn_args, tool_content, task)

            # ★ Empty result marker: prevent models from misinterpreting
            # empty tool results as conversation end.
            if isinstance(tool_content, str):
                tool_content = mark_empty_result(fn_name, tool_content)

            # ★ Layer 0: Budget tool results before they enter context.
            # Persists oversized results to disk (inspired by Claude Code's
            # per-tool maxResultSizeChars + persistence).  Exempt tools
            # (read_files) pass through unchanged.
            # Layer 1 (micro_compact) will further compress these once
            # they fall outside the hot tail.
            _l0_pre_chars = len(tool_content) if isinstance(tool_content, str) else 0
            if isinstance(tool_content, str):
                _conv_id = task.get('convId', '') if task else ''
                tool_content = budget_tool_result(fn_name, tool_content,
                                                  tool_use_id=tc_id,
                                                  conv_id=_conv_id)
            # If budget_tool_result shrank the content (persisted to disk
            # or fell back to head+tail truncation), stamp the round so
            # the frontend can flag this tool call as L0-compacted. Any
            # length reduction is a signal — budget_tool_result only
            # mutates content when it exceeds the per-tool budget.
            if (round_entry
                    and isinstance(tool_content, str)
                    and _l0_pre_chars > len(tool_content)):
                round_entry['compactionLayer'] = 'L0'
                round_entry['compactedFromChars'] = _l0_pre_chars
                round_entry['compactedToChars'] = len(tool_content)

            # ★ Layer 2: tool-agnostic hard ceiling — the LAST line of
            # defence. Unlike Layer 0 (budget_tool_result) this has NO
            # per-tool exemption, so it ALSO clamps read_files (which Layer 0
            # skips). Makes the "opaque blob floods context" bug class
            # unrepresentable: a relative-path PNG decoded as text, a str()'d
            # image dict, or any future leak gets clamped to a survivable
            # result instead of a fatal HTTP 400. See conv mqgfkmxy (2026-06).
            if isinstance(tool_content, str):
                _conv_id_hc = task.get('convId', '') if task else ''
                tool_content = clamp_tool_result_text(
                    fn_name, tool_content, tc_id=tc_id, conv_id=_conv_id_hc)

            # ★ Sync the budgeted/offloaded form back into the dedup cache.
            # The cache entry was populated with the PRE-budget content (the
            # parallel-phase writer at ~L1250 / the streaming prefetch
            # injector), while budgeting above only rewrote the local
            # message copy. Left unsynced, the full result (e.g. a 680 KB
            # web_search dump) lingers in ``_tool_result_cache`` — it
            # serializes into the persisted ``raw_state`` (state balloon) AND
            # is replayed verbatim on a later dedup hit, re-flooding context
            # with content the offloader had already spilled to disk. Rewrite
            # content[0] to the budgeted string, preserving the rest of the
            # entry (is_search / source / display / engine_breakdown /
            # vertical) so the rich UI-replay path is unchanged.
            if (isinstance(tool_content, str)
                    and fn_name in _idempotent_tools):
                _sync_key = _make_cache_key(fn_name, fn_args)
                _cached_entry = _cache.get(_sync_key)
                if (_cached_entry is not None
                        and isinstance(_cached_entry, (tuple, list))
                        and len(_cached_entry) >= 1
                        and isinstance(_cached_entry[0], str)
                        and len(_cached_entry[0]) > len(tool_content)):
                    _cache[_sync_key] = (tool_content, *tuple(_cached_entry)[1:])

            # Collect for aggregate budget check
            _round_results_for_budget.append((tc_id, tool_content, fn_name))

            messages.append({'role': 'tool', 'tool_call_id': tc_id, 'content': tool_content})

            # ★ Emit tool_complete AFTER budgeting so that toolContent
            #   reflects the ACTUAL content given to the model (budgeted/
            #   persisted form).  Preview must show what the model sees.
            try:
                if isinstance(tool_content, str):
                    tc_content_str = tool_content
                else:
                    tc_content_str = json.dumps(tool_content, ensure_ascii=False)
                if len(tc_content_str) > 50000:
                    tc_content_str = tc_content_str[:50000] + '\n... [truncated for continue context]'

                # ★ Persist toolContent on round_entry so checkpoint writes
                #   it to DB.  Without this, crash-recovery loses tool
                #   context and Continue rolls back ALL tool rounds
                #   (toolContent == null → incomplete).
                if round_entry:
                    round_entry['toolContent'] = tc_content_str

                # ★ Per-tool token count: gives the frontend an accurate
                # measure of the cost the model actually pays for this
                # result. Falls back to 0 on backend failure; the
                # frontend then renders chars instead.
                _tc_tokens = _safe_count_tokens(tc_content_str,
                                                model=task.get('model', '') if task else '')
                if round_entry and _tc_tokens > 0:
                    round_entry['toolTokens'] = _tc_tokens

                _evt = build_event(
                    EventType.TOOL_COMPLETE,
                    roundNum=rn,
                    toolCallId=tc_id,
                    toolName=fn_name,
                    toolContent=tc_content_str,
                )
                if _tc_tokens > 0:
                    _evt['toolTokens'] = _tc_tokens
                if round_entry and round_entry.get('compactionLayer'):
                    _evt['compactionLayer'] = round_entry['compactionLayer']
                    _evt['compactedFromChars'] = round_entry.get('compactedFromChars')
                    _evt['compactedToChars'] = round_entry.get('compactedToChars')
                append_event(task, _evt)
            except Exception as e:
                logger.warning(
                    '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
                    tid, fn_name, round_num, e, exc_info=True)

    # ══════════════════════════════════════════
    #  Per-round aggregate budget check
    # ══════════════════════════════════════════
    # If total tool result chars in this round exceed MAX_ROUND_TOOL_RESULTS_CHARS,
    # persist the largest non-exempt results to disk.
    # This prevents context explosion from parallel tool calls (e.g. 10 grep_search
    # calls each returning 40K chars = 400K total).
    if _round_results_for_budget:
        _agg_dict = {
            tc_id: (content, tool_name, tc_id)
            for tc_id, content, tool_name in _round_results_for_budget
            if isinstance(content, str)
        }
        _conv_id = task.get('convId', '') if task else ''
        _pre_chars_by_tc = {tc_id: len(c) for tc_id, c, _ in _round_results_for_budget
                            if isinstance(c, str)}
        _updated = enforce_round_aggregate_budget(_agg_dict, conv_id=_conv_id)
        # Apply any changes back to messages AND round_entries/toolContent
        # so Preview stays in sync with actual model content.
        for msg in messages:
            if msg.get('role') == 'tool':
                _tc_id = msg.get('tool_call_id', '')
                if _tc_id in _updated:
                    new_content, _, _ = _updated[_tc_id]
                    if new_content != msg.get('content'):
                        msg['content'] = new_content
                        # Update toolContent on the corresponding round_entry
                        for _ptc in parsed_tcs:
                            if _ptc[2] == _tc_id:  # tc_id match
                                _re = _ptc[5]  # round_entry
                                _re_rn = _ptc[4]
                                _re_fn = _ptc[1]
                                if _re:
                                    _tc_str = new_content if isinstance(new_content, str) else str(new_content)
                                    if len(_tc_str) > 50000:
                                        _tc_str = _tc_str[:50000] + '\n... [truncated for continue context]'
                                    _re['toolContent'] = _tc_str
                                    # Stamp aggregate-budget compaction
                                    _pre = _pre_chars_by_tc.get(_tc_id, 0)
                                    _post = len(_tc_str)
                                    if _pre > _post:
                                        _re['compactionLayer'] = 'L0'
                                        _re['compactedFromChars'] = _pre
                                        _re['compactedToChars'] = _post
                                        _re['toolTokens'] = _safe_count_tokens(
                                            _tc_str, model=task.get('model', '') if task else '')
                                        try:
                                            append_event(task, build_event(
                                                EventType.TOOL_COMPACTED,
                                                roundNum=_re_rn,
                                                toolCallId=_tc_id,
                                                toolName=_re_fn,
                                                compactionLayer='L0',
                                                compactedFromChars=_pre,
                                                compactedToChars=_post,
                                                toolTokens=_re.get('toolTokens', 0),
                                                compactedContent=_tc_str,
                                            ))
                                            # Diagnostic — see matching log in
                                            # compaction.py:_stamp_l1 for the rationale.
                                            logger.info(
                                                '[L0] tool_compacted emitted: tc_id=%s '
                                                'tool=%s round=%s %dch→%dch (-%.0f%%)',
                                                _tc_id[:12] if _tc_id else '?',
                                                _re_fn or '?', _re_rn,
                                                _pre, _post,
                                                (1 - _post / _pre) * 100 if _pre else 0,
                                            )
                                        except Exception as _ev_err:
                                            logger.warning(
                                                '[L0] tool_compacted SSE emit failed: '
                                                'tc_id=%s tool=%s round=%s err=%s',
                                                _tc_id[:12] if _tc_id else '?',
                                                _re_fn or '?', _re_rn, _ev_err)
                                break

    # Emit snapshot AFTER tool results appended — WIRE-FORM view (same single
    # source of truth as the orchestrator's pre-LLM and final snapshots), so
    # the panel reflects exactly what the model will receive next round. Runs
    # on an independent copy via apply_wire_sanitize (does not mutate messages).
    try:
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        _wire = apply_wire_sanitize(
            messages, conv_id=task.get('convId', ''),
            provider_id=task.get('provider_id') or '')
        snapshot = _strip_base64_for_snapshot(_wire)
        snap_evt = build_event(
            EventType.MESSAGES_SNAPSHOT,
            roundNum=round_num + 1,
            label=f'Round {round_num + 1} 工具结果后 · {len(snapshot)}条',
            messages=snapshot,
        )
        if tool_list:
            snap_evt['tools'] = tool_list
        append_event(task, snap_evt)
    except Exception:
        logger.warning(
            '[Task %s] messages_snapshot post-tool failed at round %d model=%s',
            tid, round_num + 1, model, exc_info=True)

    return _pipeline_timed_out


def _append_screenshot_message(messages, tc_id, tool_content):
    """Convert a screenshot dict into a multimodal tool message and append it.

    Parameters
    ----------
    messages : list[dict]
        Conversation messages — appended in-place.
    tc_id : str
        The tool_call_id to associate with the result message.
    tool_content : dict
        Screenshot dict with keys ``dataUrl``, ``format``, ``originalSize``,
        ``compressedSize``, ``compressionApplied``.
    """
    # A multi-image batch (read_files of several images) carries every image
    # in ``images``; otherwise treat the dict itself as the single image.
    img_dicts = tool_content.get('images') or [tool_content]

    def _data_url_parts(img):
        du = img.get('dataUrl', '')
        if du.startswith('data:'):
            header, b64 = du.split(',', 1)
            return header.split(':')[1].split(';')[0], b64
        return f'image/{img.get("format", "png")}', du

    content_blocks = []
    for img in img_dicts:
        media_type, b64_data = _data_url_parts(img)
        content_blocks.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{media_type};base64,{b64_data}'},
        })

    # Use custom text description if provided (e.g. image gen results),
    # otherwise fall back to the generic screenshot description.
    text_desc = tool_content.get('_text_fallback')
    if not text_desc:
        fmt = tool_content.get('format', 'png')
        orig_size = tool_content.get('originalSize', 0)
        comp_size = tool_content.get('compressedSize', 0)
        size_info = f'{comp_size:,} bytes'
        if tool_content.get('compressionApplied') and orig_size:
            size_info = f'{orig_size:,} → {comp_size:,} bytes (compressed)'
        text_desc = (
            f'📸 Screenshot captured ({fmt}, {size_info}). '
            f'The image above shows the current visible area of the page. '
            f'Analyze it visually.'
        )
    if len(img_dicts) > 1:
        text_desc = f'{len(img_dicts)} images loaded above.\n{text_desc}'

    content_blocks.append({'type': 'text', 'text': text_desc})

    messages.append({
        'role': 'tool',
        'tool_call_id': tc_id,
        'content': content_blocks,
    })

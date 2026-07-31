# HOT_PATH
"""Single tool-call execution entry point — unified dispatch for all tool types.

Dispatch is handled by the :data:`tool_registry` singleton (a
:class:`ToolRegistry`) supporting exact-name, special, and set-based lookup.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.executor._registry import tool_registry

logger = get_logger(__name__)


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

    # ★ Start-clock backfill (pt_67ffc2b7). Chat's rounds are stamped with
    #   `tStart` by _build_tool_round_entry, but SECONDARY surfaces (paper
    #   report / Q&A, swarm sub-agents, the timer poller) hand-build their round
    #   dicts and never went through that constructor. Backfilling at THIS seam
    #   — the one entry point every tool execution passes through — means the
    #   duration a user sees is measured for every surface instead of silently
    #   reading 0ms on the ones that built their own dict. Only fills a MISSING
    #   value, so a real dispatch-time clock is never overwritten with a later one.
    if round_entry is not None and round_entry.get('tStart') is None:
        from lib.agent_core.events import now_ms
        round_entry['tStart'] = now_ms()

    # ★ Per-client browser routing: propagate client_id to worker threads
    #   (ThreadPoolExecutor threads don't inherit the parent's thread-locals)
    _browser_cid = cfg.get('browserClientId')
    if _browser_cid:
        from lib.browser import _set_active_client
        _set_active_client(_browser_cid)

    # ★ Per-request custom tools resolve task-locally, BEFORE the global
    #   registry — a request's tools never persist into tool_registry and
    #   never leak into another task. See lib/tools/tool_env.py.
    handler = None
    _tool_env = task.get('_tool_env')
    if _tool_env is not None:
        try:
            handler = _tool_env.resolve(fn_name)
        except Exception as e:
            logger.warning('[Executor] tool_env.resolve failed for %s: %s',
                           fn_name, e, exc_info=True)
    if handler is None:
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
            # ValueError from a tool handler is the LLM's fault (bad/unknown
            # root prefix, wrong arg type, etc.) — it is cleanly converted
            # into a tool-result and the LLM retries.  Do NOT flood error.log
            # with tracebacks for this recoverable path; other exception
            # types still get full ERROR + traceback so real bugs surface.
            #
            # Special case — UnknownWorkspaceRootError: already logged ONCE
            # as WARNING at the raise site (lib/project_mod/tools.py). Do
            # NOT re-log as WARNING here (would quadruple-log the same
            # event across executor + streaming_tool_executor + tool_dispatch
            # + the raise site — ~492 lines/day in error.log per §1 of the
            # log-noise audit). Log at INFO so the LLM-facing recovery is
            # still visible in app.log without bloating error.log.
            from lib.project_mod.config import UnknownWorkspaceRootError
            if isinstance(e, UnknownWorkspaceRootError):
                logger.info(
                    '[Tool:%s] recoverable workspace-root error returned '
                    'to LLM: %s (tc_id=%s)',
                    fn_name, e, tc_id[:8],
                )
            elif isinstance(e, ValueError):
                logger.warning(
                    '[Tool:%s] recoverable ValueError (returned to LLM): %s '
                    '(tc_id=%s args=%.300s)',
                    fn_name, e, tc_id[:8], _arg_preview,
                )
            else:
                logger.error(
                    '[Executor] Tool handler %s raised %s (tc_id=%s args=%.300s) — '
                    'returning error to LLM so it can retry',
                    fn_name, type(e).__name__, tc_id[:8], _arg_preview,
                    exc_info=True,
                )
                # ── Feed the self-diagnosis loop ──
                # This is a GENUINE tool bug (not a recoverable LLM-fault
                # ValueError / UnknownWorkspaceRootError handled above). Emit a
                # structured, fingerprinted audit event so the nightly
                # optimizer can CLUSTER recurring failures by signature and
                # surface the ones that keep recurring — instead of relying on
                # brittle '[Tool:X] failed' regex scraping of app.log with no
                # dedup. req_id() (seeded to the task id in run_task) ties the
                # event back to its task automatically.
                try:
                    from lib.error_fingerprint import fingerprint
                    from lib.log import audit_log
                    audit_log('tool_error', tool=fn_name,
                              exc_type=type(e).__name__,
                              fingerprint=fingerprint(str(e), exc_type=type(e).__name__),
                              detail=str(e)[:200])
                except Exception as _ae:
                    logger.debug('[Executor] tool_error audit emit failed for %s: %s',
                                 fn_name, _ae)
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

# HOT_PATH
"""Single tool-call execution entry point — unified dispatch for all tool types.

Dispatch flows through the per-task resolver seam
(:func:`lib.tasks_pkg.executor._resolver.resolver_for`): custom tool-env,
the registry fallback, and the owner-scoped MCP bridge fallback.
"""

from __future__ import annotations

import json
from typing import Any

from lib.agent_core.execution_clock import task_execution_time_ms
from lib.log import get_logger
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context

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
    """Execute one tool call and update its presentation round."""
    if task.get('aborted'):
        logger.info('[Executor] Skipping tool %s (tc_id=%s) — task aborted',
                    fn_name, tc_id[:8])
        return tc_id, 'Task aborted by user.', False

    from lib.agent_core.execution_limits import admit_tool_execution
    limit_reason = admit_tool_execution(task)
    if limit_reason:
        return tc_id, limit_reason, False

    budget=task.get('_executionBudget')
    if budget is None and callable(task.get('_executionToolObserver')):
        from lib.agent_core.execution_budget import model_budget_admission
        model_budget_admission(task)
        budget=task.get('_executionBudget')
    event=budget.begin_tool_attempt(task,tc_id,fn_name,rn) if budget is not None else None
    try:
        from lib.tools.execution_timing import tool_execution_timing
        with tool_execution_timing(fn_name, task_id=task.get('id'),
                conversation_id=task.get('convId'), tool_call_id=tc_id, state=round_entry):
            result=_dispatch_admitted_tool_one(task,tc,fn_name,tc_id,fn_args,rn,round_entry,
                                              cfg,project_path,project_enabled,all_tools)
    except Exception:
        if budget is not None:budget.finish_tool_attempt(event,phase='executor_exception')
        raise
    else:
        digest=None
        if event is not None:
            import hashlib
            try:
                value=result[1];hasher=hashlib.sha256()
                chunks=([value] if isinstance(value,str) else
                    json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).iterencode(value))
                for chunk in chunks:
                    for offset in range(0,len(chunk),128*1024):
                        hasher.update(chunk[offset:offset+128*1024].encode('utf-8'))
                digest=hasher.hexdigest()
            except (TypeError,ValueError,IndexError):pass
        if budget is not None:budget.finish_tool_attempt(event,phase='executor_returned',result_sha256=digest)
        return result


def _dispatch_admitted_tool_one(
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
    if round_entry is not None and round_entry.get('tStart') is None:
        from lib.agent_core.events import now_ms as now_ms
        round_entry['tStart'] = task_execution_time_ms(task)

    handler = None
    tool_env = task.get('_tool_env')
    if tool_env is not None:
        try:
            handler = tool_env.resolve(fn_name)
        except Exception as exc:
            logger.warning('[Executor] tool_env.resolve failed for %s: %s',
                           fn_name, exc, exc_info=True)
    if handler is None:
        # Dispatch through the resolver seam (D2/D3 isolation contract):
        # registry fallback plus the owner-scoped MCP bridge fallback, never
        # a monkey-patched global lookup.
        from lib.tasks_pkg.executor._resolver import resolver_for
        handler = resolver_for(task).lookup(fn_name, round_entry)

    context = context_for_task(
        task,
        round_num=rn,
        tool_call_id=tc_id,
        tool_name=fn_name,
        round_entry=round_entry,
    )
    round_entry['_runtimeState'] = 'running'
    if handler is None:
        context.settle_once('error')
        round_entry['_runtimeState'] = 'error'
        unregister_context(context)
        logger.warning('[Executor] Unknown tool requested: %s', fn_name)
        return (
            tc_id,
            f'Error: unknown tool "{fn_name}". This tool is not registered. '
            'Verify the tool name against the available tool list and retry.',
            False,
        )

    try:
        try:
            result = handler(
                task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled, all_tools,
            )
        except Exception as exc:
            context.settle_once(
                'cancelled' if context.cancellation_requested else 'error')
            round_entry['_runtimeState'] = context.terminal_state
            return _handler_error_result(
                task, fn_name, tc_id, fn_args, rn, round_entry, exc)

        state = 'cancelled' if context.cancellation_requested else 'done'
        context.settle_once(state)
        round_entry['_runtimeState'] = state
        return result
    finally:
        unregister_context(context)


def _handler_error_result(task, fn_name, tc_id, fn_args, rn, round_entry, exc):
    """Convert one uncaught handler exception into a model-visible result."""
    try:
        arg_preview = json.dumps(fn_args, ensure_ascii=False)[:300]
    except Exception as dump_exc:
        logger.debug('[Executor] fn_args dump failed for %s: %s',
                     fn_name, dump_exc)
        arg_preview = repr(fn_args)[:300]

    from lib.project_mod.config import UnknownWorkspaceRootError
    if isinstance(exc, UnknownWorkspaceRootError):
        logger.info(
            '[Tool:%s] recoverable workspace-root error returned to LLM: %s '
            '(tc_id=%s)', fn_name, exc, tc_id[:8])
    elif isinstance(exc, ValueError):
        logger.warning(
            '[Tool:%s] recoverable ValueError (returned to LLM): %s '
            '(tc_id=%s args=%.300s)', fn_name, exc, tc_id[:8], arg_preview)
    else:
        logger.error(
            '[Executor] Tool handler %s raised %s (tc_id=%s args=%.300s) — '
            'returning error to LLM so it can retry',
            fn_name, type(exc).__name__, tc_id[:8], arg_preview, exc_info=True)
        try:
            from lib.error_fingerprint import fingerprint
            from lib.log import audit_log
            audit_log(
                'tool_error', tool=fn_name, exc_type=type(exc).__name__,
                fingerprint=fingerprint(str(exc), exc_type=type(exc).__name__),
                detail=str(exc)[:200],
            )
        except Exception as audit_exc:
            logger.debug('[Executor] tool_error audit emit failed for %s: %s',
                         fn_name, audit_exc)

    err_msg = (
        f'Error: tool "{fn_name}" execution failed with '
        f'{type(exc).__name__}: {exc}. '
        f'Arguments received: {arg_preview}'
    )
    if round_entry is not None and round_entry.get('status') != 'done':
        try:
            _finalize_tool_round(
                task, rn, round_entry,
                [{'type': 'error', 'content': err_msg, 'toolName': fn_name}],
                query_override=round_entry.get('query', fn_name),
                status='error',
            )
        except Exception as finalize_exc:
            logger.debug('[Executor] _finalize_tool_round on error path failed '
                         'for %s: %s', fn_name, finalize_exc)
    return tc_id, err_msg, False

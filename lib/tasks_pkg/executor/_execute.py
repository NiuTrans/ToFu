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
from lib.tasks_pkg.executor._effects import (
    begin_tool_effect, finish_tool_effect, uncertain_effect_error,
)

logger = get_logger(__name__)


class _InvalidToolHandlerResult(RuntimeError):
    """The handler did not acknowledge this dispatched occurrence."""


def _validate_handler_result(result, call_id):
    """Check correlation before effect ACK, cache admission or sibling delivery."""
    if not isinstance(result, (tuple, list)) or len(result) != 3:
        raise _InvalidToolHandlerResult(
            'Tool handler must return (call_id, content, is_search).')
    if not isinstance(result[0], str) or result[0] != call_id:
        raise _InvalidToolHandlerResult(
            'Tool handler returned a call ID that does not match this occurrence; '
            'the unverified payload was withheld.')
    if type(result[2]) is not bool:
        raise _InvalidToolHandlerResult(
            'Tool handler returned a non-boolean is_search flag; '
            'the unverified payload was withheld.')


def _audit_direct_executor_outcome(task, round_entry, *, status=None):
    """Direct consumers settle here; pipelines own their later deadline verdict."""
    if not isinstance(round_entry, dict) or round_entry.get('_outcomeAuditOwner') == 'pipeline':
        return
    if status is None:
        runtime_state = round_entry.get('_runtimeState')
        status = round_entry.get('status')
        if runtime_state == 'cancelled':
            status = 'aborted'
        elif status in ('error', 'aborted', 'rejected', 'unanswerable'):
            pass
        elif runtime_state == 'error':
            status = 'error'
        else:
            if status in (None, 'searching', 'executing'):
                status = 'done'
    from lib.tasks_pkg.tool_call_outcome import audit_round_outcome
    audit_round_outcome(task, round_entry, status=status)


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

    from lib.tasks_pkg.coordinator_policy import coordinator_call_rejection
    coordinator_denial = coordinator_call_rejection(task, fn_name, fn_args, project_path)
    if coordinator_denial:
        return tc_id, coordinator_denial, False
    from lib.project_dispatch.admission import is_dispatch_task
    if is_dispatch_task(task):
        from lib.project_dispatch.scope import admit_tool
        admit_tool(task, fn_name, fn_args, project_path=project_path)
        if task.get('_tool_env') is not None:
            from lib.llm.llm_errors import AbortedError
            raise AbortedError('Project file authority cannot be shadowed by a custom tool environment')
    if task.get('aborted'):
        logger.info('[Executor] Skipping tool %s (tc_id=%s) — task aborted',
                    fn_name, tc_id[:8])
        return tc_id, 'Task aborted by user.', False

    from lib.agent_core.execution_limits import admit_tool_execution
    limit_reason = admit_tool_execution(task)
    if limit_reason:
        return tc_id, limit_reason, False

    from lib.tasks_pkg.executor._file_recovery import check_file_recovery, recovery_receipt
    recovery = check_file_recovery(task, fn_name, fn_args, round_entry or {}, project_path)
    refusal = recovery.refusal
    if recovery.unknown and not refusal and not recovery_receipt(
            task, fn_name, tc_id, fn_args, recovery, consume=True):
        from lib.tools.result_envelope import typed_tool_error
        refusal = typed_tool_error('tool_effect_takeover_required', retryable=False,
            message='This new write has no exact human decision covering the unresolved earlier operation.',
            next_action='Use the ordinary attended approval path; do not repeat this write automatically.').to_model_text()
    if refusal:
        from lib.tools.registry import build_tool_result_meta
        from lib.tool_results.tool_rejection import stamp_tool_rejection
        stamp_tool_rejection(round_entry, {'kind': 'effect_recovery_required', 'tool': fn_name},
                             reason=refusal, retryable=False)
        _finalize_tool_round(task, rn, round_entry,
            [build_tool_result_meta(fn_name, fn_args, refusal)], status='rejected')
        _audit_direct_executor_outcome(task, round_entry)
        return tc_id, refusal, False

    budget=task.get('_executionBudget')
    if budget is None and callable(task.get('_executionToolObserver')):
        from lib.agent_core.execution_budget import model_budget_admission
        model_budget_admission(task)
        budget=task.get('_executionBudget')
    event=budget.begin_tool_attempt(task,tc_id,fn_name,rn) if budget is not None else None
    from lib.tasks_pkg.working_tool_sources import begin_tool_source
    begin_tool_source(task, fn_name, tc_id)
    outcome_status = None
    try:
        from lib.tools.execution_timing import tool_execution_timing
        with tool_execution_timing(fn_name, task_id=task.get('id'),
                conversation_id=task.get('convId'), tool_call_id=tc_id, state=round_entry):
            result=_dispatch_admitted_tool_one(task,tc,fn_name,tc_id,fn_args,rn,round_entry,
                                              cfg,project_path,project_enabled,all_tools)
    except Exception:
        outcome_status = 'error'
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
    finally:
        _audit_direct_executor_outcome(task, round_entry, status=outcome_status)


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
    from lib.project_dispatch.admission import is_file_dispatch_task
    dispatch_file = is_file_dispatch_task(task)
    if dispatch_file:
        from lib.tasks_pkg.handlers.project import _handle_project_tool
        from lib.project_dispatch.scope import assert_firstparty_file_handler
        handler = _handle_project_tool
        assert_firstparty_file_handler(fn_name, handler)
    tool_env = task.get('_tool_env')
    if tool_env is not None and not dispatch_file:
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
            from contextlib import nullcontext
            from lib.project_mod.tool_admission import tool_admission_scope
            from lib.project_dispatch.scope import tool_scope
            with tool_admission_scope(task, context, tool_name=fn_name) if dispatch_file else nullcontext():
                with tool_scope(task, fn_name, fn_args, project_path) if dispatch_file else nullcontext():
                    begin_tool_effect(task, fn_name, tc_id, rn, round_entry)
                    if task.get('aborted') or context.cancellation_requested:
                        from lib.llm.llm_errors import AbortedError
                        round_entry['executionEffect'] = {
                            **round_entry['executionEffect'], 'state': 'not_started'}
                        raise AbortedError('Task cancelled before tool handler entry')
                    result = handler(
                        task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                        cfg, project_path, project_enabled, all_tools,
                    )
                    _validate_handler_result(result, tc_id)
        except Exception as exc:
            finish_tool_effect(round_entry)
            context.settle_once(
                'cancelled' if context.cancellation_requested else 'error')
            round_entry['_runtimeState'] = context.terminal_state
            return _handler_error_result(
                task, fn_name, tc_id, fn_args, rn, round_entry, exc)

        except BaseException:
            finish_tool_effect(round_entry)
            context.settle_once('cancelled' if context.cancellation_requested else 'error')
            round_entry['_runtimeState'] = context.terminal_state
            raise

        finish_tool_effect(round_entry, returned=True)
        from lib.tools.result_value import tool_result_failure_verdict
        verdict = tool_result_failure_verdict(result[1])
        if verdict and round_entry.get('status') not in {
                'error', 'aborted', 'rejected', 'unanswerable'}:
            round_entry['status'] = verdict
            round_entry['_cacheableResult'] = False
            try:
                _finalize_tool_round(
                    task, rn, round_entry,
                    round_entry.get('results') or [
                        {'type': 'error', 'content': result[1], 'toolName': fn_name}],
                    status=verdict)
            except Exception as exc:
                logger.debug('[Executor] Returned failure presentation failed '
                             'for %s: %s', fn_name, exc)
        state = ('cancelled' if context.cancellation_requested or verdict == 'aborted'
                 else 'error' if verdict else 'done')
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
            'returning error with effect recovery policy',
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
    content = uncertain_effect_error(round_entry, err_msg)
    if isinstance(exc, _InvalidToolHandlerResult) and content == err_msg:
        from lib.tools.result_envelope import typed_tool_error
        content = typed_tool_error(
            'tool_result_invalid', retryable=False,
            next_action=(
                'Use another available tool to verify the outcome. Repair the '
                'handler receipt before retrying this tool; independent '
                'authorized work can continue.'),
            message=err_msg,
        ).to_envelope_text()
    if round_entry is not None:
        # Handler presentation can precede receipt decoding or cleanup. A
        # subsequent exception still owns the final execution verdict.
        round_entry['_cacheableResult'] = False
        try:
            _finalize_tool_round(
                task, rn, round_entry,
                [{'type': 'error', 'content': content, 'toolName': fn_name}],
                query_override=round_entry.get('query', fn_name),
                status='error',
            )
        except Exception as finalize_exc:
            logger.debug('[Executor] _finalize_tool_round on error path failed '
                         'for %s: %s', fn_name, finalize_exc)
    return tc_id, content, False

"""Stable tool-dispatch service contract.

This module is the only cross-domain entry point for parsing model tool calls
and executing them through approval, scheduling, receipt, and result handling.
"""

def publish_prepared_response_siblings(task, parsed_calls, round_num):
    """Publish one response's direct occurrences for qualified gateway reuse."""
    from lib.tools.gateway import EXECUTE_TOOLS_NAME, EXECUTE_PROGRAM_NAME, SEARCH_TOOLS_NAME
    from lib.tasks_pkg.tool_dispatch._flags import _publish_execute_gateway_direct_siblings
    gateways={EXECUTE_TOOLS_NAME,EXECUTE_PROGRAM_NAME,SEARCH_TOOLS_NAME}
    direct=[(name,call_id,arguments) for _,name,call_id,arguments,_,_,error in parsed_calls
            if name not in gateways and not error]
    _publish_execute_gateway_direct_siblings(task,round_num,direct)

from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls, publish_tool_call_entry
from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline


def authorize_prepared_tool_call(task, parsed_call, *, configuration, project_path, round_num, model):
    """Run the canonical decision gate without executing a prepared occurrence.

    The caller retains this exact parsed occurrence for subsequent execution.
    Automatic policy exceptions mint the same automatic receipt as Auto mode;
    explicit human, delegate and Git decisions retain their own authority.
    """
    from lib.tasks_pkg.tool_dispatch._flags import (
        _task_partitions, _task_confirmation_tools, _ensure_tool_result_cache,
    )
    from lib.tasks_pkg.tool_dispatch._pipeline import _approval_stops_tool_call
    from lib.tasks_pkg.tool_dispatch._approval import approval_receipt_authority
    _, name, call_id, arguments, number, row, parse_error = parsed_call

    from lib.tasks_pkg.coordinator_policy import coordinator_call_rejection
    if coordinator_call_rejection(task, name, arguments, project_path):
        return 'denied'
    write, idempotent = _task_partitions(task)
    confirmation = _task_confirmation_tools(task)
    if parse_error or name not in write | confirmation:
        raise ValueError('Prepared approval requires a declared write occurrence')
    auto = configuration.get('autoApply')
    if auto is not None and type(auto) is not bool:
        raise ValueError('Prepared approval requires a boolean Auto mode')
    stopped = _approval_stops_tool_call(
        task=task, fn_name=name, fn_args=arguments, tc_id=call_id, rn=number,
        round_entry=row, confirmation_tools=confirmation, write_tools=write,
        attended=bool(task.get('_attended')), auto_apply=True if auto is None else auto,
        cfg=configuration, project_path=project_path, round_num=round_num,
        model=model, tool_results={}, idempotent_tools=idempotent,
        cache=_ensure_tool_result_cache(task), tid=task['id'][:8],
        receipt_required=True,
    )
    if task.get('aborted') or task['abort_event'].is_set():
        withdraw_prepared_tool_call(task, parsed_call)
        return 'denied'
    if stopped:
        return 'denied'
    authority = approval_receipt_authority(task, name, call_id, arguments)
    if authority is None:
        raise ValueError('Prepared approval has no exact application receipt')
    return authority


def withdraw_prepared_tool_call(task, parsed_call, *, status='aborted'):
    """Settle an owned occurrence after its decision worker has quiesced."""
    if status not in {'aborted','error'}:
        raise ValueError('Prepared withdrawal requires abort or failure')
    _, name, call_id, arguments, number, row, _ = parsed_call
    receipts = task.get('_tool_approval_receipts') or {}
    receipts.pop(call_id, None)
    if row.get('status') in {'done', 'error', 'aborted', 'rejected', 'skipped', 'unknown'}:
        return
    from lib.tasks_pkg.executor import _finalize_tool_round
    from lib.tools.registry import build_tool_result_meta
    meta = build_tool_result_meta(name, arguments, 'Tool approval did not reach execution.')
    meta.update(badge=status, writeOk=False)
    _finalize_tool_round(task, number, row, [meta], status=status)


__all__ = ['execute_tool_pipeline', 'parse_tool_calls', 'authorize_prepared_tool_call', 'withdraw_prepared_tool_call', 'publish_prepared_response_siblings', 'publish_tool_call_entry']

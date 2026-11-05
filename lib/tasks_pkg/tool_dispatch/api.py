"""Stable tool-dispatch service contract.

This module is the only cross-domain entry point for parsing model tool calls
and executing them through approval, scheduling, receipt, and result handling.
"""

def parallel_tool_names(task, names):
    """Use the ordinary pipeline's live write/ordered-state classification."""
    from lib.tasks_pkg.tool_dispatch._flags import _task_partitions, ORDERED_STATE_TOOLS
    writes, _ = _task_partitions(task)
    return frozenset(names) - writes - ORDERED_STATE_TOOLS

def plan_concurrent_round(task, parsed_calls, project_path):
    """Order exceptions for a caller that starts every call of a round at once.

    Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md): the
    same ``_ordering`` authority as ``execute_tool_pipeline``. Returns
    ``({call_id: [earlier call ids]}, CommandDirectoryPlan)``: only calls
    with predecessors are listed, and every run_command ``working_dir`` is
    frozen into its occurrence's arguments in call order; the caller settles
    inherited directories through the plan's ``resolve``/``settle``.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _task_partitions
    from lib.tasks_pkg.tool_dispatch._ordering import call_predecessors, plan_command_working_dirs
    writes, _ = _task_partitions(task)
    calls = [item for item in parsed_calls if not item[6]]
    command_dirs = plan_command_working_dirs(task, calls)
    predecessors = call_predecessors(task, project_path, calls, writes)
    return ({call_id: list(earlier) for call_id, earlier in predecessors.items() if earlier},
            command_dirs)

def repair_prepared_call_ids(task, parsed_calls, round_num, project_path):
    """Give recycled or repeated model call IDs their admission identity early.

    ``execute_tool_pipeline`` remints an ID that history already used (a
    positional-ID provider repeats ``{tool}_{index}`` every response) or that
    repeats within one response. A caller that decides approval before the
    pipeline admits the occurrence binds the receipt to the call ID, so the
    remint would orphan it; applying the same rule first keeps one identity
    from decision to execution. Replaces changed items in ``parsed_calls``.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _ensure_tool_call_id_receipts
    from lib.tasks_pkg.tool_dispatch._pipeline import (
        _collect_used_tool_call_ids, _repair_current_tool_call_identity)
    from lib.tasks_pkg.tool_dispatch._prefetch import remap_prefetch
    rows = tuple(item[5] for item in parsed_calls if isinstance(item[5], dict))
    historical = _collect_used_tool_call_ids(task, [], current_round_entries=rows)
    used, claimed = set(historical), set()
    receipts = _ensure_tool_call_id_receipts(task)
    for index, item in enumerate(parsed_calls):
        original = item[2]
        item = _repair_current_tool_call_identity(
            task, [], parsed_calls, index, item, round_num=round_num, tid=task['id'][:8],
            receipts=receipts, historical_used_ids=historical, used_ids=used,
            claimed_ids=claimed, settled_results={}, pending_envelopes={})
        if item[2] != original:
            remap_prefetch(task, item[1], original, item[2], item[3], item[5], project_path)

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
from lib.tasks_pkg.tool_dispatch._ordering import restore_command_sticky_cwd


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
    from lib.tasks_pkg.delegate_mode import delegate_call_rejection
    if (coordinator_call_rejection(task, name, arguments, project_path)
            or delegate_call_rejection(task, name, arguments)):
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
    from lib.tool_results.tool_round_status import TERMINAL_TOOL_ROUND_STATUSES
    if row.get('status') in TERMINAL_TOOL_ROUND_STATUSES | {'skipped', 'unknown'}:
        return
    from lib.tasks_pkg.executor import _finalize_tool_round
    from lib.tools.registry import build_tool_result_meta
    meta = build_tool_result_meta(name, arguments, 'Tool approval did not reach execution.')
    meta.update(badge=status, writeOk=False)
    _finalize_tool_round(task, number, row, [meta], status=status)


__all__ = ['execute_tool_pipeline', 'parse_tool_calls', 'authorize_prepared_tool_call', 'withdraw_prepared_tool_call', 'publish_prepared_response_siblings', 'publish_tool_call_entry', 'parallel_tool_names', 'plan_concurrent_round', 'repair_prepared_call_ids', 'restore_command_sticky_cwd']

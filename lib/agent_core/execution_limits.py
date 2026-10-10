"""Optional task consumption limits at the shared loop admission boundaries.

Entry point: execution_limit_reason. Dependencies: canonical usage accounting.
Limits stop future dispatch; one in-flight model call can exceed the remaining
reported-token budget. Individual tool invocations are admitted atomically,
including streaming and nested gateway dispatch.
"""
from __future__ import annotations

import time
from lib.cost import merge_usage_totals, normalize_usage


def validate_execution_limits(limits) -> None:
    """Absent object or null dimensions are unbounded; numeric zero denies all.

    All keys remain required on a supplied object to reject malformed policies.
    """
    if limits is None:
        return
    import math
    if not isinstance(limits, dict):
        raise ValueError('executionLimits must be an object')
    for key in ('max_tokens', 'max_tool_calls'):
        if key not in limits or (limits[key] is not None and
                                 (type(limits[key]) is not int or limits[key] < 0)):
            raise ValueError(f'executionLimits.{key} must be null or a nonnegative integer')
    deadline = limits.get('deadline')
    if 'deadline' not in limits or (deadline is not None and
            (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline < 0)):
        raise ValueError('executionLimits.deadline must be null or a finite nonnegative timestamp')


def execution_limit_reason(task: dict, *, pending_tools: int = 0) -> str | None:
    budget = task.get('_executionBudget')
    if budget is not None:
        reason = budget.reason(pending_tools=pending_tools)
        if reason:
            task['_executionLimitReason'] = reason
            return reason
    limits = (task.get('config') or {}).get('executionLimits')
    if not isinstance(limits, dict):
        return None
    # ``_swarmChildUsage`` folds completed sub-agents into the same envelope
    # (lib/swarm/integration/_tools.py); unbounded tasks never carry it.
    usage = normalize_usage(merge_usage_totals(
        task.get('usage'), task.get('_swarmChildUsage')))
    calls = budget.tool_calls_for_control(task) if budget is not None else task.get('_executionToolsAdmitted', 0)
    reason = None
    if limits['deadline'] is not None and time.time() >= limits['deadline']:
        reason = 'execution_deadline_exceeded'
    elif limits['max_tokens'] is not None and usage['input'] + usage['output'] >= limits['max_tokens']:
        reason = 'execution_token_budget_exhausted'
    elif pending_tools and limits['max_tool_calls'] is not None and calls + pending_tools > limits['max_tool_calls']:
        reason = 'execution_tool_budget_exhausted'
    if reason:
        task['_executionLimitReason'] = reason
    return reason


def count_tool_calls(task: dict) -> int:
    """Count current per-call receipts and legacy grouped receipts once."""
    count = 0
    ids = set()
    for row in task.get('toolRounds') or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get('tool_calls'), list):
            count += len(row['tool_calls'])
        elif row.get('toolName') or row.get('toolCallId'):
            call_id = row.get('toolCallId')
            if not call_id or call_id not in ids:
                count += 1
            if call_id:
                ids.add(call_id)
    return count


def admit_tool_execution(task: dict) -> str | None:
    """Atomic per-call admission, including streaming and nested gateway lanes."""
    import threading
    limits = (task.get('config') or {}).get('executionLimits')
    budget = task.get('_executionBudget')
    if not isinstance(limits, dict) and budget is None:
        return None
    lock = task.setdefault('_executionBudgetLock', threading.Lock())
    with lock:
        reason = execution_limit_reason(task)
        admitted = task.get('_executionToolsAdmitted', 0)
        if (not reason and isinstance(limits,dict) and limits['max_tool_calls'] is not None
                and admitted >= limits['max_tool_calls']):
            reason = 'execution_tool_budget_exhausted'
        budget = task.get('_executionBudget')
        if not reason and budget is not None:
            reason = budget.reserve_tool(task)
        if reason:
            task['_executionLimitReason'] = reason
            task['aborted'] = True
            if task.get('abort_event') is not None:
                task['abort_event'].set()
            return reason
        task['_executionToolsAdmitted'] = admitted + 1
    return None

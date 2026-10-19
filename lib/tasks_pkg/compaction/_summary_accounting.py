"""Bind auxiliary summary requests to the already admitted execution account.

This is a policy adapter, not a ledger or a reservation. Physical transports
count/reserve/mark/settle each occurrence through ExecutionBudget, whose existing
observers fence the dispatch grant and original human request atomically.
"""
from __future__ import annotations


def summary_model_admission(task):
    """Return the existing model-admission port, or refuse unaccounted control work."""
    if not isinstance(task, dict):
        return None
    from lib.agent_core.execution_budget import ExecutionBudget, model_budget_admission
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch.admission import is_dispatch_task
    from lib.project_dispatch.continuation import current_continuation

    dispatch = is_dispatch_task(task)
    if dispatch and current_continuation(task, require_account=True) is None:
        # Other consultation/worker profiles retain their narrower prohibition.
        raise AbortedError('Project consultation cannot purchase auxiliary compaction summaries')
    budget = task.get('_executionBudget')
    if budget is None:
        if dispatch or task.get('_coordinatorRequestBudget') or task.get('_coordinatorDispatchScope'):
            raise AbortedError('Coordinator summary requires its existing execution account')
        return None  # Standalone utility/manual callers have no task account.
    if not isinstance(budget, ExecutionBudget):
        raise AbortedError('Summary requires the canonical execution account')
    admission = model_budget_admission(task)

    def admit(**counts):
        if task.get('_executionBudget') is not budget:
            raise AbortedError('Summary execution account changed before dispatch')
        if dispatch and current_continuation(task, require_account=True) is None:
            raise AbortedError('Summary lost its coordinator continuation before dispatch')
        return admission(**counts)

    return admit

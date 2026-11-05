"""Bind auxiliary summary requests to the already admitted execution account.

This is a policy adapter, not a ledger or a reservation. Physical transports
count/reserve/mark/settle each occurrence through ExecutionBudget, whose existing
observers fence the dispatch grant and original human request atomically.
Every project-dispatch profile (consult, files, project, coordinator) binds its
own grant's account at task start, so its summaries charge that same account
under that grant's recorded ceilings; no profile gains a second account.
"""
from __future__ import annotations


def _bound_dispatch_grant(task):
    """Return this executor's validated grant binding; unknown authority stops the task."""
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch.admission import stop_on_authority_failure, validate_task
    try:
        return validate_task(task, require_account=True)
    except Exception as error:
        # The first stop cause wins; validate_task normally recorded it already.
        stop_on_authority_failure(task, error)
        raise AbortedError(
            'Project dispatch authority is unavailable for auxiliary compaction '
            f'summaries: {error}') from error


def summary_model_admission(task):
    """Return the existing model-admission port, or refuse unaccounted control work."""
    if not isinstance(task, dict):
        return None
    from lib.agent_core.execution_budget import ExecutionBudget, model_budget_admission
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch.admission import is_dispatch_task, stop_on_authority_failure

    dispatch = is_dispatch_task(task)
    # The grant check also proves the task holds that grant's bound account.
    grant = _bound_dispatch_grant(task) if dispatch else None
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
        if dispatch and _bound_dispatch_grant(task) != grant:
            # A different, otherwise valid carrier is not this summary's authority.
            lost = AbortedError('Summary lost its dispatch grant before dispatch')
            stop_on_authority_failure(task, lost)
            raise lost
        return admission(**counts)

    return admit

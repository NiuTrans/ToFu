"""Summary attempt lifecycle: cancellation and failure deduplication, never a timer.

A slow valid summary must be allowed to finish. Provider transport failures and
user cancellation remain authoritative; incomplete output is never adopted.
Legacy ``summaryBudgetSeconds`` values are deliberately ignored. Elapsed time
is telemetry, not permission to interrupt a summary or discard its source.
"""
import time


def bounded_summary_task(task):
    """Tasks whose source must survive a failed summary, without lossy recovery."""
    return isinstance(task, dict) and bool(
        task.get('_transientRuntime')
        or (task.get('id') and task.get('convId'))
        or (task.get('config') or {}).get('disableModelFallback'))


def summary_failure_reason(task):
    """Read current keyed-attempt failure; never revive an older task failure."""
    if not isinstance(task, dict):
        return ''
    state = task.get('_summaryAttemptBudget')
    if not isinstance(state, dict):
        state = task
    return str(state.get('_summaryBudgetFailure') or '')


class SummaryBudget:
    """Compatibility name for a cancellation-only summary attempt guard."""

    def __init__(self, task, parent_abort=None, *, request_key=None):
        if request_key is None:
            self.task = task
        else:
            state = task.get('_summaryAttemptBudget')
            if not isinstance(state, dict) or state.get('requestKey') != request_key:
                state = {'requestKey': request_key}
                task['_summaryAttemptBudget'] = state
            self.task = state
        self.parent_abort = parent_abort
        self.started = time.monotonic()

    def is_set(self):
        return bool(
            self.task.get('_summaryBudgetFailure')
            or (self.parent_abort and self.parent_abort()))

    def finish(self, success, *, failure_reason='summary_dispatch_failed'):
        self.task['_summaryBudgetSpentSeconds'] = float(
            self.task.get('_summaryBudgetSpentSeconds') or 0) + max(
                0.0, time.monotonic() - self.started)
        if not success:
            if self.parent_abort and self.parent_abort():
                self.task['_summaryBudgetFailure'] = 'summary_cancelled'
            else:
                # A suppressed repeat is not a new dispatch failure. Retain
                # the first useful cause for admission and durable receipts.
                self.task.setdefault('_summaryBudgetFailure', failure_reason)

"""Transient summary budget: task-local cumulative time, no worker or retry loop.

The existing streaming abort seam covers slot waits, backoff and transport.
A failed attempt latches for this task so unchanged admission cannot restart it.
"""
import math
import time

DEFAULT_SUMMARY_BUDGET_SECONDS = 60.0
MAX_SUMMARY_BUDGET_SECONDS = 120.0


def bounded_summary_task(task):
    return isinstance(task, dict) and bool(
        task.get('_transientRuntime')
        or (task.get('config') or {}).get('disableModelFallback'))


class SummaryBudget:
    def __init__(self, task, parent_abort=None):
        self.task = task
        self.parent_abort = parent_abort
        cfg = (task.get('config') or {}).get('compaction') or {}
        raw = cfg.get('summaryBudgetSeconds', DEFAULT_SUMMARY_BUDGET_SECONDS)
        try:
            seconds = float(raw)
            if isinstance(raw, bool) or not math.isfinite(seconds):
                raise ValueError('invalid summary budget')
        except (TypeError, ValueError, OverflowError):
            seconds = DEFAULT_SUMMARY_BUDGET_SECONDS
        seconds = max(0.0, min(seconds, MAX_SUMMARY_BUDGET_SECONDS))
        self.started = time.monotonic()
        self.deadline = self.started + max(
            0.0, seconds - float(task.get('_summaryBudgetSpentSeconds') or 0))

    def is_set(self):
        return bool(
            self.task.get('_summaryBudgetFailure')
            or (self.parent_abort and self.parent_abort())
            or time.monotonic() >= self.deadline)

    def finish(self, success):
        self.task['_summaryBudgetSpentSeconds'] = float(
            self.task.get('_summaryBudgetSpentSeconds') or 0) + max(
                0.0, time.monotonic() - self.started)
        if not success:
            self.task['_summaryBudgetFailure'] = (
                'summary_budget_exhausted' if time.monotonic() >= self.deadline
                else 'summary_dispatch_failed')

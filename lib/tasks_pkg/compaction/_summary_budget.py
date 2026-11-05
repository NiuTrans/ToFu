"""Summary attempt lifecycle: cancellation and failure deduplication, never a timer.

A slow valid summary must be allowed to finish. Provider transport failures and
user cancellation remain authoritative; incomplete output is never adopted.
Legacy ``summaryBudgetSeconds`` values are deliberately ignored. Elapsed time
is telemetry, not permission to interrupt a summary or discard its source.
Keyed guards also own bounded, redacted failure metadata; only their current
in-process projection may reconstruct a typed credential cause at admission.
"""
import time

from lib.tasks_pkg.compaction._summary_failure import (
    SUMMARY_FAILURE_CODE,
    _failure_matches_attempts,
    validate_summary_attempts,
    validate_summary_failure,
)

_BUDGET_FAILURE_REASONS = frozenset({
    SUMMARY_FAILURE_CODE, 'summary_dispatch_failed', 'summary_incomplete',
    'summary_cancelled', 'summary_route_revalidation_failed',
})


def bounded_summary_task(task):
    """Tasks whose source must survive a failed summary, without lossy recovery."""
    from lib.tasks_pkg.invocation import is_child_invocation
    return isinstance(task, dict) and bool(
        task.get('_transientRuntime')
        or task.get('_invocationStorageFree')
        or is_child_invocation(task)
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


class _SummaryAttemptState(dict):
    """Issued task/key state; copying or serializing retains data, never trust.

    Private snapshots contain only validated scalar fields, not an exception or
    credential/source reference. The original task is borrowed for exact identity
    fencing, never cloned, and the state has no process-global registry/lifecycle.
    """

    __slots__ = ('_owner_task', '_request_key', '_failure', '_attempts',
                 'projected_failure')

    def __init__(self, task, request_key):
        super().__init__(requestKey=request_key)
        self._owner_task = task
        self._request_key = request_key
        self._failure = None
        self._attempts = []
        self.projected_failure = None

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        from copy import deepcopy
        return deepcopy(dict(self), memo)

    def is_current(self, task):
        return (self._owner_task is task
                and task.get('_summaryAttemptBudget') is self
                and type(self._request_key) is str and bool(self._request_key)
                and self.get('requestKey') == self._request_key)

    def validated_metadata(self):
        """Revalidate public fields against independently held safe snapshots."""
        attempts = validate_summary_attempts(self.get('_summaryAttempts'))
        if ('_summaryAttempts' in self and not attempts) or attempts != self._attempts:
            return None, []
        failure = validate_summary_failure(self.get('_summaryFailure'))
        if (failure != self._failure
                or (failure is not None and not _failure_matches_attempts(failure, attempts))):
            return None, attempts
        return failure, attempts

    def record_metadata(self, failure, attempts):
        """Store detached safe copies and invalidate the prior usage witness."""
        self.pop('_summaryFailure', None)
        self.pop('_summaryAttempts', None)
        self.projected_failure = None
        self._failure = dict(failure) if failure else None
        self._attempts = [dict(row) for row in attempts]
        if self._failure is not None:
            self['_summaryFailure'] = dict(self._failure)
        if self._attempts:
            self['_summaryAttempts'] = [dict(row) for row in self._attempts]


def cancel_summary_attempt(task):
    """Withdraw current issued failure authority when owner Stop wins later.

    Actual attempt rows remain accounting evidence; the old usage projection
    cannot certify a present credential outage after cancellation.
    """
    if not isinstance(task, dict):
        return
    state = task.get('_summaryAttemptBudget')
    if type(state) is _SummaryAttemptState and state.is_current(task):
        _failure, attempts = state.validated_metadata()
        state['_summaryBudgetFailure'] = 'summary_cancelled'
        state.record_metadata(None, attempts)


class SummaryBudget:
    """Compatibility name for a cancellation-only summary attempt guard."""

    def __init__(self, task, parent_abort=None, *, request_key=None):
        self.owner_task = task
        if request_key is None:
            self.task = task
        else:
            state = task.get('_summaryAttemptBudget')
            if (type(state) is not _SummaryAttemptState
                    or not state.is_current(task) or state.get('requestKey') != request_key):
                state = _SummaryAttemptState(task, request_key)
                task['_summaryAttemptBudget'] = state
            self.task = state
        self.parent_abort = parent_abort
        self.started = time.monotonic()

    def is_set(self):
        return bool(
            self.task.get('_summaryBudgetFailure')
            or (self.parent_abort and self.parent_abort()))

    def finish(self, success, *, failure_reason='summary_dispatch_failed',
               failure_evidence=None, attempts=None):
        """Settle one keyed attempt, including safe metadata without raw errors.

        ``None`` on a suppressed repeat preserves already validated metadata.
        New invalid evidence is discarded, not sanitized into a trusted cause.
        Success or owner cancellation withdraws any previous credential failure.
        """
        if type(failure_reason) is not str or failure_reason not in _BUDGET_FAILURE_REASONS:
            failure_reason = 'summary_dispatch_failed'
        state = self.task if type(self.task) is _SummaryAttemptState else None
        if state is not None and not state.is_current(self.owner_task):
            return  # A late older guard cannot settle the replacement request.
        prior_failure, prior_attempts = state.validated_metadata() if state else (None, [])
        self.task['_summaryBudgetSpentSeconds'] = float(
            self.task.get('_summaryBudgetSpentSeconds') or 0) + max(
                0.0, time.monotonic() - self.started)
        cancelled = bool(self.parent_abort and self.parent_abort())
        if cancelled:
            self.task['_summaryBudgetFailure'] = 'summary_cancelled'
        elif success:
            self.task.pop('_summaryBudgetFailure', None)
        else:
            # A suppressed repeat is not a new dispatch failure. Retain
            # the first useful cause for admission and durable receipts.
            self.task.setdefault('_summaryBudgetFailure', failure_reason)
        if state is None:
            return  # Unkeyed utility guards cannot mint current keyed evidence.
        safe_attempts = (prior_attempts if attempts is None
                         else validate_summary_attempts(attempts))
        attempts_valid = (attempts is None or type(attempts) is list
                          and (not attempts or bool(safe_attempts)))
        safe_failure = (prior_failure if failure_evidence is None
                        else validate_summary_failure(failure_evidence))
        if (success or cancelled or not attempts_valid
                or self.task.get('_summaryBudgetFailure') != SUMMARY_FAILURE_CODE
                or (safe_failure is not None
                    and not _failure_matches_attempts(safe_failure, safe_attempts))):
            safe_failure = None
        state.record_metadata(safe_failure, safe_attempts)

    def project_usage(self, usage_out):
        """Publish the current key and detached safe metadata; never raw causes.

        Call after finish, or after a same-key guard suppresses dispatch. The
        final compactor's shallow metadata copy retains this projection witness;
        receipts use only the allowlisted fields and never expose the key.
        """
        if not isinstance(usage_out, dict):
            return
        for key in ('_summaryRequestKey', '_summaryFailure', '_summaryAttempts'):
            usage_out.pop(key, None)
        state = self.task
        if type(state) is not _SummaryAttemptState or not state.is_current(self.owner_task):
            return
        failure, attempts = state.validated_metadata()
        usage_out['_summaryRequestKey'] = state['requestKey']
        if failure is not None and state.get('_summaryBudgetFailure') == SUMMARY_FAILURE_CODE:
            usage_out['_summaryFailure'] = failure
            state.projected_failure = failure
        else:
            state.projected_failure = None
        if attempts:
            usage_out['_summaryAttempts'] = attempts

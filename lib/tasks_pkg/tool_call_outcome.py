"""First-attempt tool-call outcome audit events.

Single owner of the ``tool_call_outcome`` and ``ptc_program_outcome`` audit
taxonomy behind ``docs/TOOL_CALL_SUCCESS_RENDERING_DESIGN_PROPOSAL.md``
Proposal A. Each settled tool round and each terminal program run is
audited at most once; the offline report
(``debug/tool_call_success_report.py``) aggregates these lines into
first-attempt pass rates by model x tool x failure category.

These events are diagnostic evidence only — never an admission, routing,
or access gate. Emission is best-effort: a diagnostics failure must never
fail the tool round or program it describes. Argument VALUES stay out of
the payload for the same reason ``tool_gateway_repaired`` excludes them;
tool names are public catalog metadata.
"""

from __future__ import annotations

from typing import Any

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

#: Terminal non-done verdicts that map 1:1 onto an outcome label. 'done'
#: and 'rejected' are derived from repair/rejection evidence instead.
_OUTCOME_STATUSES = frozenset({'error', 'aborted', 'unanswerable'})

#: Bound on the repair-pattern list attached to one event. Patterns are
#: short vocabulary labels, but one pathological call must not bloat the
#: bounded JSONL row.
_MAX_REPAIR_PATTERNS = 8


def _base(task: dict[str, Any]) -> dict[str, Any]:
    return {
        'model': str(task.get('model') or ''),
        'task_id': str(task.get('id') or ''),
        'conv_id': str(task.get('convId') or ''),
    }


def audit_round_outcome(
        task: dict[str, Any], round_entry: Any, *, status: str) -> None:
    """Emit one ``tool_call_outcome`` audit for a settled tool round.

    Idempotent per round dict: the first audit wins, so a late protective
    finalize (pool-timeout lane finishing after the pipeline recorded a
    verdict) cannot double-count one model action.

    Execution audits enter from canonical pipeline settlement, after handler
    returns and timeout decisions. Direct executor consumers audit on executor
    exit. Handler presentation updates are not final execution evidence.
    Admission may separately audit a proven rejection.

    Outcome mapping:
      * ``status='rejected'`` → ``rejected`` + ``rejection_kind`` from the
        typed descriptor (hallucinated / tool_contract_invalid /
        programmatic_* / multi_agent_read_only / ...).
      * ``status='done'`` with harness repairs → ``repaired`` + the
        bounded repair-pattern list.
      * ``status='done'`` clean → ``ok``.
      * anything else → the raw verdict (``error`` / ``aborted`` /
        ``unanswerable``).
    """
    try:
        if not isinstance(round_entry, dict) \
                or round_entry.get('_outcomeAudited'):
            return
        round_entry['_outcomeAudited'] = True
        status = str(status or '')
        tool = str(round_entry.get('toolName') or '')
        detail = {**_base(task), 'tool': tool, 'status': status}
        source = str(round_entry.get('source') or '')
        if source:
            detail['source'] = source
        if round_entry.get('_programCallId'):
            detail['program_child'] = True
        from lib.tool_results.tool_rejection import tool_rejection_descriptor
        rejection = tool_rejection_descriptor(round_entry)
        if status == 'rejected':
            detail['outcome'] = 'rejected'
            kind = str((rejection or {}).get('kind') or '')
            if kind:
                detail['rejection_kind'] = kind
            attempted = str((rejection or {}).get('attempted') or '')
            if attempted:
                detail['attempted'] = attempted
        elif status == 'done':
            repaired = round_entry.get('_repaired')
            patterns = []
            if isinstance(repaired, dict):
                patterns = [str(pattern) for pattern in
                            (repaired.get('patterns') or ())
                            [:_MAX_REPAIR_PATTERNS]]
            if patterns:
                detail['outcome'] = 'repaired'
                detail['repairs'] = patterns
            else:
                detail['outcome'] = 'ok'
        else:
            detail['outcome'] = (
                status if status in _OUTCOME_STATUSES else 'error')
        audit_log('tool_call_outcome', **detail)
    except Exception as exc:  # diagnostics never fail the round
        logger.debug('[tool_call_outcome] round audit skipped: %s', exc)


def audit_gateway_rejected_call(
        task: dict[str, Any], error: Any, *, source: str) -> None:
    """Emit ``tool_call_outcome`` for a gateway normalize-time refusal.

    Gateway children that fail ``normalize_gateway_call`` never become
    tool rounds, so the round-level audit cannot see them; each normalize
    error dict is produced exactly once per model-issued call, which keeps
    this emission idempotence-free by construction.
    """
    try:
        if not isinstance(error, dict):
            return
        code = str(error.get('code') or '')
        detail = {
            **_base(task),
            'tool': str(error.get('name') or ''),
            'status': 'rejected',
            'outcome': 'rejected',
            'rejection_kind': f'gateway_{code}' if code else 'gateway',
            'source': str(source or ''),
        }
        attempted = str(error.get('attempted') or '')
        if attempted:
            detail['attempted'] = attempted
        audit_log('tool_call_outcome', **detail)
    except Exception as exc:  # diagnostics never fail the gateway
        logger.debug('[tool_call_outcome] gateway audit skipped: %s', exc)


def audit_program_outcome(
        task: dict[str, Any], run: dict[str, Any], *,
        backend: str, status: str) -> None:
    """Emit one ``ptc_program_outcome`` audit for a terminal program run.

    Called from the sole program-projection writer once per distinct
    terminal state; covers ``native_openai`` and ``local_javascript``.
    Historical ``local_toolscript`` records retain their measured syntax
    repairs; new JavaScript programs are never auto-rewritten. The report's
    "authored clean on the first attempt" rate is backend-neutral.
    """
    try:
        if not isinstance(run, dict) or run.get('_programOutcomeAudited'):
            return
        run['_programOutcomeAudited'] = True
        children = [child for child in (run.get('childCalls') or ())
                    if isinstance(child, dict)]
        rejected_children = sum(
            1 for child in children if child.get('status') == 'rejected')
        error = run.get('error')
        error = error if isinstance(error, dict) else {}
        detail = {
            **_base(task),
            'backend': str(backend or ''),
            'status': str(status or ''),
            'children': len(children),
            'rejected_children': rejected_children,
            'syntax_repairs': len(run.get('syntaxRepairs') or ()),
            'duration_ms': max(
                0, int(run.get('tEnd') or 0) - int(run.get('tStart') or 0)),
        }
        error_code = str(error.get('code') or error.get('kind') or '')
        if error_code:
            detail['error_code'] = error_code
        audit_log('ptc_program_outcome', **detail)
    except Exception as exc:  # diagnostics never fail the program
        logger.debug('[tool_call_outcome] program audit skipped: %s', exc)


__all__ = [
    'audit_gateway_rejected_call',
    'audit_program_outcome',
    'audit_round_outcome',
]

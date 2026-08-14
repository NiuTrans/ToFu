"""Transport and persistence projections for canonical terminal outcomes."""

from __future__ import annotations

from typing import Any

from lib.agent_verdict import is_incomplete_stop
from lib.orchestration.outcome_domain import (
    TerminalOutcome,
    classify_terminal_outcome,
    outcome_from_result,
)


def _error_message(value: Any) -> str:
    if isinstance(value, dict):
        for key in ('message', 'detail', 'error'):
            if value.get(key):
                return str(value[key])
        return ''
    return str(value or '')


def outcome_from_run_header(run: dict | None) -> TerminalOutcome | None:
    """Recover the canonical outcome represented by one durable run header."""
    run = run if isinstance(run, dict) else {}
    status = str(run.get('status') or '')
    from lib.orchestration.run_status import is_terminal_run_status
    terminal = bool(run.get('terminal')) or is_terminal_run_status(status)
    if not terminal:
        return None

    error_value = run.get('error')
    error_envelope = error_value if isinstance(error_value, dict) else {}
    embedded = error_envelope.get('outcome')
    if isinstance(embedded, dict):
        return outcome_from_result({
            'outcome': embedded,
            'error': _error_message(error_value),
        })
    if status == 'done':
        return classify_terminal_outcome('completed', reported_ok=True)
    if status == 'aborted':
        return classify_terminal_outcome('aborted')

    detail = _error_message(error_value)
    if is_incomplete_stop(detail):
        return classify_terminal_outcome(
            'completed',
            reported_ok=False,
            reported_stop_reason=detail,
        )
    return classify_terminal_outcome(
        'failed', error=detail, failure_kind='failed')


def project_run_header_outcome(run: dict | None) -> dict | None:
    """Detach and repair a durable header before publishing it to clients."""
    if run is None:
        return None
    projected = dict(run)
    definition = projected.get('definition')
    if isinstance(definition, dict):
        from lib.orchestration._layout import project_definition_layout
        projected['definition'] = project_definition_layout(definition)
    terminal = outcome_from_run_header(projected)
    if terminal is not None:
        projected['outcome'] = terminal.as_dict()
    return projected


def project_terminal_result(
    result: dict | None,
    outcome: TerminalOutcome,
) -> dict:
    """Publish a terminal result through the single versioned outcome shape."""
    projected = dict(result or {})
    projected.update({
        'ok': outcome.ok,
        'status': outcome.engine_status,
        'stop_reason': outcome.stop_reason,
        'outcome': outcome.as_dict(),
    })
    return projected


def failure_result(exc: Exception, failure_kind: str) -> dict:
    """Build the shared synthetic result for an execution exception."""
    detail = f'{type(exc).__name__}: {exc}'
    terminal = classify_terminal_outcome(
        'failed', error=detail, failure_kind=failure_kind)
    return project_terminal_result({
        'final': '',
        'transcript': [],
        'trace': [],
        'loop_exits': [],
        'agents_run': 0,
        'artifacts': [],
        'error': detail,
    }, terminal)


def aborted_result(result: dict | None) -> dict:
    """Project an abort fence over a detached execution result."""
    projected = dict(result or {})
    projected['final'] = ''
    projected.pop('error', None)
    terminal = classify_terminal_outcome('aborted')
    return project_terminal_result(projected, terminal)


__all__ = [
    'aborted_result',
    'failure_result',
    'outcome_from_run_header',
    'project_run_header_outcome',
    'project_terminal_result',
]

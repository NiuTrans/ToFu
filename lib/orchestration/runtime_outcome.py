"""Normalized orchestration execution outcomes.

This module owns the transport-independent result model and the two synthetic
terminal outcomes used by runtime projection. Worker orchestration remains in
``runtime_service`` so adapters can keep one stable execution entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lib.log import get_logger
from lib.orchestration.outcome_domain import (
    TerminalOutcome,
    outcome_from_result,
)
from lib.orchestration.outcome_projection import aborted_result, failure_result


logger = get_logger(__name__)


@dataclass
class FlowRunOutcome:
    """Normalized result of one FlowExecutor invocation."""

    result: dict
    executor: Any = None
    exception: Exception | None = None
    failure_kind: str = ''

    @property
    def terminal_outcome(self) -> TerminalOutcome:
        return outcome_from_result(
            self.result, failure_kind=self.failure_kind)

    @property
    def lifecycle_status(self) -> str:
        return self.terminal_outcome.lifecycle_status

    @property
    def error(self) -> str:
        return self.terminal_outcome.runtime_error

    @property
    def error_envelope(self) -> dict | None:
        return self.terminal_outcome.error_envelope

def failure_outcome(
    error: Exception,
    failure_kind: str,
    *,
    executor=None,
) -> FlowRunOutcome:
    """Build the one transport-safe failure shape used by every run path."""
    logger.error(
        '[OrchestrationRuntime] flow execution failed kind=%s: %s',
        failure_kind,
        error,
        exc_info=(type(error), error, error.__traceback__)
        if error.__traceback__ is not None else False,
    )
    return FlowRunOutcome(
        failure_result(error, failure_kind),
        executor=executor,
        exception=error,
        failure_kind=failure_kind,
    )


def aborted_race_outcome(outcome: FlowRunOutcome) -> FlowRunOutcome:
    """Align the in-memory result when a persisted abort wins the fence."""
    return FlowRunOutcome(
        aborted_result(outcome.result),
        executor=outcome.executor,
        failure_kind='aborted',
    )


__all__ = [
    'FlowRunOutcome',
    'failure_outcome',
    'aborted_race_outcome',
]

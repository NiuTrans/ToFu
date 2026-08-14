"""Compatibility facade for canonical outcome semantics and projections.

New runtime code should depend on ``outcome_domain`` or
``outcome_projection`` directly. This module preserves the established import
surface for extensions while keeping classification independent from adapters.
"""

from lib.orchestration.outcome_contract import (
    OUTCOME_ERROR_DISPLAY_CHARS,
    OUTCOME_FINAL_DISPLAY_CHARS,
)
from lib.orchestration.outcome_domain import (
    OUTCOME_CATEGORIES,
    OUTCOME_FORMAT,
    TerminalOutcome,
    classify_terminal_outcome,
    outcome_from_result,
)
from lib.orchestration.outcome_projection import (
    aborted_result,
    failure_result,
    outcome_from_run_header,
    project_run_header_outcome,
    project_terminal_result,
)


__all__ = [
    'OUTCOME_CATEGORIES',
    'OUTCOME_ERROR_DISPLAY_CHARS',
    'OUTCOME_FINAL_DISPLAY_CHARS',
    'OUTCOME_FORMAT',
    'TerminalOutcome',
    'aborted_result',
    'classify_terminal_outcome',
    'failure_result',
    'outcome_from_result',
    'outcome_from_run_header',
    'project_run_header_outcome',
    'project_terminal_result',
]

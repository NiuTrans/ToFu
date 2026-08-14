"""Compatibility facade for modular orchestration outcome ownership."""

from lib.orchestration.outcome_contract import (
    OUTCOME_ERROR_DISPLAY_CHARS,
    OUTCOME_FINAL_DISPLAY_CHARS,
    outcome_contract,
    outcome_contract_schema,
    outcome_payload_schema,
)
from lib.orchestration.outcome_domain import (
    OUTCOME_CATEGORIES,
    OUTCOME_FORMAT,
    TerminalOutcome,
    classify_terminal_outcome,
    outcome_from_result,
)
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration.outcome_projection import (
    aborted_result,
    failure_result,
    outcome_from_run_header,
    project_run_header_outcome,
    project_terminal_result,
)


__all__ = [
    'OUTCOME_FORMAT', 'OUTCOME_CATEGORIES', 'OUTCOME_FINAL_DISPLAY_CHARS',
    'OUTCOME_ERROR_DISPLAY_CHARS', 'TerminalOutcome',
    'OrchestrationOutcomeLedger', 'classify_terminal_outcome',
    'outcome_from_result', 'project_terminal_result', 'failure_result',
    'aborted_result', 'outcome_from_run_header', 'project_run_header_outcome',
    'outcome_contract', 'outcome_contract_schema', 'outcome_payload_schema',
]

"""Compatibility facade for the modular orchestration mutation contract."""

from lib.orchestration.mutation_contract import (
    mutation_contract,
    mutation_contract_schema,
    mutation_payload_schema,
    mutation_response_schema,
)
from lib.orchestration.mutation_operations import (
    resolved_mutation,
    runtime_abort_mutation,
)
from lib.orchestration.mutation_response import (
    mutation_error_message,
    mutation_http_status,
    mutation_response,
)
from lib.orchestration.mutation_result import (
    MUTATION_ACCEPTED,
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_ACTION_TRANSITION_RUN,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_FORMAT,
    MUTATION_LEGACY_GATE_ID_FIELD,
    MUTATION_LEGACY_RUN_ID_FIELD,
    MUTATION_LEGACY_RUN_STATUS_FIELD,
    MUTATION_LEGACY_STATUS_FIELD,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    MUTATION_TRANSPORT_FAILED,
    OrchestrationMutationResult,
    RunMutationResult,
)


__all__ = [
    'MUTATION_FORMAT', 'MUTATION_ACCEPTED', 'MUTATION_NOT_FOUND',
    'MUTATION_TERMINAL', 'MUTATION_ACTIVE', 'MUTATION_CONFLICT',
    'MUTATION_PERSISTENCE_FAILED', 'MUTATION_TRANSPORT_FAILED',
    'MUTATION_ACTION_ABORT_RUN',
    'MUTATION_ACTION_DELETE_RUN', 'MUTATION_ACTION_APPROVE_GATE',
    'MUTATION_ACTION_INPUT_GATE', 'MUTATION_ACTION_TRANSITION_RUN',
    'MUTATION_LEGACY_RUN_ID_FIELD', 'MUTATION_LEGACY_GATE_ID_FIELD',
    'MUTATION_LEGACY_RUN_STATUS_FIELD', 'MUTATION_LEGACY_STATUS_FIELD',
    'OrchestrationMutationResult', 'RunMutationResult',
    'resolved_mutation', 'runtime_abort_mutation', 'mutation_http_status',
    'mutation_error_message', 'mutation_response', 'mutation_contract',
    'mutation_contract_schema', 'mutation_payload_schema',
    'mutation_response_schema',
]

"""Compatibility facade for focused orchestration wire-contract owners.

New consumers may import a precise definition/runtime/inspection/limit module;
rolling extensions keep this stable aggregate path without duplicating policy.
"""

from lib.orchestration._definition_contract import MAX_NAME_LEN, MAX_NODES
from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH
from lib.orchestration.definition_wire_contracts import (
    MAX_DEFINITION_VERSION,
    definition_candidate_schema,
    definition_entry_contract,
    definition_entry_summary,
    definition_list_contract,
    definition_request_schema,
    definition_write_conflict,
    definition_write_contract,
    definition_write_version_token,
    parse_definition_write_precondition,
    project_definition_entry,
    project_definition_list,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.durable_run_field_registry import (
    durable_run_contract,
    project_durable_run_snapshot,
)
from lib.orchestration.request_limit_contract import (
    MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    MAX_COMPOSE_HISTORY_ITEMS,
    MAX_COMPOSE_REQUIREMENT_LENGTH,
    MAX_HUMAN_INPUT_LENGTH,
    MAX_RUN_INPUT_LENGTH,
    request_limits_contract,
)
from lib.orchestration.runtime_wire_contracts import (
    RUNTIME_START_KINDS,
    project_runtime_start,
    runtime_start_contract,
)
from lib.orchestration.wire_formats import (
    AUTHORING_CONTRACT_FORMAT,
    DEFINITION_ENTRY_FORMAT,
    DEFINITION_LIST_FORMAT,
    DEFINITION_WRITE_FORMAT,
    INSPECTION_FORMAT,
    RUNTIME_START_FORMAT,
    DURABLE_RUN_FORMAT,
)


__all__ = [
    'AUTHORING_CONTRACT_FORMAT', 'INSPECTION_FORMAT',
    'DEFINITION_WRITE_FORMAT', 'DEFINITION_LIST_FORMAT',
    'DEFINITION_ENTRY_FORMAT', 'RUNTIME_START_FORMAT', 'DURABLE_RUN_FORMAT',
    'RUNTIME_START_KINDS', 'MAX_DEFINITION_VERSION', 'MAX_NAME_LEN',
    'MAX_NODES', 'MAX_SUBFLOW_DEPTH',
    'MAX_COMPOSE_REQUIREMENT_LENGTH', 'MAX_COMPOSE_HISTORY_ITEMS',
    'MAX_COMPOSE_HISTORY_CONTENT_LENGTH',
    'MAX_RUN_INPUT_LENGTH', 'MAX_HUMAN_INPUT_LENGTH',
    'definition_candidate_schema', 'definition_request_schema',
    'definition_entry_summary',
    'definition_list_contract', 'definition_entry_contract',
    'definition_write_contract', 'inspection_response_fields',
    'definition_write_version_token',
    'runtime_start_contract', 'request_limits_contract',
    'durable_run_contract', 'project_durable_run_snapshot',
    'project_runtime_start',
    'project_definition_list', 'project_definition_entry',
    'parse_definition_write_precondition', 'definition_write_conflict',
]

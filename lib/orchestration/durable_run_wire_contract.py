"""Compatibility facade for durable-run registry and wire schemas."""

from lib.orchestration.durable_run_field_registry import (
    DURABLE_RUN_DETAIL_FIELDS,
    DURABLE_RUN_FORMAT,
    DURABLE_RUN_HEADER_FIELDS,
    DURABLE_RUN_LIST_PAGE_FIELDS,
    DURABLE_RUN_OPTIONAL_FIELDS,
    durable_run_contract,
    durable_run_list_envelope_contract,
    project_durable_run_snapshot,
)
from lib.orchestration.durable_run_wire_schema import (
    durable_replay_response_schema,
    durable_run_contract_schema,
    durable_run_list_response_schema,
    durable_run_read_response_schema,
    durable_run_schema,
)


__all__ = [
    'DURABLE_RUN_FORMAT', 'DURABLE_RUN_HEADER_FIELDS',
    'DURABLE_RUN_DETAIL_FIELDS', 'DURABLE_RUN_OPTIONAL_FIELDS',
    'DURABLE_RUN_LIST_PAGE_FIELDS', 'durable_run_list_envelope_contract',
    'durable_run_contract', 'durable_run_contract_schema',
    'durable_run_schema', 'durable_run_list_response_schema',
    'durable_run_read_response_schema', 'durable_replay_response_schema',
    'project_durable_run_snapshot',
]

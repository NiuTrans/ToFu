"""Executable field registry and snapshot projection for durable runs."""

from __future__ import annotations

from lib.orchestration.wire_formats import DURABLE_RUN_FORMAT


DURABLE_RUN_HEADER_FIELDS = (
    'id', 'orch_id', 'name', 'status', 'terminal', 'final', 'error',
    'created_by', 'created_at', 'updated_at', 'finished_at',
)
DURABLE_RUN_DETAIL_FIELDS = (*DURABLE_RUN_HEADER_FIELDS, 'definition', 'input')
DURABLE_RUN_OPTIONAL_FIELDS = ('outcome',)
DURABLE_RUN_LIST_PAGE_FIELDS = ('limit', 'has_more', 'next_limit')


def durable_run_list_envelope_contract() -> dict:
    """Return collection field names and bounded progressive-load policy."""
    return {
        'itemsField': 'runs',
        'pageField': 'page',
        'pageFields': list(DURABLE_RUN_LIST_PAGE_FIELDS),
        'limitField': 'limit',
        'hasMoreField': 'has_more',
        'nextLimitField': 'next_limit',
        'defaultLimit': 50,
        'pageStep': 50,
        'maxLimit': 150,
    }


def project_durable_run_snapshot(
    values: dict,
    *,
    detail: bool,
) -> dict:
    """Project storage values through the published required field set."""
    fields = DURABLE_RUN_DETAIL_FIELDS if detail else DURABLE_RUN_HEADER_FIELDS
    return {field: values[field] for field in fields}


def durable_run_contract() -> dict:
    """Return the detached run-snapshot contract shared across transports."""
    return {
        'format': DURABLE_RUN_FORMAT,
        'idField': 'id',
        'statusField': 'status',
        'terminalField': 'terminal',
        'outcomeField': 'outcome',
        'listFields': list(DURABLE_RUN_HEADER_FIELDS),
        'readFields': list(DURABLE_RUN_DETAIL_FIELDS),
        'optionalFields': list(DURABLE_RUN_OPTIONAL_FIELDS),
        'listEnvelope': durable_run_list_envelope_contract(),
    }


__all__ = [
    'DURABLE_RUN_FORMAT', 'DURABLE_RUN_HEADER_FIELDS',
    'DURABLE_RUN_DETAIL_FIELDS', 'DURABLE_RUN_OPTIONAL_FIELDS',
    'DURABLE_RUN_LIST_PAGE_FIELDS', 'durable_run_list_envelope_contract',
    'durable_run_contract', 'project_durable_run_snapshot',
]

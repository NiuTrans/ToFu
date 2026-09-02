"""Versioned identity contract for ephemeral and durable runtime starts."""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)
from lib.orchestration.wire_formats import RUNTIME_START_FORMAT


RUNTIME_START_KINDS = ('ephemeral', 'durable')


def runtime_start_contract() -> dict:
    """Describe the shared identity envelope returned by both start modes."""
    return {
        'format': RUNTIME_START_FORMAT,
        'kinds': list(RUNTIME_START_KINDS),
        'idField': 'id',
        'kindField': 'kind',
        'successStatuses': {
            'ephemeral': 200,
            'durable': 201,
        },
    }


def runtime_start_contract_schema() -> dict:
    """Describe runtime identity policy from its live contract."""
    return contract_snapshot_schema(runtime_start_contract())


def runtime_start_identity_schema(kind: str) -> dict:
    """Describe one canonical ephemeral/durable start identity."""
    contract = runtime_start_contract()
    if kind not in contract['kinds']:
        raise ValueError(f'unknown runtime start kind {kind!r}')
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': [
            contract['idField'], contract['kindField'], 'format',
        ],
        'properties': {
            'format': {'type': 'string', 'enum': [contract['format']]},
            contract['kindField']: {'type': 'string', 'enum': [kind]},
            contract['idField']: {'type': 'string', 'minLength': 1},
        },
    }


def run_start_response_schema(kind: str) -> dict:
    """Describe one start response from the shared runtime identity contract."""
    contract = runtime_start_contract()
    if kind not in contract['kinds']:
        raise ValueError(f'unknown runtime start kind {kind!r}')
    return {
        'type': 'object',
        'required': [
            'ok', 'start', 'definitionSource', 'inspection',
            'warnings', 'contract',
        ],
        'properties': {
            'ok': {'type': 'boolean', 'const': True},
            'request_id': {'type': 'string'},
            'start': runtime_start_identity_schema(kind),
            'definitionSource': {'type': 'string'},
            'inspection': inspection_response_schema(),
            'warnings': {
                'type': 'array', 'items': {'type': 'string'},
            },
            'contract': {'type': ['object', 'null']},
        },
    }


def project_runtime_start(runtime_id: str, kind: str) -> dict:
    """Build the one versioned start identity shared by both HTTP routes."""
    canonical_id = str(runtime_id or '')
    canonical_kind = str(kind or '')
    if not canonical_id:
        raise ValueError('runtime start id is required')
    if canonical_kind not in RUNTIME_START_KINDS:
        raise ValueError(f'unknown runtime start kind {canonical_kind!r}')
    return {
        'format': RUNTIME_START_FORMAT,
        'kind': canonical_kind,
        'id': canonical_id,
    }


__all__ = [
    'RUNTIME_START_FORMAT',
    'RUNTIME_START_KINDS',
    'runtime_start_contract', 'runtime_start_contract_schema',
    'runtime_start_identity_schema',
    'run_start_response_schema',
    'project_runtime_start',
]

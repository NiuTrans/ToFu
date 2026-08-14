"""Detached inspection response projection shared by all HTTP adapters."""

from __future__ import annotations

import copy

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.wire_formats import INSPECTION_FORMAT


def inspection_contract() -> dict:
    """Return the versioned diagnostic vocabulary shared with Studio."""
    return {
        'format': INSPECTION_FORMAT,
        'responseFields': [
            'format', 'ok', 'errors', 'warnings', 'diagnostics', 'contract',
        ],
        'responseStringArrayFields': ['errors', 'warnings'],
        'diagnosticSeverities': ['error', 'warning'],
        'diagnosticFields': ['severity', 'code', 'path', 'message'],
        'diagnosticStringFields': ['code', 'path', 'message'],
        'diagnosticPathFormat': 'json-pointer',
        'contractFields': [
            'schema', 'projection', 'initialPhase', 'nodes', 'edges',
        ],
        'contractStringFields': ['schema', 'projection', 'initialPhase'],
        'contractNonNegativeIntegerFields': ['nodes', 'edges'],
    }


def inspection_response_schema() -> dict:
    """Describe the canonical inspection object from its live vocabulary."""
    contract = inspection_contract()
    field_schemas = {
        'severity': {
            'type': 'string', 'enum': contract['diagnosticSeverities'],
        },
        'code': {'type': 'string'},
        'path': {
            'type': 'string',
            'description': contract['diagnosticPathFormat'],
        },
        'message': {'type': 'string'},
    }
    diagnostic_fields = list(contract['diagnosticFields'])
    contract_fields = list(contract['contractFields'])
    return {
        'type': 'object',
        'required': list(contract['responseFields']),
        'properties': {
            'request_id': {'type': 'string'},
            'format': {'type': 'string', 'enum': [contract['format']]},
            'ok': {'type': 'boolean'},
            'errors': {'type': 'array', 'items': {'type': 'string'}},
            'warnings': {'type': 'array', 'items': {'type': 'string'}},
            'diagnostics': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'required': diagnostic_fields,
                    'properties': {
                        field: field_schemas.get(field, {})
                        for field in diagnostic_fields
                    },
                },
            },
            'contract': {
                'type': 'object',
                'required': contract_fields,
                'properties': {
                    'schema': {'type': 'string'},
                    'projection': {'type': 'string'},
                    'initialPhase': {'type': 'string'},
                    'nodes': {'type': 'integer', 'minimum': 0},
                    'edges': {'type': 'integer', 'minimum': 0},
                },
            },
        },
    }


def inspection_contract_schema() -> dict:
    """Describe inspection metadata from the executable vocabulary."""
    return contract_snapshot_schema(inspection_contract())


def inspection_response_fields(
    inspection: dict,
    *,
    include_errors: bool = False,
) -> dict:
    """Project one inspection into canonical plus rolling response fields."""
    canonical = copy.deepcopy(inspection or {})
    fields = {
        'inspection': canonical,
        'warnings': list(canonical.get('warnings') or []),
        'contract': copy.deepcopy(canonical.get('contract')),
    }
    if include_errors:
        fields['errors'] = list(canonical.get('errors') or [])
    return fields


__all__ = [
    'inspection_contract', 'inspection_response_schema',
    'inspection_contract_schema',
    'inspection_response_fields',
]

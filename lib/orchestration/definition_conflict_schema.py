"""JSON Schema projection for definition CAS conflict responses."""

from __future__ import annotations

from lib.orchestration.definition_contract_registry import (
    definition_write_contract,
)
from lib.orchestration.definition_version import definition_version_schema


def definition_conflict_response_schema() -> dict:
    """Describe stale writes from the same contract as runtime payloads."""
    contract = definition_write_contract()
    field_specs = contract['conflictFields']
    fields = {
        semantic: spec['name']
        for semantic, spec in field_specs.items()
    }
    write_properties = {
        spec['name']: (
            definition_version_schema(nullable=True)
            if spec['type'] == 'nullable_non_negative_integer'
            else {'type': spec['type']}
        )
        for spec in field_specs.values()
    }
    write_properties[fields['format']]['enum'] = [contract['format']]
    write_properties[fields['reason']]['enum'] = [
        contract['conflictReason']]
    write_properties[fields['operation']]['enum'] = contract['operations']
    return {
        'type': 'object',
        'required': [
            'ok', 'error', 'conflict', 'write', fields['currentUpdatedAt'],
        ],
        'properties': {
            'ok': {'type': 'boolean', 'const': False},
            'error': {'type': 'string'},
            'request_id': {'type': 'string'},
            'conflict': {
                'type': 'string', 'enum': [contract['conflictReason']],
            },
            fields['currentUpdatedAt']: definition_version_schema(
                nullable=True),
            'write': {
                'type': 'object',
                'additionalProperties': False,
                'required': [spec['name'] for spec in field_specs.values()],
                'properties': write_properties,
            },
        },
    }


__all__ = ['definition_conflict_response_schema']

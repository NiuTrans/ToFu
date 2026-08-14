"""Framework-neutral response schemas for Studio authoring actions."""

from __future__ import annotations

from lib.orchestration.definition_wire_contracts import (
    definition_layout_schema,
    definition_request_schema,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)


def _request_metadata_properties() -> dict:
    return {'request_id': {'type': 'string'}}


def definition_action_response_schema(
    *,
    inspection: bool = False,
    definition_source: bool = False,
    layout: bool = False,
) -> dict:
    """Describe built-in/layout responses through one definition projection."""
    required = ['ok', 'definition']
    properties = {
        **_request_metadata_properties(),
        'ok': {'type': 'boolean', 'const': True},
        'definition': definition_layout_schema()
        if layout else definition_request_schema(),
    }
    if inspection:
        required.append('inspection')
        properties['inspection'] = inspection_response_schema()
    if definition_source:
        required.append('definitionSource')
        properties['definitionSource'] = {'type': 'string'}
    return {
        'type': 'object', 'required': required, 'properties': properties,
    }


def compose_response_schema() -> dict:
    """Describe Composer transport success and logical rejection together."""
    return {
        'type': 'object',
        'required': ['ok', 'reply', 'definition', 'validation', 'error'],
        'properties': {
            **_request_metadata_properties(),
            'ok': {'type': 'boolean'},
            'reply': {'type': 'string'},
            'definition': {
                'oneOf': [definition_request_schema(), {'type': 'null'}],
            },
            'inspection': {
                'oneOf': [inspection_response_schema(), {'type': 'null'}],
            },
            'validation': {'type': ['object', 'null']},
            'error': {'type': ['string', 'null']},
        },
        'allOf': [{
            'if': {
                'properties': {'ok': {'const': True}},
                'required': ['ok'],
            },
            'then': {'properties': {
                'definition': definition_request_schema(),
            }},
        }],
    }


def plan_response_schema() -> dict:
    """Describe dry-run plans plus their canonical inspection evidence."""
    return {
        'type': 'object',
        'required': [
            'ok', 'steps', 'error', 'inspection', 'warnings', 'contract',
            'definitionSource',
        ],
        'properties': {
            **_request_metadata_properties(),
            'ok': {'type': 'boolean'},
            'steps': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'required': ['node_id', 'action'],
                    'properties': {
                        'node_id': {'type': 'string'},
                        'action': {'type': 'string'},
                        'role': {'type': ['string', 'null']},
                        'kind': {'type': ['string', 'null']},
                        'scope': {'type': 'string'},
                        'path': {'type': 'string'},
                    },
                },
            },
            'error': {'type': ['string', 'null']},
            'inspection': inspection_response_schema(),
            'warnings': {'type': 'array', 'items': {'type': 'string'}},
            'contract': {'type': ['object', 'null']},
            'definitionSource': {'type': 'string'},
        },
    }


__all__ = [
    'definition_action_response_schema', 'compose_response_schema',
    'plan_response_schema',
]

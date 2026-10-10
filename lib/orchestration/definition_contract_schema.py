"""JSON Schema projections for stored-definition wire contracts."""

from __future__ import annotations

import copy

from lib.orchestration._control_specs import CONTROL_KINDS
from lib.orchestration._definition_contract import (
    MAX_NAME_LEN,
    MAX_NODES,
    NODE_TYPE_ORDER,
    SCHEMA_ID,
)
from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.definition_contract_registry import (
    definition_entry_contract,
    definition_list_contract,
    definition_write_contract,
)
from lib.orchestration.definition_version import definition_version_schema
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)


_DEFINITION_REQUEST_SCHEMA = {
    'type': 'object',
    'properties': {
        # Unknown revisions receive a validator warning, so this is a default
        # rather than a false ``enum`` restriction.
        'schema': {'type': 'string', 'default': SCHEMA_ID},
        'name': {
            'type': 'string', 'minLength': 1, 'maxLength': MAX_NAME_LEN,
        },
        'nodes': {
            'type': 'array',
            'maxItems': MAX_NODES,
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'minLength': 1},
                    'type': {'type': 'string', 'enum': list(NODE_TYPE_ORDER)},
                    'role': {'type': 'string'},
                    'kind': {
                        'type': 'string', 'enum': list(CONTROL_KINDS),
                    },
                    'name': {'type': 'string'},
                    'pos': {
                        'type': 'object',
                        'properties': {
                            'x': {'type': 'number'},
                            'y': {'type': 'number'},
                        },
                    },
                    'params': {'type': 'object'},
                },
                'required': ['id', 'type'],
            },
        },
        'edges': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'from': {'type': 'string', 'minLength': 1},
                    'to': {'type': 'string', 'minLength': 1},
                    'label': {'type': 'string'},
                    'condition': {'type': 'string'},
                },
                'required': ['from', 'to'],
            },
        },
    },
    'required': ['name', 'nodes', 'edges'],
}


def definition_request_schema() -> dict:
    """Return the detached JSON Schema advertised by authoring endpoints."""
    return copy.deepcopy(_DEFINITION_REQUEST_SCHEMA)


def definition_layout_schema() -> dict:
    """Describe the complete finite-coordinate definition returned by layout."""
    schema = definition_request_schema()
    node = schema['properties']['nodes']['items']
    node['required'].append('pos')
    node['properties']['pos']['required'] = ['x', 'y']
    return schema


def definition_candidate_schema() -> dict:
    """Describe any object the diagnostic endpoint can inspect."""
    return {
        'type': 'object',
        'additionalProperties': True,
        'description': 'Possibly incomplete orchestration draft to inspect.',
    }


def definition_list_contract_schema() -> dict:
    return contract_snapshot_schema(definition_list_contract())


def definition_entry_contract_schema() -> dict:
    return contract_snapshot_schema(definition_entry_contract())


def definition_write_contract_schema() -> dict:
    return contract_snapshot_schema(definition_write_contract())


def _definition_success_properties() -> dict:
    return {
        'ok': {'type': 'boolean', 'const': True},
        'request_id': {'type': 'string'},
    }


def definition_list_response_schema() -> dict:
    """Describe the stored-definition collection envelope."""
    contract = definition_list_contract()
    field_schemas = {
        'id': {'type': 'string', 'minLength': 1},
        'name': {'type': 'string'},
        'nodeCount': {'type': 'integer', 'minimum': 0},
        'createdAt': definition_version_schema(nullable=True),
        'updatedAt': definition_version_schema(nullable=True),
    }
    item_fields = list(contract['itemFields'])
    return {
        'type': 'object',
        'required': ['ok', 'format', 'items'],
        'properties': {
            **_definition_success_properties(),
            'format': {'type': 'string', 'enum': [contract['format']]},
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'required': item_fields,
                    'properties': {
                        field: field_schemas.get(field, {})
                        for field in item_fields
                    },
                },
            },
        },
    }


def definition_entry_response_schema(*, written: bool = False) -> dict:
    """Describe stored reads and inspected create/replace responses."""
    contract = definition_entry_contract()
    field_schemas = {
        'id': {'type': 'string', 'minLength': 1},
        'name': {'type': 'string', 'minLength': 1},
        'definition': definition_request_schema(),
        'createdAt': definition_version_schema(),
        'updatedAt': definition_version_schema(),
    }
    entry_fields = list(contract['fields'])
    properties = {
        **_definition_success_properties(),
        'format': {'type': 'string', 'enum': [contract['format']]},
        **{
            field: field_schemas.get(field, {})
            for field in entry_fields
        },
    }
    required = ['ok', 'format', *entry_fields]
    if written and contract['inspectionIncludedOnWrite']:
        properties.update({
            'inspection': inspection_response_schema(),
            'warnings': {'type': 'array', 'items': {'type': 'string'}},
            'contract': {'type': ['object', 'null']},
        })
        required.extend(['inspection', 'warnings', 'contract'])
    return {
        'type': 'object',
        'required': required,
        'properties': properties,
    }


def definition_delete_response_schema() -> dict:
    """Describe the stable success envelope for a stored delete."""
    return {
        'type': 'object',
        'required': ['ok'],
        'properties': _definition_success_properties(),
    }


__all__ = [
    'definition_candidate_schema',
    'definition_delete_response_schema',
    'definition_entry_contract_schema',
    'definition_entry_response_schema',
    'definition_list_contract_schema',
    'definition_list_response_schema',
    'definition_request_schema',
    'definition_write_contract_schema',
]

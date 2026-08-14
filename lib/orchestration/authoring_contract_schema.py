"""OpenAPI schema projection for the backend-owned authoring contract."""

from __future__ import annotations

import copy
from collections.abc import Callable

from lib.orchestration._control_specs import CONTROL_KINDS, CONTROL_PARAM_SCHEMA
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration.field_spec_contract import (
    field_spec_list_schema,
    field_spec_registry_schema,
    field_spec_schema,
)
from lib.orchestration.field_values import field_value_contract_schema
from lib.orchestration.io_contract import (
    io_contract_document_schema,
    io_contract_schema,
)
from lib.orchestration._role_axes import EXECUTION_OPTION_ORDER, KNOWN_ROLES
from lib.orchestration._role_specs import VALID_PARAM_KINDS
from lib.orchestration.authoring_builtin_registry import builtin_names
from lib.orchestration.authoring_contract_registry import (
    AUTHORING_OBJECT_SECTION_NAMES,
    contract_section_registry_schema,
)
from lib.orchestration.authoring_contract_sections import (
    default_emits_contract,
    execution_options_contract,
    node_authoring_defaults,
    persona_registry,
    role_schema_registry,
)
from lib.orchestration._runtime_params import node_runtime_defaults
from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.definition_wire_contracts import (
    definition_entry_contract_schema,
    definition_list_contract_schema,
    definition_write_contract_schema,
)
from lib.orchestration.durable_run_wire_schema import (
    durable_run_contract_schema,
)
from lib.orchestration.events import runtime_event_contract_schema
from lib.orchestration.inspection_wire_contract import inspection_contract_schema
from lib.orchestration.mutation_contract import mutation_contract_schema
from lib.orchestration.outcome_contract import outcome_contract_schema
from lib.orchestration.request_limit_contract import request_limits_contract_schema
from lib.orchestration.run_status import run_status_contract_schema
from lib.orchestration.runtime_wire_contracts import runtime_start_contract_schema
from lib.orchestration.wire_formats import AUTHORING_CONTRACT_FORMAT
from lib.orchestration_trace import trace_contract_schema
from lib.task_replay import task_replay_contract_schema


def _persona_registry_schema() -> dict:
    personas = persona_registry()
    persona_schema = {
        'type': 'object',
        'required': ['prompt', 'whenToUse', 'tier'],
        'additionalProperties': False,
        'properties': {
            'prompt': {'type': 'string'},
            'whenToUse': {'type': 'string'},
            'tier': {'type': 'string'},
        },
    }
    return {
        'type': 'object',
        'required': list(personas),
        'additionalProperties': False,
        'properties': {
            name: copy.deepcopy(persona_schema)
            for name in personas
        },
    }


def _default_emits_registry_schema() -> dict:
    values = default_emits_contract()
    return {
        'type': 'object',
        'required': list(values),
        'additionalProperties': False,
        'properties': {
            name: {
                'type': 'string',
                'enum': list(EXECUTION_OPTION_ORDER['emits']),
            }
            for name in values
        },
    }


def node_authoring_defaults_schema() -> dict:
    """Describe the exact backend-owned defaults used for new nodes."""
    defaults = node_authoring_defaults()
    schema = contract_snapshot_schema(defaults)
    blank_schema = schema['properties']['blankSubflow']['properties']
    for name in ('nodes', 'edges'):
        values = defaults['blankSubflow'][name]
        blank_schema[name]['items'] = {
            'oneOf': [contract_snapshot_schema(value) for value in values],
        }
    return schema


_AUTHORING_SECTION_SCHEMA_BUILDERS: dict[str, Callable[[], dict]] = {
    'roles': lambda: field_spec_registry_schema(role_schema_registry()),
    'controlSchemas': lambda: field_spec_registry_schema(
        CONTROL_PARAM_SCHEMA),
    'personas': _persona_registry_schema,
    'defaultEmits': _default_emits_registry_schema,
    'executionOptions': lambda: contract_snapshot_schema(
        execution_options_contract()),
    'nodeDefaults': node_authoring_defaults_schema,
    'nodeRuntimeDefaults': lambda: contract_snapshot_schema(
        node_runtime_defaults()),
    'requestLimits': request_limits_contract_schema,
    'eventContract': runtime_event_contract_schema,
    'runContract': run_status_contract_schema,
    'outcomeContract': outcome_contract_schema,
    'traceContract': trace_contract_schema,
    'mutationContract': mutation_contract_schema,
    'replayContract': task_replay_contract_schema,
    'runtimeStartContract': runtime_start_contract_schema,
    'durableRunContract': durable_run_contract_schema,
    'inspectionContract': inspection_contract_schema,
    'definitionListContract': definition_list_contract_schema,
    'definitionEntryContract': definition_entry_contract_schema,
    'fieldValueContract': field_value_contract_schema,
    'definitionWriteContract': definition_write_contract_schema,
    'ioContract': io_contract_document_schema,
}


def authoring_object_section_schemas() -> dict[str, dict]:
    """Return detached OpenAPI schemas for every object-policy section."""
    unknown = set(_AUTHORING_SECTION_SCHEMA_BUILDERS).difference(
        AUTHORING_OBJECT_SECTION_NAMES)
    if unknown:
        raise RuntimeError(
            'authoring section-schema registry contains unknown sections: '
            + ', '.join(sorted(unknown)))
    return {
        name: copy.deepcopy(_AUTHORING_SECTION_SCHEMA_BUILDERS[name]())
        for name in AUTHORING_OBJECT_SECTION_NAMES
    }


def authoring_contract_response_schema() -> dict:
    """Describe the capability response from its live section registries."""
    authoring_names = list(AUTHORING_OBJECT_SECTION_NAMES)
    io_contract = io_contract_schema()
    properties = authoring_object_section_schemas()
    properties.update({
        'ok': {'type': 'boolean', 'const': True},
        'request_id': {'type': 'string'},
        'format': {
            'type': 'string', 'enum': [AUTHORING_CONTRACT_FORMAT],
        },
        'schema': {'type': 'string', 'enum': [SCHEMA_ID]},
        'roleNames': contract_snapshot_schema(sorted(KNOWN_ROLES)),
        'generic': field_spec_list_schema(),
        'kinds': contract_snapshot_schema(sorted(VALID_PARAM_KINDS)),
        'controls': contract_snapshot_schema(CONTROL_KINDS),
        'builtins': contract_snapshot_schema(list(builtin_names())),
        'ioTypes': contract_snapshot_schema(list(io_contract['types'])),
        'defaultOutput': contract_snapshot_schema(
            io_contract['defaultOutput']['name']),
        'contractSections': contract_section_registry_schema(),
    })
    return {
        'type': 'object',
        'required': [
            'ok', 'format', 'schema', 'roleNames', 'generic', 'controls',
            'kinds', 'builtins', 'contractSections', 'ioTypes',
            'defaultOutput', *authoring_names,
        ],
        'properties': properties,
    }


def role_contract_response_schema() -> dict:
    """Describe the compatibility endpoint's full or single-role result."""
    role_schema = {
        'type': 'object',
        'required': ['ok', 'role', 'fields', 'persona'],
        'properties': {
            'ok': {'type': 'boolean', 'const': True},
            'request_id': {'type': 'string'},
            'role': {'type': 'string'},
            'fields': {
                'type': 'array',
                'items': field_spec_schema(),
            },
            'persona': {
                'type': 'object',
                'required': ['prompt', 'whenToUse', 'tier'],
                'properties': {
                    'prompt': {'type': 'string'},
                    'whenToUse': {'type': 'string'},
                    'tier': {'type': 'string'},
                },
            },
        },
    }
    return {'oneOf': [authoring_contract_response_schema(), role_schema]}


__all__ = [
    'authoring_contract_response_schema', 'authoring_object_section_schemas',
    'node_authoring_defaults_schema', 'role_contract_response_schema',
]

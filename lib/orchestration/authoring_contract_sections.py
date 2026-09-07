"""Backend-owned values for Orchestration Studio contract sections."""

from __future__ import annotations

import copy

from lib.orchestration._control_specs import CONTROL_PARAM_SCHEMA
from lib.orchestration._defaults import (
    all_control_node_params,
    node_authoring_params,
)
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration.field_values import field_value_contract
from lib.orchestration.io_contract import io_contract_schema
from lib.orchestration._role_axes import (
    DEFAULT_ROLE_TIER,
    EXECUTION_OPTION_ORDER,
    KNOWN_ROLES,
    resolve_emits,
)
from lib.orchestration._role_personas import role_persona
from lib.orchestration._role_specs import role_param_schema
from lib.orchestration._runtime_params import node_runtime_defaults
from lib.orchestration.authoring_contract_registry import (
    AUTHORING_OBJECT_SECTION_NAMES,
)
from lib.orchestration.definition_contract_registry import (
    definition_entry_contract,
    definition_list_contract,
    definition_write_contract,
)
from lib.orchestration.durable_run_field_registry import durable_run_contract
from lib.orchestration.events import runtime_event_contract
from lib.orchestration.inspection_wire_contract import inspection_contract
from lib.orchestration.mutation_contract import mutation_contract
from lib.orchestration.outcome_contract import outcome_contract
from lib.orchestration.request_limit_contract import request_limits_contract
from lib.orchestration.run_status import run_status_contract
from lib.orchestration.runtime_wire_contracts import runtime_start_contract
from lib.orchestration_trace import trace_contract
from lib.task_replay import task_replay_contract


def execution_options_contract() -> dict:
    return {
        axis: list(values)
        for axis, values in EXECUTION_OPTION_ORDER.items()
    }


def default_emits_contract() -> dict:
    return {
        role_name: resolve_emits({'role': role_name})
        for role_name in sorted(KNOWN_ROLES)
    }


def role_schema_registry() -> dict:
    """Project detached FieldSpecs for every advertised role."""
    return {
        role_name: copy.deepcopy(role_param_schema(role_name))
        for role_name in sorted(KNOWN_ROLES)
    }


def persona_registry() -> dict:
    """Project detached runtime personas for every advertised role."""
    return {
        role_name: copy.deepcopy(role_persona(role_name))
        for role_name in sorted(KNOWN_ROLES)
    }


def node_authoring_defaults() -> dict:
    """Return canonical params for nodes newly created by authoring clients."""
    personas = role_persona()

    def role_defaults(role_name: str) -> dict:
        persona = personas.get(role_name) or role_persona(role_name)
        return node_authoring_params(
            'role', tier=persona.get('tier') or DEFAULT_ROLE_TIER)

    generic_role = role_defaults('general')
    blank_subflow = {
        'schema': SCHEMA_ID,
        'name': 'Group',
        'nodes': [
            {'id': 'gstart', 'type': 'control', 'kind': 'start',
             'params': node_authoring_params('control', kind='start')},
            {'id': 'gagent', 'type': 'role', 'role': 'general',
             'params': copy.deepcopy(generic_role)},
            {'id': 'gstop', 'type': 'control', 'kind': 'stop',
             'params': node_authoring_params('control', kind='stop')},
        ],
        'edges': [
            {'from': 'gstart', 'to': 'gagent'},
            {'from': 'gagent', 'to': 'gstop'},
        ],
    }
    return {
        'roles': {
            role_name: role_defaults(role_name)
            for role_name in sorted(KNOWN_ROLES)
        },
        'genericRole': generic_role,
        'controls': all_control_node_params(),
        'subflow': node_authoring_params('subflow'),
        'blankSubflow': blank_subflow,
    }


def authoring_object_sections() -> dict:
    """Return every detached object-policy document consumed by Studio."""
    sections = {
        'roles': role_schema_registry(),
        'controlSchemas': copy.deepcopy(CONTROL_PARAM_SCHEMA),
        'personas': persona_registry(),
        'defaultEmits': default_emits_contract(),
        'executionOptions': execution_options_contract(),
        'nodeDefaults': node_authoring_defaults(),
        'nodeRuntimeDefaults': node_runtime_defaults(),
        'eventContract': runtime_event_contract(),
        'runContract': run_status_contract(),
        'outcomeContract': outcome_contract(),
        'traceContract': trace_contract(),
        'mutationContract': mutation_contract(),
        'replayContract': task_replay_contract(),
        'inspectionContract': inspection_contract(),
        'definitionListContract': definition_list_contract(),
        'definitionEntryContract': definition_entry_contract(),
        'runtimeStartContract': runtime_start_contract(),
        'fieldValueContract': field_value_contract(),
        'durableRunContract': durable_run_contract(),
        'definitionWriteContract': definition_write_contract(),
        'requestLimits': request_limits_contract(),
        'ioContract': io_contract_schema(),
    }
    if tuple(sections) != AUTHORING_OBJECT_SECTION_NAMES:
        raise RuntimeError('authoring object-section registry is out of order')
    return sections


__all__ = [
    'authoring_object_sections', 'default_emits_contract',
    'execution_options_contract', 'node_authoring_defaults',
    'persona_registry', 'role_schema_registry',
]

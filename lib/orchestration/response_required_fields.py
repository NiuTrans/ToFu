"""Browser response-field policy derived from backend-owned wire schemas."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from lib.orchestration.authoring_action_wire_contracts import (
    compose_response_schema,
    definition_action_response_schema,
    plan_response_schema,
)
from lib.orchestration.authoring_contract_schema import (
    authoring_contract_response_schema,
)
from lib.orchestration.definition_contract_schema import (
    definition_delete_response_schema,
    definition_entry_response_schema,
    definition_list_response_schema,
)
from lib.orchestration.durable_run_wire_schema import (
    durable_replay_response_schema,
    durable_run_list_response_schema,
    durable_run_read_response_schema,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)
from lib.orchestration.mutation_contract import mutation_response_schema
from lib.orchestration.mutation_endpoint_contract import (
    mutation_endpoint_contracts,
)
from lib.orchestration.runtime_wire_contracts import run_start_response_schema
from lib.task_replay import live_task_replay_response_schema


def _mutation_required_fields() -> tuple[str, ...]:
    """Return fields shared by every documented mutation success shape."""
    schemas = []
    for config in mutation_endpoint_contracts().values():
        for reasons in config['outcomes']:
            schemas.append(mutation_response_schema(
                config['action'], reasons))
    first = tuple(schemas[0]['required'])
    required_sets = [set(schema['required']) for schema in schemas[1:]]
    return tuple(field for field in first if all(
        field in required for required in required_sets))


ORCHESTRATION_RESPONSE_REQUIRED_FIELDS: Mapping[
    str, tuple[str, ...],
] = MappingProxyType({
    'definition-list': tuple(definition_list_response_schema()['required']),
    'definition-read': tuple(definition_entry_response_schema()['required']),
    'definition-save': tuple(definition_entry_response_schema(
        written=True)['required']),
    'definition-delete': tuple(
        definition_delete_response_schema()['required']),
    'validation': tuple(inspection_response_schema()['required']),
    'compose': tuple(compose_response_schema()['required']),
    'builtin': tuple(definition_action_response_schema(
        inspection=True)['required']),
    'layout': tuple(definition_action_response_schema(
        definition_source=True, layout=True)['required']),
    'authoring-contract': tuple(
        authoring_contract_response_schema()['required']),
    'plan': tuple(plan_response_schema()['required']),
    'run-start': tuple(run_start_response_schema('ephemeral')['required']),
    'run-poll': tuple(live_task_replay_response_schema()['required']),
    'mutation': _mutation_required_fields(),
    'task-list': tuple(durable_run_list_response_schema()['required']),
    'task-read': tuple(durable_run_read_response_schema()['required']),
    'task-create': tuple(run_start_response_schema('durable')['required']),
    'task-events': tuple(durable_replay_response_schema()['required']),
})


__all__ = ['ORCHESTRATION_RESPONSE_REQUIRED_FIELDS']

"""HTTP projections shared by repository-free authoring endpoints."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.api_response import (
    api_bad_request,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.orchestration.application_result_ports import AuthoringBuiltinResultPort
from lib.orchestration.compose_request_contract import (
    MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    MAX_COMPOSE_HISTORY_ITEMS,
    MAX_COMPOSE_REQUIREMENT_LENGTH,
    compose_request_contract,
    compose_request_schema,
)
from lib.orchestration.http_endpoint_contract import (
    orchestration_http_endpoint,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.request_limit_contract import (
    normalize_compose_history,
)
from lib.request_parser import (
    optional_dict,
    optional_list,
    optional_str,
    query_str,
)
from .orchestration_request_http import OrchestrationHttpPreparation
_SOURCE_UNSET = object()
_COMPOSE = compose_request_contract()
(_ROLE_QUERY,) = orchestration_http_endpoint('role-schema').query_fields


def role_contract_parameters() -> list[dict]:
    return [{
        'name': _ROLE_QUERY,
        'in': 'query',
        'schema': {'type': 'string'},
        'description': 'Optional role name; omit for the full contract.',
    }]


def role_contract_query(args: Mapping[str, Any]) -> str:
    return query_str(args, _ROLE_QUERY)


@dataclass(frozen=True)
class PreparedComposeRequest:
    """Typed Composer inputs after HTTP body normalization."""

    requirement: str
    current: dict | None
    history: list | None


def prepare_compose_request(
    body: dict,
) -> OrchestrationHttpPreparation[PreparedComposeRequest]:
    """Parse Composer body fields and project its canonical required error."""
    requirement_field = _COMPOSE['requirementField']
    current_field = _COMPOSE['currentField']
    history_field = _COMPOSE['historyField']
    requirement = optional_str(
        body, requirement_field, default='',
        max_len=_COMPOSE['requirementMaxLength'],
    )
    if not requirement:
        return OrchestrationHttpPreparation.reject(api_bad_request(
            f'{requirement_field} is required', field=requirement_field,
        ))
    current = optional_dict(body, current_field) \
        if body.get(current_field) is not None else None
    history = optional_list(body, history_field, item_type=dict) \
        if body.get(history_field) is not None else None
    if history is not None:
        history = normalize_compose_history(history)
    return OrchestrationHttpPreparation.accept(PreparedComposeRequest(
        requirement=requirement,
        current=current,
        history=history,
    ))


def authoring_definition_response(
    definition: dict,
    *,
    inspection: dict | None = None,
    definition_source=_SOURCE_UNSET,
):
    """Project the one definition-action shape consumed by Studio.

    Built-ins omit ``definitionSource`` for wire compatibility; layout passes
    it explicitly, including the empty string. Optional inspection is detached
    by ``api_ok``/``jsonify`` at the framework boundary.
    """
    fields = {'definition': definition}
    if inspection is not None:
        fields['inspection'] = inspection
    if definition_source is not _SOURCE_UNSET:
        fields['definitionSource'] = str(definition_source or '')
    return api_ok(fields)


def authoring_builtin_response(
    result: AuthoringBuiltinResultPort, *, name: str,
):
    """Project both found and missing built-ins through one HTTP boundary."""
    if result.definition is None:
        return api_not_found(f'Unknown built-in flow {name!r}')
    return authoring_definition_response(
        result.definition, inspection=result.inspection)


def authoring_compose_response(result: dict):
    """Preserve Composer's logical ``ok`` inside a successful HTTP exchange."""
    return api_payload(result)


def authoring_plan_response(
    plan: dict,
    inspection: dict,
    *,
    definition_source: str,
):
    """Project one dry-run plan without mutating application-service output."""
    response = copy.deepcopy(plan)
    response.update(inspection_response_fields(inspection))
    response['definitionSource'] = str(definition_source or '')
    return api_ok(response)


__all__ = [
    'MAX_COMPOSE_REQUIREMENT_LENGTH',
    'MAX_COMPOSE_HISTORY_ITEMS',
    'MAX_COMPOSE_HISTORY_CONTENT_LENGTH',
    'PreparedComposeRequest',
    'role_contract_parameters',
    'role_contract_query',
    'compose_request_schema',
    'prepare_compose_request',
    'authoring_builtin_response',
    'authoring_definition_response',
    'authoring_compose_response',
    'authoring_plan_response',
]

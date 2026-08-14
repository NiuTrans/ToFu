"""Shared HTTP request preparation for ephemeral and durable flow starts."""

from __future__ import annotations

from dataclasses import dataclass
from lib.api_response import api_created, api_ok
from lib.orchestration.application_provider_ports import DefinitionResolver
from lib.orchestration.definition_selection_contract import (
    definition_selection_input,
    definition_selection_origin_id,
)
from lib.orchestration.definition_inspection import prepare_definition
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.runtime_wire_contracts import (
    project_runtime_start,
    runtime_start_contract,
)

from .orchestration_definition_http import invalid_definition_response
from .orchestration_definition_request_http import (
    resolve_definition_request,
)
from .orchestration_request_http import OrchestrationHttpPreparation


@dataclass(frozen=True)
class PreparedRunRequest:
    """Canonical inputs shared by Studio runs and durable Task Mode runs."""

    definition: dict
    inspection: dict
    definition_source: str
    input_text: str
    orchestration_id: str


def prepare_run_request(
    body: dict,
    *,
    resolve_definition: DefinitionResolver,
) -> OrchestrationHttpPreparation[PreparedRunRequest]:
    """Resolve, inspect and canonicalize one run-start request exactly once."""
    resolved_result = resolve_definition_request(
        body, resolve_definition=resolve_definition,
    )
    if not resolved_result.accepted:
        return OrchestrationHttpPreparation.reject(resolved_result.failure)
    resolved = resolved_result.require()
    definition = resolved.definition
    assert isinstance(definition, dict)

    prepared = prepare_definition(definition)
    inspection = prepared.inspection
    if not prepared.valid:
        return OrchestrationHttpPreparation.reject(
            invalid_definition_response(inspection))

    canonical = prepared.definition
    assert isinstance(canonical, dict)
    origin_id = definition_selection_origin_id(body)
    return OrchestrationHttpPreparation.accept(PreparedRunRequest(
        definition=canonical,
        inspection=inspection,
        definition_source=resolved.source,
        input_text=definition_selection_input(body),
        orchestration_id=resolved.stored_id or (
            origin_id if resolved.source == 'inline' else ''),
    ))


def run_start_response_fields(
    prepared: PreparedRunRequest,
    runtime_id: str,
    *,
    kind: str,
) -> dict:
    """Project one canonical start envelope plus rolling identity alias."""
    start = project_runtime_start(runtime_id, kind)
    legacy_field = runtime_start_contract()['legacyIdFields'][kind]
    return {
        legacy_field: runtime_id,
        'start': start,
        'definitionSource': prepared.definition_source,
        **inspection_response_fields(prepared.inspection),
    }


def run_start_response(
    prepared: PreparedRunRequest,
    runtime_id: str,
    *,
    kind: str,
):
    """Project the status-accurate HTTP response for either start kind."""
    fields = run_start_response_fields(prepared, runtime_id, kind=kind)
    status = runtime_start_contract()['successStatuses'][kind]
    if status == 200:
        return api_ok(**fields)
    if status == 201:
        return api_created(**fields)
    raise ValueError(f'unsupported runtime start success status {status!r}')


__all__ = [
    'PreparedRunRequest', 'prepare_run_request',
    'run_start_response', 'run_start_response_fields',
]

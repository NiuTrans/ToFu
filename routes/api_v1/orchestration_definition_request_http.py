"""Shared HTTP ingress for inline-or-stored orchestration definitions."""

from __future__ import annotations

from quart import request

from lib.api_response import api_bad_request
from lib.orchestration.application_provider_ports import DefinitionResolver
from lib.orchestration.application_result_ports import ResolvedDefinitionPort
from lib.orchestration.definition_selection_contract import (
    MAX_RUN_INPUT_LENGTH,
    definition_selection_contract,
    definition_selection_request_schema,
)
from lib.orchestration.definition_wire_contracts import (
    definition_write_contract,
    parse_definition_write_precondition,
)
from .orchestration_request_http import OrchestrationHttpPreparation
from .orchestration_service_http import orchestration_service_call


_RESOLVE_CONTEXT = 'api_v1.orchestrations.resolve_definition'
_DEFINITION_WRITE = definition_write_contract()


def definition_precondition():
    """Return one parsed definition version or its canonical 400 response."""
    header = _DEFINITION_WRITE['preconditionHeader']
    try:
        return OrchestrationHttpPreparation.accept(
            parse_definition_write_precondition(
                request.headers.get(header)),
        )
    except ValueError as error:
        return OrchestrationHttpPreparation.reject(
            api_bad_request(str(error), field=header))


def resolve_definition_request(
    body: dict,
    *,
    resolve_definition: DefinitionResolver,
) -> OrchestrationHttpPreparation[ResolvedDefinitionPort]:
    """Resolve one ``definition``/``id`` body through the canonical failure.

    Layout, plan and both run-start adapters accept the same selection shape.
    Keeping its missing-definition response here prevents those endpoints from
    drifting on accepted inputs or error copy while preserving provenance.
    """
    resolved, service_failure = orchestration_service_call(
        _RESOLVE_CONTEXT,
        lambda: resolve_definition(body),
    )
    if service_failure is not None:
        return OrchestrationHttpPreparation.reject(service_failure)
    assert resolved is not None
    if not isinstance(resolved.definition, dict):
        contract = definition_selection_contract()
        return OrchestrationHttpPreparation.reject(api_bad_request(
            f"{contract['inlineField']} or {contract['storedIdField']} "
            'is required'))
    return OrchestrationHttpPreparation.accept(resolved)


__all__ = [
    'MAX_RUN_INPUT_LENGTH',
    'definition_selection_request_schema',
    'definition_precondition',
    'resolve_definition_request',
]

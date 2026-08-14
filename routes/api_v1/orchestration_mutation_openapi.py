"""OpenAPI projection for the shared orchestration mutation protocol."""

from __future__ import annotations

from lib.orchestration.mutation_endpoint_contract import (
    mutation_endpoint_contract,
    mutation_endpoint_contracts,
)
from lib.orchestration.mutation_contract import (
    mutation_contract,
    mutation_payload_schema,
    mutation_response_schema,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_error_response,
    orchestration_json_response,
)

def _mutation_response(
    operation: str,
    status: str,
    reasons: list[str],
) -> dict:
    config = mutation_endpoint_contract(operation)
    schema = mutation_response_schema(
        config['action'], reasons, config['compatibility'])
    if status == '500':
        schema = {
            'oneOf': [
                schema,
                orchestration_error_response(500)['content'][
                    'application/json']['schema'],
            ],
        }
    return orchestration_json_response(
        'Mutation accepted' if status == '200'
        else 'Mutation rejected with canonical reconciliation metadata',
        schema,
    )


def mutation_route_responses(operation: str) -> dict:
    """Return status-accurate docs for one HTTP mutation adapter."""
    config = mutation_endpoint_contract(operation)
    status_by_reason = mutation_contract()['httpStatusByReason']
    responses = {}
    for reasons in config['outcomes']:
        statuses = {status_by_reason[reason] for reason in reasons}
        if len(statuses) != 1:
            raise ValueError(
                f'mutation outcomes span HTTP statuses: {reasons!r}')
        status = str(statuses.pop())
        responses[status] = _mutation_response(
            operation, status, reasons)
    errors = [401, 403]
    if config.get('badRequest'):
        errors.insert(0, 400)
    if '500' not in responses:
        errors.append(500)
    return orchestration_api_responses(responses, *errors)


def mutation_route_response_registry() -> dict[str, dict]:
    return {
        operation: mutation_route_responses(operation)
        for operation in mutation_endpoint_contracts()
    }


__all__ = [
    'mutation_payload_schema', 'mutation_response_schema',
    'mutation_route_responses', 'mutation_route_response_registry',
]

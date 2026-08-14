"""OpenAPI projection shared by ephemeral and durable run starts."""

from __future__ import annotations

from lib.orchestration.runtime_wire_contracts import (
    run_start_response_schema,
    runtime_start_contract,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_error_response,
    orchestration_json_response,
)


def _run_start_error_response(kind: str, contract: dict) -> dict:
    """Document the durable identity retained after worker-start failure."""
    response = orchestration_error_response(500)
    if kind != 'durable':
        return response
    error_schema = response['content']['application/json']['schema']
    legacy_id = contract['legacyIdFields'][kind]
    response['content']['application/json']['schema'] = {
        'allOf': [
            error_schema,
            {
                'type': 'object',
                'properties': {
                    legacy_id: {'type': 'string', 'minLength': 1},
                },
            },
        ],
    }
    return response


def run_start_responses(kind: str) -> dict:
    """Return response metadata with the canonical status for one run kind."""
    contract = runtime_start_contract()
    if kind not in contract['kinds']:
        raise ValueError(f'unknown runtime start kind {kind!r}')
    status = str(contract['successStatuses'][kind])
    responses = orchestration_api_responses({
        status: orchestration_json_response(
            f'{kind.capitalize()} orchestration run started',
            run_start_response_schema(kind),
        ),
    }, 400, 401, 403)
    responses['500'] = _run_start_error_response(kind, contract)
    return responses


__all__ = ['run_start_response_schema', 'run_start_responses']

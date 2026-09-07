"""OpenAPI projection of the backend-owned Studio authoring contract."""

from __future__ import annotations

from lib.orchestration.authoring_contract import (
    authoring_contract_response_schema,
)

from .orchestration_authoring_action_openapi import (
    authoring_action_response_registry,
)
from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_json_response,
)


def authoring_contract_responses() -> dict:
    """Return detached response metadata for Studio contract discovery."""
    return orchestration_api_responses({
        '200': orchestration_json_response(
            'Backend-owned Orchestration Studio contract',
            authoring_contract_response_schema(),
        ),
    }, 401, 403, 500)


def authoring_route_response_registry() -> dict[str, dict]:
    """Build every authoring-route response map behind one registration port."""
    responses = authoring_action_response_registry()
    responses.update({
        'authoring-contract': authoring_contract_responses(),
    })
    return responses


__all__ = [
    'authoring_contract_response_schema',
    'authoring_contract_responses',
    'authoring_route_response_registry',
]

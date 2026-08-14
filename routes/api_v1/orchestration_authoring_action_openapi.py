"""OpenAPI projections for repository-free Studio authoring actions."""

from __future__ import annotations

from lib.orchestration.authoring_action_wire_contracts import (
    compose_response_schema,
    definition_action_response_schema,
    plan_response_schema,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_json_response,
)


def authoring_action_responses(action: str) -> dict:
    """Return one detached response map for a Studio authoring action."""
    schemas = {
        'validation': inspection_response_schema,
        'compose': compose_response_schema,
        'builtin': lambda: definition_action_response_schema(inspection=True),
        'layout': lambda: definition_action_response_schema(
            definition_source=True, layout=True),
        'plan': plan_response_schema,
    }
    descriptions = {
        'validation': 'Definition inspection completed',
        'compose': 'Composer request completed',
        'builtin': 'Built-in orchestration definition',
        'layout': 'Definition layout completed',
        'plan': 'Dry-run plan completed',
    }
    if action not in schemas:
        raise ValueError(f'unknown authoring action {action!r}')
    success = {'200': orchestration_json_response(
        descriptions[action], schemas[action]())}
    errors = (401, 403, 404, 500) if action == 'builtin' \
        else (400, 401, 403, 500)
    return orchestration_api_responses(success, *errors)


def authoring_action_response_registry() -> dict[str, dict]:
    """Build all action metadata behind one route-registration interface."""
    return {
        action: authoring_action_responses(action)
        for action in ('validation', 'compose', 'builtin', 'layout', 'plan')
    }


__all__ = [
    'inspection_response_schema',
    'definition_action_response_schema',
    'compose_response_schema',
    'plan_response_schema',
    'authoring_action_responses',
    'authoring_action_response_registry',
]

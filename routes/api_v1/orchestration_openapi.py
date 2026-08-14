"""Shared OpenAPI response primitives for orchestration HTTP adapters."""

from __future__ import annotations

import copy


_ERROR_DESCRIPTIONS = {
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    500: 'Internal Server Error',
}


def orchestration_json_response(description: str, schema: dict) -> dict:
    """Wrap one JSON schema in the shared OpenAPI response shape."""
    return {
        'description': description,
        'content': {'application/json': {'schema': schema}},
    }


def orchestration_error_response(status: int) -> dict:
    """Describe one standard API error envelope by HTTP status."""
    if status not in _ERROR_DESCRIPTIONS:
        raise ValueError(f'unsupported orchestration error status {status!r}')
    return {
        'description': _ERROR_DESCRIPTIONS[status],
        'content': {'application/json': {
            'schema': {'$ref': '#/components/schemas/ErrorEnvelope'},
        }},
    }


def orchestration_api_responses(
    success: dict,
    *error_statuses: int,
) -> dict:
    """Merge detached success metadata with shared error responses."""
    responses = copy.deepcopy(success)
    for status in error_statuses:
        responses.setdefault(
            str(status), orchestration_error_response(status))
    return responses


__all__ = [
    'orchestration_json_response',
    'orchestration_error_response',
    'orchestration_api_responses',
]

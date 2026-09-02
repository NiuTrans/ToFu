"""OpenAPI projection of the versioned orchestration definition interface.

Runtime payloads and interface metadata both derive from the same wire
contracts.  This keeps format identifiers, field registries, version headers
and conflict semantics aligned without teaching each CRUD route its own copy.
"""

from __future__ import annotations

from lib.orchestration.definition_conflict_schema import (
    definition_conflict_response_schema,
)
from lib.orchestration.definition_contract_registry import (
    definition_write_contract,
)
from lib.orchestration.definition_contract_schema import (
    definition_delete_response_schema,
    definition_entry_response_schema,
    definition_list_response_schema,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_json_response,
)


def _json_response(description: str, schema: dict, *, etag: bool = False) \
        -> dict:
    response = orchestration_json_response(description, schema)
    if etag:
        header = definition_write_contract()['versionResponseHeader']
        response['headers'] = {
            header: {
                'description': 'Quoted updatedAt version accepted by If-Match',
                'schema': {'type': 'string', 'pattern': r'^"[0-9]+"$'},
            },
        }
    return response


def definition_precondition_parameters() -> list[dict]:
    """Publish the required optimistic-concurrency header once."""
    contract = definition_write_contract()
    return [{
        'name': contract['preconditionHeader'],
        'in': 'header',
        'required': True,
        'description': 'Required quoted updatedAt token from the response ETag.',
        'schema': {'type': 'string', 'pattern': r'^(?:W/)?"[0-9]+"$'},
    }]


def definition_route_responses(operation: str) -> dict:
    """Return one detached response map for a definition CRUD operation."""
    if operation == 'list':
        return orchestration_api_responses({
            '200': _json_response(
                'Stored orchestration definitions',
                definition_list_response_schema(),
            ),
        }, 401, 403, 500)
    if operation == 'read':
        return orchestration_api_responses({
            '200': _json_response(
                'Stored orchestration definition',
                definition_entry_response_schema(), etag=True,
            ),
        }, 401, 403, 404, 500)
    if operation in {'create', 'replace'}:
        status = '201' if operation == 'create' else '200'
        responses = {
            status: _json_response(
                'Orchestration definition created' if operation == 'create'
                else 'Orchestration definition replaced',
                definition_entry_response_schema(written=True), etag=True,
            ),
        }
        if operation == 'create':
            return orchestration_api_responses(
                responses, 400, 401, 403, 500)
        else:
            responses[str(definition_write_contract()['conflictStatus'])] = \
                orchestration_json_response(
                    'Definition changed since it was read',
                    definition_conflict_response_schema(),
                )
        return orchestration_api_responses(
            responses, 400, 401, 403, 404, 500)
    if operation == 'delete':
        responses = {
            '200': _json_response(
                'Orchestration definition deleted',
                definition_delete_response_schema(),
            ),
            str(definition_write_contract()['conflictStatus']):
                orchestration_json_response(
                    'Definition changed since it was read',
                    definition_conflict_response_schema(),
                ),
        }
        return orchestration_api_responses(
            responses, 400, 401, 403, 404, 500)
    raise ValueError(f'unknown definition operation {operation!r}')


def definition_route_response_registry() -> dict[str, dict]:
    """Build every CRUD response map behind one adapter registration port."""
    return {
        operation: definition_route_responses(operation)
        for operation in ('list', 'read', 'create', 'replace', 'delete')
    }


__all__ = [
    'definition_list_response_schema',
    'definition_entry_response_schema',
    'definition_delete_response_schema',
    'definition_conflict_response_schema',
    'definition_precondition_parameters',
    'definition_route_responses',
    'definition_route_response_registry',
]

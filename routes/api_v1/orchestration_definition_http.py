"""HTTP projection helpers for versioned orchestration definitions.

The framework-free token parser and conflict payload live in the focused
definition wire-contract module. This module is the one HTTP adapter that
maps them to request headers, ETags and API response tuples, keeping CRUD route
handlers focused on endpoint flow.
"""

from __future__ import annotations

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_created,
    api_not_found,
    api_ok,
    api_typed_error,
)
from lib.orchestration.application_result_ports import (
    DefinitionDeleteResultPort,
    DefinitionWriteResultPort,
)
from lib.orchestration.definition_contract_registry import (
    definition_write_contract,
)
from lib.orchestration.definition_wire_projection import (
    definition_write_conflict,
    definition_write_version_token,
    project_definition_entry,
    project_definition_list,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)


_DEFINITION_WRITE = definition_write_contract()


def definition_conflict_response(
    expected_updated_at: int,
    current_updated_at: int,
    *,
    operation: str,
):
    """Map every stale definition mutation through one HTTP projection."""
    return api_conflict(
        'Orchestration changed since the client read it',
        **definition_write_conflict(
            expected_updated_at,
            current_updated_at,
            operation=operation,
        ),
    )


def invalid_definition_response(inspection: dict):
    """Project one failed inspection identically at save and run seams."""
    return api_bad_request(
        'Invalid orchestration definition',
        **inspection_response_fields(inspection, include_errors=True),
    )


def with_definition_etag(response, entry: dict):
    """Attach the same version token accepted by ``If-Match``."""
    http_response, status = response
    version = entry.get(_DEFINITION_WRITE['versionField'])
    if isinstance(version, int) and not isinstance(version, bool):
        http_response.headers[_DEFINITION_WRITE['versionResponseHeader']] = \
            definition_write_version_token(version)
    return http_response, status


def definition_list_response(entries: list[dict]):
    """Return the canonical versioned collection envelope."""
    return api_ok(project_definition_list(entries))


def definition_entry_response(entry: dict | None):
    """Project one repository read, including its HTTP version token."""
    if entry is None:
        return api_not_found('Orchestration not found')
    return with_definition_etag(
        api_ok(project_definition_entry(entry)), entry)


def definition_write_response(
    result: DefinitionWriteResultPort,
    *,
    operation: str,
    expected_updated_at: int | None = None,
):
    """Project create/replace results through one validation/CAS boundary.

    Returns ``(response, entry)`` so the thin route can retain success logging
    without reinterpreting the application result.
    """
    if not result.valid:
        return invalid_definition_response(result.inspection), None
    if result.conflict:
        return definition_conflict_response(
            expected_updated_at,
            result.current_updated_at,
            operation=operation,
        ), None
    entry = result.entry
    if entry is None:
        if operation == 'create':
            return api_typed_error(
                'internal', status=500,
                detail='Failed to create orchestration definition',
                context='api_v1.orchestrations.create',
                source='orchestration.definition.write',
            ), None
        return api_not_found('Orchestration not found'), None
    payload = project_definition_entry(
        entry, inspection=result.inspection)
    response = api_created(payload) if operation == 'create' \
        else api_ok(payload)
    return with_definition_etag(response, entry), entry


def definition_delete_response(
    result: DefinitionDeleteResultPort,
    *,
    operation: str,
    expected_updated_at: int,
):
    """Project one guarded delete without route-local CAS interpretation."""
    if result.conflict:
        return definition_conflict_response(
            expected_updated_at,
            result.current_updated_at,
            operation=operation,
        ), False
    if not result.deleted:
        return api_not_found('Orchestration not found'), False
    return api_ok(), True


__all__ = [
    'definition_conflict_response', 'invalid_definition_response',
    'with_definition_etag', 'definition_list_response',
    'definition_entry_response', 'definition_write_response',
    'definition_delete_response',
]

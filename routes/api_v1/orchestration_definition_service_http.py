"""Shared service-to-HTTP adapter for orchestration definition mutations."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration.application_result_ports import (
    DefinitionDeleteResultPort,
    DefinitionWriteResultPort,
)
from lib.orchestration.http_endpoint_contract import orchestration_http_endpoint

from .orchestration_definition_http import (
    definition_delete_response,
    definition_write_response,
)
from .orchestration_service_http import orchestration_service_response


def _definition_write_operation(
    endpoint: str,
    *,
    allowed: tuple[str, ...],
) -> str:
    operation = orchestration_http_endpoint(endpoint).write_operation
    if operation not in allowed:
        raise ValueError(
            f'{endpoint!r} is not a definition {"/".join(allowed)} endpoint')
    return operation


def orchestration_definition_write_service_response(
    context: str,
    operation: Callable[[], DefinitionWriteResultPort],
    *,
    endpoint: str,
    expected_updated_at: int | None = None,
    on_success: Callable[[dict], None] | None = None,
):
    """Invoke and project one create/replace through the canonical boundary."""
    write_operation = _definition_write_operation(
        endpoint, allowed=('create', 'replace'))

    def project_result(result: DefinitionWriteResultPort):
        response, entry = definition_write_response(
            result,
            operation=write_operation,
            expected_updated_at=expected_updated_at,
        )
        if entry is not None and on_success is not None:
            on_success(entry)
        return response

    return orchestration_service_response(context, operation, project_result)


def orchestration_definition_delete_service_response(
    context: str,
    operation: Callable[[], DefinitionDeleteResultPort],
    *,
    endpoint: str,
    expected_updated_at: int | None = None,
    on_success: Callable[[], None] | None = None,
):
    """Invoke and project one guarded delete through the canonical boundary."""
    write_operation = _definition_write_operation(
        endpoint, allowed=('delete',))

    def project_result(result: DefinitionDeleteResultPort):
        response, deleted = definition_delete_response(
            result,
            operation=write_operation,
            expected_updated_at=expected_updated_at,
        )
        if deleted and on_success is not None:
            on_success()
        return response

    return orchestration_service_response(context, operation, project_result)


__all__ = [
    'orchestration_definition_write_service_response',
    'orchestration_definition_delete_service_response',
]

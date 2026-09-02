"""Shared service-to-HTTP adapter for orchestration mutations."""

from __future__ import annotations

from collections.abc import Callable

from lib.orchestration.mutation_endpoint_contract import mutation_endpoint_contract
from lib.orchestration.mutation_result import OrchestrationMutationResult

from .orchestration_mutation_http import mutation_http_response
from .orchestration_service_http import orchestration_service_response


def orchestration_mutation_service_response(
    context: str,
    operation: Callable[[], OrchestrationMutationResult],
    *,
    endpoint: str,
    on_success: Callable[[], None] | None = None,
):
    """Invoke and project one mutation through the canonical HTTP boundary."""
    mutation_endpoint_contract(endpoint)

    def project_result(result: OrchestrationMutationResult):
        if result.ok and on_success is not None:
            on_success()
        return mutation_http_response(result)

    return orchestration_service_response(context, operation, project_result)


__all__ = ['orchestration_mutation_service_response']

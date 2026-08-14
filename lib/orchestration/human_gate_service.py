"""Application boundary for resolving orchestration human gates.

The underlying approval and guidance registries are shared with chat tasks.
This focused service keeps that concrete dependency and canonical mutation
classification out of HTTP adapters while retaining lazy imports for startup
and test replacement compatibility.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration.errors import HumanGateServiceError
from lib.orchestration.mutation_operations import resolved_mutation
from lib.orchestration.mutation_result import (
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_INPUT_GATE,
    OrchestrationMutationResult,
)
from lib.orchestration.service_call import orchestration_dependency_call


ApprovalResolver = Callable[[str, bool], Any]
GuidanceResolver = Callable[[str, str], Any]


def _resolve_approval(request_id: str, approved: bool) -> Any:
    from lib.tasks_pkg import resolve_write_approval
    return resolve_write_approval(request_id, approved)


def _resolve_guidance(request_id: str, response: str) -> Any:
    from lib.tasks_pkg import resolve_human_guidance
    return resolve_human_guidance(request_id, response)


class OrchestrationHumanGateService:
    """Resolve shared gate registries into the orchestration mutation model."""

    def __init__(
        self,
        *,
        approval_resolver: ApprovalResolver | None = None,
        guidance_resolver: GuidanceResolver | None = None,
    ) -> None:
        self._approval_resolver = approval_resolver or _resolve_approval
        self._guidance_resolver = guidance_resolver or _resolve_guidance

    @staticmethod
    def _resolution_call(message: str, resolver: Callable[[], Any]) -> Any:
        return orchestration_dependency_call(
            resolver,
            error_type=HumanGateServiceError,
            message=message,
        )

    def approve(
        self,
        request_id: str,
        approved: bool,
    ) -> OrchestrationMutationResult:
        return resolved_mutation(
            MUTATION_ACTION_APPROVE_GATE,
            request_id,
            lambda: self._resolution_call(
                'failed to resolve orchestration approval request',
                lambda: self._approval_resolver(request_id, approved),
            ),
        )

    def input(
        self,
        request_id: str,
        response: str,
    ) -> OrchestrationMutationResult:
        return resolved_mutation(
            MUTATION_ACTION_INPUT_GATE,
            request_id,
            lambda: self._resolution_call(
                'failed to resolve orchestration input request',
                lambda: self._guidance_resolver(request_id, response),
            ),
        )


__all__ = [
    'ApprovalResolver',
    'GuidanceResolver',
    'HumanGateServiceError',
    'OrchestrationHumanGateService',
]

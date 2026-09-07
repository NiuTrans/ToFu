"""Reusable collaborator ports for orchestration runtime execution.

These are capability contracts, not concrete services.  The start facade and
worker pipeline can share their wiring without importing TaskRuntime,
definition repositories or HTTP-layer provider types.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from lib.task_runtime_ports import TaskRouteRuntimePort


class OrchestrationTaskRuntimePort(Protocol):
    """Task registry capabilities used by orchestration workers."""

    def create(
        self,
        *,
        user_id: int,
        task_id: str = '',
        meta: dict | None = None,
    ) -> dict: ...

    def append_event(self, task_id: str, event: dict) -> int | None: ...

    def finish(
        self,
        task_id: str,
        *,
        result: object | None = None,
        error: object | None = None,
        error_context: str = '',
    ) -> bool: ...

    def spawn(
        self,
        task_id: str,
        worker: Callable[[], None],
    ) -> None: ...


class OrchestrationRuntimePort(
    OrchestrationTaskRuntimePort,
    TaskRouteRuntimePort,
    Protocol,
):
    """Complete orchestration runtime used by composition services/routes."""


class OrchestrationDefinitionLookupPort(Protocol):
    """Stored-definition capability needed for late subflow resolution."""

    def get_definition(self, orchestration_id: str) -> dict | None: ...


class OrchestrationRunTransitionPort(Protocol):
    """Mutation facts consumed by worker projection policy."""

    ok: bool
    reason: str
    run_status: str


class OrchestrationDurableRunPort(Protocol):
    """Durable-run capabilities shared by start and worker projection."""

    def create_new(
        self,
        *,
        definition: dict,
        input_text: str = '',
        orch_id: str = '',
        name: str = '',
        created_by: str = '',
    ) -> str: ...

    def project_event(
        self,
        run_id: str,
        seq: int,
        event: dict,
        status: str = '',
    ) -> bool: ...

    def transition_status(
        self,
        run_id: str,
        status: str,
        *,
        final: str | None = None,
        error: dict | str | None = None,
    ) -> OrchestrationRunTransitionPort: ...


OrchestrationDefinitionProvider = Callable[
    [], OrchestrationDefinitionLookupPort
]
OrchestrationDurableRunProvider = Callable[[], OrchestrationDurableRunPort]


__all__ = [
    'OrchestrationTaskRuntimePort',
    'OrchestrationRuntimePort',
    'OrchestrationDefinitionLookupPort',
    'OrchestrationRunTransitionPort',
    'OrchestrationDurableRunPort',
    'OrchestrationDefinitionProvider',
    'OrchestrationDurableRunProvider',
]

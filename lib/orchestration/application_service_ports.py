"""Capability ports implemented by orchestration application services."""

from __future__ import annotations

from typing import Protocol

from lib.orchestration.application_result_ports import (
    AuthoringBuiltinResultPort,
    AuthoringPlanResultPort,
    DefinitionDeleteResultPort,
    DefinitionWriteResultPort,
    DurableReplayResultPort,
    OrchestrationMutationResultPort,
    ResolvedDefinitionPort,
)
from lib.orchestration.runtime_ports import OrchestrationDurableRunPort


class AuthoringServicePort(Protocol):
    def build_builtin(
        self,
        name: str,
        **options: object,
    ) -> dict | None: ...

    def inspect(self, definition: dict) -> dict: ...

    def compose(
        self,
        requirement: str,
        *,
        current: dict | None = None,
        history: list[dict] | None = None,
    ) -> dict: ...

    def builtin_inspection(
        self,
        name: str,
    ) -> AuthoringBuiltinResultPort: ...

    def contract(self) -> dict: ...

    def layout(self, definition: dict) -> dict: ...

    def plan(self, definition: dict) -> AuthoringPlanResultPort: ...


class DefinitionServicePort(Protocol):
    def resolve(
        self,
        *,
        inline: dict | None = None,
        builtin: str = '',
        stored_id: str = '',
        require_inline_nodes: bool = False,
    ) -> ResolvedDefinitionPort: ...

    def list_summaries(self) -> list[dict]: ...

    def get_entry(self, orchestration_id: str) -> dict | None: ...

    def get_definition(self, orchestration_id: str) -> dict | None: ...

    def create(self, definition: dict) -> DefinitionWriteResultPort: ...

    def update(
        self,
        orchestration_id: str,
        definition: dict,
        *,
        expected_updated_at: int,
    ) -> DefinitionWriteResultPort: ...

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int,
    ) -> DefinitionDeleteResultPort: ...


class RunServicePort(OrchestrationDurableRunPort, Protocol):
    """Complete durable-run application API used by delivery adapters."""

    def list(
        self,
        *,
        status: str = '',
        orch_id: str = '',
        limit: int = 50,
    ) -> list[dict]: ...

    def get(self, run_id: str) -> dict | None: ...

    def replay(
        self,
        run_id: str,
        cursor: int = 0,
    ) -> DurableReplayResultPort | None: ...

    def abort(
        self,
        run_id: str,
    ) -> OrchestrationMutationResultPort: ...

    def delete(self, run_id: str) -> OrchestrationMutationResultPort: ...

    def transition_status(
        self,
        run_id: str,
        status: str,
        *,
        final: str | None = None,
        error: dict | str | None = None,
    ) -> OrchestrationMutationResultPort: ...


class RuntimeStartServicePort(Protocol):
    """Canonical delivery port for every orchestration start mode."""

    def start(
        self,
        kind: str,
        definition: dict,
        *,
        owner_user_id: int,
        input_text: str = '',
        orchestration_id: str = '',
        created_by: str = '',
    ) -> str: ...


class RuntimeMutationServicePort(Protocol):
    """Canonical service boundary for transient runtime mutations."""

    def abort(self, task_id: str) -> OrchestrationMutationResultPort: ...


class HumanGateServicePort(Protocol):
    def approve(
        self,
        request_id: str,
        approved: bool,
    ) -> OrchestrationMutationResultPort: ...

    def input(
        self,
        request_id: str,
        response: str,
    ) -> OrchestrationMutationResultPort: ...


__all__ = [
    'AuthoringServicePort',
    'DefinitionServicePort',
    'RunServicePort',
    'RuntimeStartServicePort',
    'RuntimeMutationServicePort',
    'HumanGateServicePort',
]

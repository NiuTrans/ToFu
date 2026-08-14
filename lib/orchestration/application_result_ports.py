"""Structural result ports returned by orchestration application services."""

from __future__ import annotations

from typing import Protocol

from lib.orchestration.runtime_ports import OrchestrationRunTransitionPort


class ResolvedDefinitionPort(Protocol):
    definition: dict | None
    source: str
    stored_id: str


class AuthoringPlanResultPort(Protocol):
    plan: dict
    inspection: dict


class AuthoringBuiltinResultPort(Protocol):
    definition: dict | None
    inspection: dict | None


class DefinitionWriteResultPort(Protocol):
    entry: dict | None
    inspection: dict
    conflict: bool
    current_updated_at: int | None

    @property
    def valid(self) -> bool: ...


class DefinitionDeleteResultPort(Protocol):
    deleted: bool
    conflict: bool
    current_updated_at: int | None


class DurableReplayResultPort(Protocol):
    def payload(self, extras: dict | None = None) -> dict: ...


class OrchestrationMutationResultPort(
    OrchestrationRunTransitionPort,
    Protocol,
):
    """Full mutation outcome extending the runtime transition facts."""

    action: str
    target_id: str

    @property
    def canonical_reason(self) -> str: ...

    @property
    def retryable(self) -> bool: ...

    @property
    def reconcile_required(self) -> bool: ...

    @property
    def target_exists(self) -> bool | None: ...

    @property
    def resource_terminal(self) -> bool | None: ...

    def payload(self) -> dict: ...


__all__ = [
    'ResolvedDefinitionPort',
    'AuthoringPlanResultPort',
    'AuthoringBuiltinResultPort',
    'DefinitionWriteResultPort',
    'DefinitionDeleteResultPort',
    'DurableReplayResultPort',
    'OrchestrationMutationResultPort',
]

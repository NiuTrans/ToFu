"""Structural port for the complete orchestration-definition repository."""

from __future__ import annotations

from typing import Protocol, cast


class DefinitionStoreMutationPort(Protocol):
    entry: dict | None
    conflict: bool
    current_updated_at: int | None
    deleted: bool


class OrchestrationDefinitionStorePort(Protocol):
    def list_entries(self) -> list[dict]: ...
    def get_entry(self, orchestration_id: str) -> dict | None: ...
    def get_definition(self, orchestration_id: str) -> dict | None: ...
    def create(self, definition: dict) -> dict: ...

    def update_if_current(
        self,
        orchestration_id: str,
        definition: dict,
        *,
        expected_updated_at: int,
    ) -> DefinitionStoreMutationPort: ...

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int,
    ) -> DefinitionStoreMutationPort: ...


_REQUIRED_METHODS = (
    "list_entries",
    "get_entry",
    "get_definition",
    "create",
    "update_if_current",
    "delete_if_current",
)


def bind_orchestration_definition_store(
    candidate: object,
) -> OrchestrationDefinitionStorePort:
    """Reject an incomplete repository at composition; no runtime adapters."""
    missing = [
        name for name in _REQUIRED_METHODS
        if not callable(getattr(candidate, name, None))
    ]
    if missing:
        raise TypeError(
            "invalid orchestration definition store; missing callable(s): "
            + ", ".join(missing))
    return cast(OrchestrationDefinitionStorePort, candidate)


__all__ = [
    "DefinitionStoreMutationPort",
    "OrchestrationDefinitionStorePort",
    "bind_orchestration_definition_store",
]

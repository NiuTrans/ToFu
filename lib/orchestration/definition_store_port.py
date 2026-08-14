"""Structural repository port for stored orchestration definitions.

DefinitionService binds once to this complete capability set. Legacy stores
with unguarded ``update``/``delete`` methods are adapted here so validation,
resolution and mutation business logic never probes repository versions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast


class DefinitionStoreMutationPort(Protocol):
    entry: dict | None
    conflict: bool
    current_updated_at: int | None
    deleted: bool


class DefinitionStoreConcurrencyError(RuntimeError):
    """A legacy repository cannot honor a requested mutation fence."""


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
        expected_updated_at: int | None = None,
    ) -> DefinitionStoreMutationPort: ...

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int | None = None,
    ) -> DefinitionStoreMutationPort: ...


@dataclass(frozen=True)
class _LegacyMutationResult:
    entry: dict | None = None
    conflict: bool = False
    current_updated_at: int | None = None
    deleted: bool = False


class _LegacyDefinitionStoreAdapter:
    """Upgrade an unguarded store at the composition boundary only."""

    def __init__(self, candidate):
        self._candidate = candidate

    def list_entries(self) -> list[dict]:
        return self._candidate.list_entries()

    def get_entry(self, orchestration_id: str) -> dict | None:
        return self._candidate.get_entry(orchestration_id)

    def get_definition(self, orchestration_id: str) -> dict | None:
        return self._candidate.get_definition(orchestration_id)

    def create(self, definition: dict) -> dict:
        return self._candidate.create(definition)

    def update_if_current(self, orchestration_id: str, definition: dict, *,
                          expected_updated_at: int | None = None) \
            -> DefinitionStoreMutationPort:
        guarded = getattr(self._candidate, 'update_if_current', None)
        if callable(guarded):
            return guarded(
                orchestration_id,
                definition,
                expected_updated_at=expected_updated_at,
            )
        if expected_updated_at is not None:
            raise DefinitionStoreConcurrencyError(
                'legacy orchestration definition store cannot perform '
                'a guarded update')
        entry = self._candidate.update(orchestration_id, definition)
        version = entry.get('updatedAt') if isinstance(entry, dict) else None
        return _LegacyMutationResult(
            entry=entry,
            current_updated_at=(
                version
                if isinstance(version, int) and not isinstance(version, bool)
                else None
            ),
        )

    def delete_if_current(self, orchestration_id: str, *,
                          expected_updated_at: int | None = None) \
            -> DefinitionStoreMutationPort:
        guarded = getattr(self._candidate, 'delete_if_current', None)
        if callable(guarded):
            return guarded(
                orchestration_id,
                expected_updated_at=expected_updated_at,
            )
        if expected_updated_at is not None:
            raise DefinitionStoreConcurrencyError(
                'legacy orchestration definition store cannot perform '
                'a guarded delete')
        return _LegacyMutationResult(
            deleted=bool(self._candidate.delete(orchestration_id)))


_BASE_METHODS = (
    'list_entries', 'get_entry', 'get_definition', 'create',
)


def bind_orchestration_definition_store(candidate: object) \
        -> OrchestrationDefinitionStorePort:
    """Validate or explicitly adapt one repository at service construction."""
    missing = [
        name for name in _BASE_METHODS
        if not callable(getattr(candidate, name, None))
    ]
    if missing:
        raise TypeError(
            'invalid orchestration definition store; missing callable(s): '
            + ', '.join(missing)
        )

    guarded = ('update_if_current', 'delete_if_current')
    if all(callable(getattr(candidate, name, None)) for name in guarded):
        return cast(OrchestrationDefinitionStorePort, candidate)

    mutation_pairs = (
        ('update_if_current', 'update'),
        ('delete_if_current', 'delete'),
    )
    missing_mutations = [
        guarded_name
        for guarded_name, legacy_name in mutation_pairs
        if not callable(getattr(candidate, guarded_name, None))
        and not callable(getattr(candidate, legacy_name, None))
    ]
    if missing_mutations:
        raise TypeError(
            'invalid orchestration definition store; missing guarded '
            'callable(s): ' + ', '.join(missing_mutations)
        )
    return _LegacyDefinitionStoreAdapter(candidate)


__all__ = [
    'DefinitionStoreMutationPort', 'DefinitionStoreConcurrencyError',
    'OrchestrationDefinitionStorePort',
    'bind_orchestration_definition_store',
]

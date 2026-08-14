"""Application service for stored orchestration definitions.

Owns repository failure typing, validation-before-write, optimistic mutation
results and stored/inline resolution. Pure inspection/canonicalization lives in
``definition_inspection.py``. HTTP, chat and runtime adapters depend on focused
owners instead of the compatibility ``service`` facade or JSON repository.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from lib.orchestration.definition_inspection import (
    PreparedDefinition,
    inspect_definition,
    prepare_definition,
)
from lib.orchestration.definition_resolution import resolve_definition
from lib.orchestration.definition_results import (
    DefinitionDeleteResult,
    DefinitionWriteResult,
    ResolvedDefinition,
)
from lib.orchestration.definition_store_port import (
    OrchestrationDefinitionStorePort,
    bind_orchestration_definition_store,
)
from lib.orchestration.errors import DefinitionServiceError
from lib.orchestration.service_call import orchestration_dependency_call
from lib.orchestration.definition_wire_contracts import (
    definition_entry_summary,
)


_ResultT = TypeVar('_ResultT')


def _repository_call(
    operation: str,
    callback: Callable[[], _ResultT],
) -> _ResultT:
    """Keep repository exceptions behind one debuggable service boundary."""
    return orchestration_dependency_call(
        callback,
        error_type=DefinitionServiceError,
        message=f'failed to {operation} orchestration definitions',
    )


class OrchestrationDefinitionService:
    """Application interface for stored authoring definitions."""

    def __init__(self, repository: OrchestrationDefinitionStorePort):
        self._repository = bind_orchestration_definition_store(repository)

    @classmethod
    def from_path(cls, path: str | None = None):
        from lib.orchestration.store import OrchestrationStore
        return cls(OrchestrationStore(path))

    def list_entries(self) -> list[dict]:
        return _repository_call(
            'list', self._repository.list_entries)

    def list_summaries(self) -> list[dict]:
        """Return newest-first metadata rows without complete DAGs."""
        summaries = (
            definition_entry_summary(entry)
            for entry in self.list_entries()
        )
        rows = [summary for summary in summaries if summary is not None]

        def _key(item):
            updated_at = item.get('updatedAt')
            created_at = item.get('createdAt')
            return (
                -(updated_at if isinstance(updated_at, int) else -1),
                -(created_at if isinstance(created_at, int) else -1),
                item['id'],
            )

        return sorted(rows, key=_key)

    def get_entry(self, orchestration_id: str) -> dict | None:
        return _repository_call(
            'read',
            lambda: self._repository.get_entry(orchestration_id),
        )

    def get_definition(self, orchestration_id: str) -> dict | None:
        return _repository_call(
            'read',
            lambda: self._repository.get_definition(orchestration_id),
        )

    def resolve(self, *, inline: dict | None = None, builtin: str = '',
                stored_id: str = '',
                require_inline_nodes: bool = False) -> ResolvedDefinition:
        return resolve_definition(
            inline=inline,
            builtin=builtin,
            stored_id=stored_id,
            load_stored=self.get_definition,
            require_inline_nodes=require_inline_nodes,
        )

    def create(self, definition: dict) -> DefinitionWriteResult:
        prepared = prepare_definition(definition)
        entry = (_repository_call(
            'create',
            lambda: self._repository.create(prepared.definition),
        ) if prepared.definition is not None else None)
        return DefinitionWriteResult(entry, prepared.inspection)

    def update(self, orchestration_id: str, definition: dict, *,
               expected_updated_at: int | None = None) -> DefinitionWriteResult:
        prepared = prepare_definition(definition)
        if prepared.definition is None:
            return DefinitionWriteResult(None, prepared.inspection)
        stored = _repository_call(
            'update',
            lambda: self._repository.update_if_current(
                orchestration_id,
                prepared.definition,
                expected_updated_at=expected_updated_at,
            ),
        )
        return DefinitionWriteResult(
            stored.entry,
            prepared.inspection,
            conflict=bool(stored.conflict),
            current_updated_at=stored.current_updated_at,
        )

    def delete(self, orchestration_id: str) -> bool:
        """Compatibility delete for callers without a known version."""
        return self.delete_if_current(orchestration_id).deleted

    def delete_if_current(
        self,
        orchestration_id: str,
        *,
        expected_updated_at: int | None = None,
    ) -> DefinitionDeleteResult:
        stored = _repository_call(
            'delete',
            lambda: self._repository.delete_if_current(
                orchestration_id,
                expected_updated_at=expected_updated_at,
            ),
        )
        return DefinitionDeleteResult(
            bool(stored.deleted),
            conflict=bool(stored.conflict),
            current_updated_at=stored.current_updated_at,
        )


__all__ = [
    'DefinitionServiceError', 'ResolvedDefinition', 'DefinitionWriteResult',
    'DefinitionDeleteResult', 'PreparedDefinition',
    'OrchestrationDefinitionService', 'prepare_definition',
    'resolve_definition', 'inspect_definition',
]

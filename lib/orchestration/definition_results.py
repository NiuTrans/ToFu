"""Concrete result values returned by the definition application service."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedDefinition:
    definition: dict | None
    source: str = ''
    stored_id: str = ''


@dataclass(frozen=True)
class DefinitionWriteResult:
    """One validated repository mutation with its authoring contract."""

    entry: dict | None
    inspection: dict
    conflict: bool = False
    current_updated_at: int | None = None

    @property
    def valid(self) -> bool:
        return bool(self.inspection.get('ok'))


@dataclass(frozen=True)
class DefinitionDeleteResult:
    """One repository deletion with optimistic-concurrency classification."""

    deleted: bool
    conflict: bool = False
    current_updated_at: int | None = None


__all__ = [
    'ResolvedDefinition',
    'DefinitionWriteResult',
    'DefinitionDeleteResult',
]

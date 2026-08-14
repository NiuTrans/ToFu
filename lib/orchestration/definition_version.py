"""Shared stored-definition version value and schema policy."""

from __future__ import annotations

from typing import TypeGuard

from lib.orchestration.definition_contract_registry import (
    MAX_DEFINITION_VERSION,
)


def is_definition_version(value: object) -> TypeGuard[int]:
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= MAX_DEFINITION_VERSION)


def require_definition_version(
    value: object,
    *,
    field: str = 'definition version',
    nullable: bool = False,
) -> int | None:
    if value is None and nullable:
        return None
    if not is_definition_version(value):
        raise ValueError(f'{field} must be a safe non-negative int')
    return value


def definition_version_schema(*, nullable: bool = False) -> dict:
    return {
        'type': ['integer', 'null'] if nullable else 'integer',
        'minimum': 0,
        'maximum': MAX_DEFINITION_VERSION,
    }


__all__ = [
    'definition_version_schema',
    'is_definition_version',
    'require_definition_version',
]

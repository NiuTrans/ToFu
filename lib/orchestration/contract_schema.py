"""OpenAPI projection primitives for backend-owned contract metadata."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def contract_snapshot_schema(
    value: Any,
    *,
    open_object_paths: Iterable[tuple[str, ...]] = (),
) -> dict:
    """Describe one detached contract snapshot without copying its policy.

    Contract documents are executable metadata rather than arbitrary payloads:
    scalar values are constants, vocabularies contain every published member,
    and object keys are required. ``open_object_paths`` keeps explicitly
    forward-compatible registries (currently event types) additive.
    """
    open_paths = frozenset(tuple(path) for path in open_object_paths)

    def project(current: Any, path: tuple[str, ...]) -> dict:
        if isinstance(current, dict):
            properties = {
                str(name): project(item, (*path, str(name)))
                for name, item in current.items()
            }
            schema = {
                'type': 'object',
                'additionalProperties': path in open_paths,
                'properties': properties,
            }
            if properties:
                schema['required'] = list(properties)
            return schema
        if isinstance(current, list):
            schema = {
                'type': 'array',
                'minItems': len(current),
                'maxItems': len(current),
            }
            if not current:
                schema['items'] = {}
                return schema
            if all(isinstance(item, str) for item in current):
                schema.update({
                    'items': {'type': 'string', 'enum': list(current)},
                    'uniqueItems': len(set(current)) == len(current),
                })
                return schema
            if all(isinstance(item, int) and not isinstance(item, bool)
                   for item in current):
                schema.update({
                    'items': {'type': 'integer', 'enum': list(current)},
                    'uniqueItems': len(set(current)) == len(current),
                })
                return schema
            schema['items'] = project(current[0], (*path, '*'))
            return schema
        if isinstance(current, bool):
            return {'type': 'boolean', 'const': current}
        if isinstance(current, int):
            return {
                'type': 'integer',
                'const': current,
                'minimum': 1 if current > 0 else 0,
            }
        if isinstance(current, float):
            return {'type': 'number', 'const': current}
        if current is None:
            return {'type': 'null'}
        return {'type': 'string', 'enum': [str(current)]}

    return project(value, ())


__all__ = ['contract_snapshot_schema']

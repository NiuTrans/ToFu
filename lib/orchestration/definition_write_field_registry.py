"""Canonical semantic-to-wire fields for definition CAS conflicts."""

from __future__ import annotations


_DEFINITION_WRITE_CONFLICT_FIELDS = (
    ('format', 'format', 'string'),
    ('reason', 'reason', 'string'),
    ('operation', 'operation', 'string'),
    (
        'expectedUpdatedAt', 'expectedUpdatedAt',
        'non_negative_integer',
    ),
    (
        'currentUpdatedAt', 'currentUpdatedAt',
        'non_negative_integer',
    ),
)


def definition_write_conflict_fields() -> dict[str, dict[str, str]]:
    """Return a detached semantic-to-wire registry for stale writes."""
    return {
        semantic: {'name': name, 'type': field_type}
        for semantic, name, field_type in _DEFINITION_WRITE_CONFLICT_FIELDS
    }


__all__ = ['definition_write_conflict_fields']

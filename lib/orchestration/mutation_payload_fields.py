"""Canonical field registry for versioned orchestration mutation payloads."""

from __future__ import annotations

from typing import Final


MUTATION_PAYLOAD_FIELD_SPECS: Final = (
    ('format', 'format', 'string'),
    ('ok', 'ok', 'boolean'),
    ('action', 'action', 'string'),
    ('reason', 'reason', 'string'),
    ('targetId', 'target_id', 'string'),
    ('resourceStatus', 'resource_status', 'string'),
    ('resourceTerminal', 'resource_terminal', 'nullable_boolean'),
    ('targetExists', 'target_exists', 'nullable_boolean'),
    ('retryable', 'retryable', 'boolean'),
    ('reconcileRequired', 'reconcile_required', 'boolean'),
)


def mutation_payload_field_contract() -> dict[str, dict[str, str]]:
    """Return detached semantic-to-wire metadata for every payload field."""
    return {
        semantic: {'name': name, 'type': field_type}
        for semantic, name, field_type in MUTATION_PAYLOAD_FIELD_SPECS
    }


def mutation_payload_field_names() -> dict[str, str]:
    """Return the semantic-to-wire names used by response serialization."""
    return {
        semantic: name
        for semantic, name, _field_type in MUTATION_PAYLOAD_FIELD_SPECS
    }


__all__ = [
    'MUTATION_PAYLOAD_FIELD_SPECS',
    'mutation_payload_field_contract',
    'mutation_payload_field_names',
]

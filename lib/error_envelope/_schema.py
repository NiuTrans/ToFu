"""Canonical structure and OpenAPI schema for typed error envelopes.

The runtime validator and generated API documentation both consume the
definitions in this module.  Keeping the Python field types and the wire
schema together prevents a runtime field from being added without appearing
in OpenAPI (or vice versa).
"""

from __future__ import annotations

from typing import Any

from lib.error_envelope._constants import KINDS


_REQUIRED_ENVELOPE_FIELDS: dict[str, type] = {
    'kind': str,
    'severity': str,
    'retryable': bool,
    'message': str,
    'hint': str,
    'detail': str,
    'model': str,
    'context': str,
    'source': str,
    'raw': str,
}
_OPTIONAL_ENVELOPE_FIELDS: dict[str, type] = {
    'titleKey': str,
    'hintKey': str,
}
_CORE_FIELDS = frozenset(
    (*_REQUIRED_ENVELOPE_FIELDS, *_OPTIONAL_ENVELOPE_FIELDS))
_SEVERITIES = ('warning', 'error')


def _is_complete_envelope(error: dict[str, Any]) -> bool:
    """Return whether *error* satisfies the durable runtime contract."""
    return (all(isinstance(error.get(field), expected)
                for field, expected in _REQUIRED_ENVELOPE_FIELDS.items())
            and error.get('kind') in KINDS
            and error.get('severity') in _SEVERITIES
            and bool(error.get('message')))


def typed_error_envelope_schema() -> dict[str, Any]:
    """Build a fresh OpenAPI 3.1 schema for the typed envelope payload.

    Additional properties remain allowed intentionally: domains can attach
    versioned diagnostics such as orchestration's ``outcome`` while every
    consumer can rely on the shared core fields.
    """
    string_fields = {
        name: {'type': 'string'}
        for name, value_type in {
            **_REQUIRED_ENVELOPE_FIELDS,
            **_OPTIONAL_ENVELOPE_FIELDS,
        }.items()
        if value_type is str
    }
    return {
        'type': 'object',
        'properties': {
            **string_fields,
            'kind': {'type': 'string', 'enum': sorted(KINDS)},
            'severity': {'type': 'string', 'enum': list(_SEVERITIES)},
            'retryable': {'type': 'boolean'},
        },
        'required': list(_REQUIRED_ENVELOPE_FIELDS),
        'additionalProperties': True,
    }


__all__ = ['typed_error_envelope_schema']

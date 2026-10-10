"""Inline-or-stored definition selection request contract."""

from __future__ import annotations

from typing import Any

from lib.orchestration.definition_contract_schema import (
    definition_request_schema,
)
from lib.orchestration.request_limit_contract import MAX_RUN_INPUT_LENGTH
from lib.request_parser import optional_str


def definition_selection_contract() -> dict:
    """Return detached request field identity shared across transports."""
    return {
        'inlineField': 'definition',
        'storedIdField': 'id',
        'originField': 'originId',
        'inputField': 'input',
        'inputMaxLength': MAX_RUN_INPUT_LENGTH,
    }


def definition_selection_request_schema(
    *,
    include_input: bool = False,
) -> dict:
    """Describe the same inline/stored selection accepted by services."""
    contract = definition_selection_contract()
    inline_field = contract['inlineField']
    stored_id_field = contract['storedIdField']
    properties = {
        inline_field: definition_request_schema(),
        stored_id_field: {'type': 'string', 'minLength': 1},
        contract['originField']: {'type': 'string', 'minLength': 1},
    }
    if include_input:
        properties[contract['inputField']] = {
            'type': 'string', 'maxLength': contract['inputMaxLength'],
        }
    return {
        'type': 'object',
        'properties': properties,
        'anyOf': [
            {'required': [inline_field]},
            {'required': [stored_id_field]},
        ],
    }


def definition_selection_values(body: dict) -> tuple[Any, str]:
    """Return raw inline value and normalized stored identity."""
    contract = definition_selection_contract()
    stored_id = body.get(contract['storedIdField'])
    return (
        body.get(contract['inlineField']),
        stored_id if isinstance(stored_id, str) else '',
    )


def definition_selection_input(body: dict) -> str:
    """Parse the optional run input through the published bound."""
    contract = definition_selection_contract()
    return optional_str(
        body,
        contract['inputField'],
        default='',
        max_len=contract['inputMaxLength'],
    )


def definition_selection_origin_id(body: dict) -> str:
    """Return explicit inline-snapshot lineage without selecting storage.

    ``id`` chooses a stored definition when no inline snapshot is supplied.
    ``originId`` only associates an inline snapshot with the saved definition
    it was edited from, so selection and provenance never overload one field.
    """
    value = body.get(definition_selection_contract()['originField'])
    return value.strip() if isinstance(value, str) else ''


__all__ = [
    'MAX_RUN_INPUT_LENGTH', 'definition_selection_contract',
    'definition_selection_request_schema', 'definition_selection_values',
    'definition_selection_input', 'definition_selection_origin_id',
]

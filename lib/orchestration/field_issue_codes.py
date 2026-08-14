"""Canonical diagnostic codes for backend and Studio FieldSpec validation."""

from __future__ import annotations


FIELD_UNKNOWN = 'field.unknown'
FIELD_TYPE_LIST = 'field.type.list'
FIELD_MAX_ITEMS = 'field.max_items'
FIELD_MAX_ITEM_LENGTH = 'field.max_item_length'
FIELD_TYPE_BOOLEAN = 'field.type.boolean'
FIELD_TYPE_INTEGER = 'field.type.integer'
FIELD_MINIMUM = 'field.minimum'
FIELD_MAXIMUM = 'field.maximum'
FIELD_RUNTIME_MAX = 'field.runtime_max'
FIELD_CHOICE = 'field.choice'
FIELD_TYPE_STRING = 'field.type.string'
FIELD_MAX_LENGTH = 'field.max_length'
FIELD_CONTRACT_UNSUPPORTED = 'field.contract.unsupported'


_CLIENT_FAILURE_CODES = {
    'unsupportedContract': FIELD_CONTRACT_UNSUPPORTED,
    'invalidNumber': FIELD_TYPE_INTEGER,
    'invalidBoolean': FIELD_TYPE_BOOLEAN,
    'maxLength': FIELD_MAX_LENGTH,
    'maxItems': FIELD_MAX_ITEMS,
    'maxItemLength': FIELD_MAX_ITEM_LENGTH,
}


def field_client_failure_codes() -> dict[str, str]:
    """Return detached codes for failures Studio can reject before saving."""
    return dict(_CLIENT_FAILURE_CODES)


__all__ = [
    'FIELD_CHOICE',
    'FIELD_CONTRACT_UNSUPPORTED',
    'FIELD_MAXIMUM',
    'FIELD_MAX_ITEMS',
    'FIELD_MAX_ITEM_LENGTH',
    'FIELD_MAX_LENGTH',
    'FIELD_MINIMUM',
    'FIELD_RUNTIME_MAX',
    'FIELD_TYPE_BOOLEAN',
    'FIELD_TYPE_INTEGER',
    'FIELD_TYPE_LIST',
    'FIELD_TYPE_STRING',
    'FIELD_UNKNOWN',
    'field_client_failure_codes',
]

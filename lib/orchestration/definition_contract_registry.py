"""Stable stored-definition wire contract values."""

from __future__ import annotations

from lib.orchestration.wire_formats import (
    DEFINITION_ENTRY_FORMAT,
    DEFINITION_LIST_FORMAT,
    DEFINITION_WRITE_FORMAT,
)
from lib.orchestration.definition_write_field_registry import (
    definition_write_conflict_fields,
)


MAX_DEFINITION_VERSION = 9_007_199_254_740_991


def definition_list_contract() -> dict:
    return {
        'format': DEFINITION_LIST_FORMAT,
        'itemFields': ['id', 'name', 'nodeCount', 'createdAt', 'updatedAt'],
        'definitionIncluded': False,
        'orderBy': [
            {'field': 'updatedAt', 'direction': 'desc'},
            {'field': 'createdAt', 'direction': 'desc'},
            {'field': 'id', 'direction': 'asc'},
        ],
    }


def definition_entry_contract() -> dict:
    return {
        'format': DEFINITION_ENTRY_FORMAT,
        'fields': [
            'id', 'name', 'definition', 'createdAt', 'updatedAt',
        ],
        'versionField': 'updatedAt',
        'versionRequiredOnWrite': True,
        'inspectionIncludedOnWrite': True,
    }


def definition_write_contract() -> dict:
    return {
        'format': DEFINITION_WRITE_FORMAT,
        'versionField': 'updatedAt',
        'versionResponseHeader': 'ETag',
        'preconditionHeader': 'If-Match',
        'tokenSyntax': 'quoted-decimal',
        'conflictStatus': 409,
        'conflictReason': 'stale_definition',
        'operations': ['replace', 'delete'],
        'conflictFields': definition_write_conflict_fields(),
    }


__all__ = [
    'MAX_DEFINITION_VERSION',
    'definition_entry_contract',
    'definition_list_contract',
    'definition_write_contract',
]

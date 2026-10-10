"""Canonical request contract for orchestration Composer actions."""

from __future__ import annotations

from lib.orchestration.request_limit_contract import (
    MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    MAX_COMPOSE_HISTORY_ITEMS,
    MAX_COMPOSE_REQUIREMENT_LENGTH,
)


def compose_request_contract() -> dict:
    """Publish detached field identity and enforced Composer bounds."""
    return {
        'requirementField': 'requirement',
        'currentField': 'current',
        'historyField': 'history',
        'requirementMaxLength': MAX_COMPOSE_REQUIREMENT_LENGTH,
        'historyRetainedItems': MAX_COMPOSE_HISTORY_ITEMS,
        'historyContentMaxLength': MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    }


def compose_request_schema() -> dict:
    """Describe the Composer body accepted by HTTP ingress."""
    contract = compose_request_contract()
    requirement = contract['requirementField']
    current = contract['currentField']
    history = contract['historyField']
    return {
        'type': 'object',
        'required': [requirement],
        'properties': {
            requirement: {
                'type': 'string',
                'minLength': 1,
                'maxLength': contract['requirementMaxLength'],
            },
            current: {'type': 'object'},
            history: {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'required': ['role', 'content'],
                    'properties': {
                        'role': {
                            'type': 'string',
                            'enum': ['user', 'assistant'],
                        },
                        'content': {
                            'type': 'string',
                            'maxLength': contract[
                                'historyContentMaxLength'],
                        },
                    },
                },
                'x-retainedItems': contract['historyRetainedItems'],
                'description': 'Older clients may send more entries; only '
                               'the newest retained window is consumed.',
            },
        },
    }


__all__ = [
    'MAX_COMPOSE_HISTORY_CONTENT_LENGTH',
    'MAX_COMPOSE_HISTORY_ITEMS',
    'MAX_COMPOSE_REQUIREMENT_LENGTH',
    'compose_request_contract',
    'compose_request_schema',
]

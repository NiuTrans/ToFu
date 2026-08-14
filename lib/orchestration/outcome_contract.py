"""Published orchestration outcome policy and OpenAPI schema."""

from __future__ import annotations

from typing import Final

from lib.agent_verdict import INCOMPLETE_STOP_REASONS
from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.outcome_domain import OUTCOME_CATEGORIES
from lib.orchestration.wire_formats import OUTCOME_FORMAT


OUTCOME_FINAL_DISPLAY_CHARS: Final = 16000
OUTCOME_ERROR_DISPLAY_CHARS: Final = 4000


def outcome_contract() -> dict:
    from lib.orchestration.run_status import run_status_contract

    return {
        'format': OUTCOME_FORMAT,
        'categories': list(OUTCOME_CATEGORIES),
        'engineStatuses': ['completed', 'failed', 'aborted'],
        'lifecycleStatuses': run_status_contract()['terminal'],
        'chatStatuses': ['done', 'error', 'aborted'],
        'finishReasons': ['stop', 'incomplete', 'error', 'aborted'],
        'incompleteStopReasons': sorted(INCOMPLETE_STOP_REASONS),
        'displayLimits': {
            'final': OUTCOME_FINAL_DISPLAY_CHARS,
            'error': OUTCOME_ERROR_DISPLAY_CHARS,
        },
    }


def outcome_contract_schema() -> dict:
    return contract_snapshot_schema(outcome_contract())


def outcome_payload_schema() -> dict:
    contract = outcome_contract()
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': [
            'format', 'category', 'engine_status', 'lifecycle_status',
            'chat_status', 'ok', 'stop_reason', 'finish_reason', 'error',
        ],
        'properties': {
            'format': {'type': 'string', 'enum': [contract['format']]},
            'category': {
                'type': 'string', 'enum': contract['categories'],
            },
            'engine_status': {
                'type': 'string', 'enum': contract['engineStatuses'],
            },
            'lifecycle_status': {
                'type': 'string', 'enum': contract['lifecycleStatuses'],
            },
            'chat_status': {
                'type': 'string', 'enum': contract['chatStatuses'],
            },
            'ok': {'type': 'boolean'},
            'stop_reason': {'type': 'string'},
            'finish_reason': {
                'type': 'string', 'enum': contract['finishReasons'],
            },
            'error': {'type': 'string'},
        },
    }


__all__ = [
    'OUTCOME_ERROR_DISPLAY_CHARS',
    'OUTCOME_FINAL_DISPLAY_CHARS',
    'outcome_contract',
    'outcome_contract_schema',
    'outcome_payload_schema',
]

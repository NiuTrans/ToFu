"""Canonical request contracts for orchestration human-gate mutations."""

from __future__ import annotations

from lib.human_gate_contract import MAX_HUMAN_GATE_REQUEST_ID_LENGTH
from lib.orchestration.request_limit_contract import MAX_HUMAN_INPUT_LENGTH


def human_gate_request_contract() -> dict:
    """Publish detached field identity, defaults and enforced input bounds."""
    return {
        'requestIdField': 'requestId',
        'requestIdMaxLength': MAX_HUMAN_GATE_REQUEST_ID_LENGTH,
        'approvalField': 'approved',
        'approvalDefault': False,
        'inputField': 'response',
        'inputMaxLength': MAX_HUMAN_INPUT_LENGTH,
    }


def human_approval_request_schema() -> dict:
    contract = human_gate_request_contract()
    request_id = contract['requestIdField']
    approval = contract['approvalField']
    return {
        'type': 'object',
        'required': [request_id],
        'properties': {
            request_id: {
                'type': 'string',
                'minLength': 1,
                'maxLength': contract['requestIdMaxLength'],
            },
            approval: {
                'type': 'boolean',
                'default': contract['approvalDefault'],
            },
        },
    }


def human_input_request_schema() -> dict:
    contract = human_gate_request_contract()
    request_id = contract['requestIdField']
    input_field = contract['inputField']
    return {
        'type': 'object',
        'required': [request_id, input_field],
        'properties': {
            request_id: {
                'type': 'string',
                'minLength': 1,
                'maxLength': contract['requestIdMaxLength'],
            },
            input_field: {
                'type': 'string',
                'minLength': 1,
                'maxLength': contract['inputMaxLength'],
            },
        },
    }


__all__ = [
    'MAX_HUMAN_INPUT_LENGTH',
    'MAX_HUMAN_GATE_REQUEST_ID_LENGTH',
    'human_approval_request_schema',
    'human_gate_request_contract',
    'human_input_request_schema',
]

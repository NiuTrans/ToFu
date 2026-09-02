"""Published policy metadata and OpenAPI schemas for mutations."""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.mutation_payload_fields import (
    mutation_payload_field_contract,
)
from lib.orchestration.mutation_response import mutation_reason_http_status
from lib.orchestration.mutation_result import (
    MUTATION_ACCEPTED,
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_ACTION_TRANSITION_RUN,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_FORMAT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_RETRYABLE_REASONS,
    MUTATION_TERMINAL,
    MUTATION_TRANSPORT_FAILED,
)


def mutation_contract() -> dict:
    reasons = [
        MUTATION_ACCEPTED,
        MUTATION_NOT_FOUND,
        MUTATION_TERMINAL,
        MUTATION_ACTIVE,
        MUTATION_CONFLICT,
        MUTATION_PERSISTENCE_FAILED,
    ]
    payload_fields = mutation_payload_field_contract()
    return {
        'format': MUTATION_FORMAT,
        'actions': [
            MUTATION_ACTION_ABORT_RUN,
            MUTATION_ACTION_DELETE_RUN,
            MUTATION_ACTION_APPROVE_GATE,
            MUTATION_ACTION_INPUT_GATE,
            MUTATION_ACTION_TRANSITION_RUN,
        ],
        'reasons': reasons,
        'retryableReasons': sorted(MUTATION_RETRYABLE_REASONS),
        'transportFailureReason': MUTATION_TRANSPORT_FAILED,
        'clientRetryableReasons': sorted({
            *MUTATION_RETRYABLE_REASONS,
            MUTATION_TRANSPORT_FAILED,
        }),
        'httpStatusByReason': {
            reason: (
                200
                if reason == MUTATION_ACCEPTED
                else mutation_reason_http_status(reason)
            )
            for reason in reasons
        },
        'reconcileField': payload_fields['reconcileRequired']['name'],
        'targetExistsField': payload_fields['targetExists']['name'],
        'resourceTerminalField': payload_fields['resourceTerminal']['name'],
        'payloadFields': payload_fields,
    }


def mutation_contract_schema() -> dict:
    return contract_snapshot_schema(mutation_contract())


def mutation_payload_schema(
    action: str | None = None,
    reasons: list[str] | None = None,
) -> dict:
    contract = mutation_contract()
    actions = [action] if action else contract['actions']
    reason_values = list(reasons or contract['reasons'])
    field_specs = contract['payloadFields']
    properties = {}
    for semantic, spec in field_specs.items():
        field_type = spec['type']
        properties[spec['name']] = {
            'type': ['boolean', 'null']
            if field_type == 'nullable_boolean' else field_type,
        }
    properties[field_specs['format']['name']]['enum'] = [contract['format']]
    properties[field_specs['action']['name']]['enum'] = actions
    properties[field_specs['reason']['name']]['enum'] = reason_values
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': [spec['name'] for spec in field_specs.values()],
        'properties': properties,
    }


def mutation_response_schema(
    action: str,
    reasons: list[str],
) -> dict:
    accepted = reasons == [MUTATION_ACCEPTED]
    properties = {
        'ok': {'type': 'boolean', 'const': accepted},
        'request_id': {'type': 'string'},
        'mutation': mutation_payload_schema(action, reasons),
        'error': {'type': ['string', 'object']},
    }
    return {
        'type': 'object',
        'required': ['ok', 'mutation'] + ([] if accepted else ['error']),
        'properties': properties,
    }


__all__ = [
    'mutation_contract',
    'mutation_contract_schema',
    'mutation_payload_schema',
    'mutation_response_schema',
]

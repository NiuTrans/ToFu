"""JSON/OpenAPI schemas derived from the durable-run field registry."""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.durable_run_field_registry import (
    durable_run_contract,
    durable_run_list_envelope_contract,
)


def durable_run_contract_schema() -> dict:
    """Describe the durable snapshot field registry from its live contract."""
    return contract_snapshot_schema(durable_run_contract())


def durable_run_schema(*, detail: bool = False) -> dict:
    """Describe one persisted run from the executable field registry."""
    from lib.orchestration.run_status import run_status_contract
    from lib.orchestration.outcome_contract import outcome_payload_schema

    contract = durable_run_contract()
    fields = contract['readFields'] if detail else contract['listFields']
    properties = {
        'id': {'type': 'string', 'minLength': 1},
        'orch_id': {'type': 'string'},
        'name': {'type': 'string'},
        'status': {
            'type': 'string', 'enum': run_status_contract()['statuses'],
        },
        'terminal': {'type': 'boolean'},
        'final': {'type': 'string'},
        'error': {'type': ['string', 'object', 'null']},
        'created_by': {'type': 'string'},
        'created_at': {'type': 'integer', 'minimum': 0},
        'updated_at': {'type': 'integer', 'minimum': 0},
        'finished_at': {'type': 'integer', 'minimum': 0},
        'definition': {'type': 'object'},
        'input': {'type': 'string'},
        contract['outcomeField']: outcome_payload_schema(),
    }
    return {
        'type': 'object',
        'required': list(fields),
        'properties': {
            field: properties[field]
            for field in [*fields, *contract['optionalFields']]
        },
    }


def durable_run_list_response_schema() -> dict:
    """Describe the durable-run collection response envelope."""
    envelope = durable_run_list_envelope_contract()
    items_field = envelope['itemsField']
    page_field = envelope['pageField']
    return {
        'type': 'object',
        'required': ['ok', items_field, page_field],
        'properties': {
            'ok': {'type': 'boolean', 'const': True},
            'request_id': {'type': 'string'},
            items_field: {
                'type': 'array', 'items': durable_run_schema(),
            },
            page_field: {
                'type': 'object',
                'additionalProperties': False,
                'required': list(envelope['pageFields']),
                'properties': {
                    envelope['limitField']: {
                        'type': 'integer', 'minimum': 1,
                        'maximum': envelope['maxLimit'],
                    },
                    envelope['hasMoreField']: {'type': 'boolean'},
                    envelope['nextLimitField']: {
                        'type': ['integer', 'null'], 'minimum': 1,
                        'maximum': envelope['maxLimit'],
                    },
                },
            },
        },
    }


def durable_run_read_response_schema() -> dict:
    """Describe one durable-run detail response envelope."""
    return {
        'type': 'object',
        'required': ['ok', 'run'],
        'properties': {
            'ok': {'type': 'boolean', 'const': True},
            'request_id': {'type': 'string'},
            'run': durable_run_schema(detail=True),
        },
    }


def durable_replay_response_schema(*, missing: bool = False) -> dict:
    """Describe durable success and missing pages via the replay protocol."""
    from lib.task_replay import task_replay_contract, task_replay_response_schema

    if missing:
        return task_replay_response_schema(
            missing=True,
            snapshot_schema=durable_run_schema(detail=True),
            message_required=True,
        )
    caught_up_field = task_replay_contract()['caughtUpField']
    return task_replay_response_schema(
        snapshot_schema=durable_run_schema(detail=True),
        extra_properties={caught_up_field: {'type': 'boolean'}},
        extra_required=(caught_up_field,),
    )


__all__ = [
    'durable_run_contract_schema', 'durable_run_schema',
    'durable_run_list_response_schema', 'durable_run_read_response_schema',
    'durable_replay_response_schema',
]

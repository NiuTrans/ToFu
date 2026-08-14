"""Endpoint-specific HTTP compatibility over canonical mutation results."""

from __future__ import annotations

import copy

from lib.orchestration.human_gate_request_contract import (
    human_gate_request_contract,
)
from lib.orchestration.mutation_contract import mutation_contract
from lib.orchestration.mutation_result import (
    MUTATION_ACCEPTED,
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_APPROVE_GATE,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_INPUT_GATE,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    OrchestrationMutationResult,
)


_GATE = human_gate_request_contract()
_MUTATION = mutation_contract()
_RUN_ID, _GATE_ID = _MUTATION['legacyTargetFields']
_RUN_STATUS, _STATUS = _MUTATION['legacyStatusFields']

_ENDPOINTS = {
    'human-approve': {
        'action': MUTATION_ACTION_APPROVE_GATE,
        'compatibility': {
            _GATE_ID: {'type': 'string'},
            _GATE['approvalField']: {'type': 'boolean'},
        },
        'outcomes': [[MUTATION_ACCEPTED], [MUTATION_NOT_FOUND]],
        'badRequest': True,
    },
    'human-input': {
        'action': MUTATION_ACTION_INPUT_GATE,
        'compatibility': {_GATE_ID: {'type': 'string'}},
        'outcomes': [[MUTATION_ACCEPTED], [MUTATION_NOT_FOUND]],
        'badRequest': True,
    },
    'task-abort': {
        'action': MUTATION_ACTION_ABORT_RUN,
        'compatibility': {
            _RUN_ID: {'type': 'string'},
            _STATUS: {'type': 'string'},
            _RUN_STATUS: {'type': 'string'},
        },
        'outcomes': [
            [MUTATION_ACCEPTED],
            [MUTATION_NOT_FOUND],
            [MUTATION_TERMINAL, MUTATION_CONFLICT],
            [MUTATION_PERSISTENCE_FAILED],
        ],
    },
    'task-remove': {
        'action': MUTATION_ACTION_DELETE_RUN,
        'compatibility': {_RUN_STATUS: {'type': 'string'}},
        'outcomes': [
            [MUTATION_ACCEPTED],
            [MUTATION_NOT_FOUND],
            [MUTATION_ACTIVE],
            [MUTATION_PERSISTENCE_FAILED],
        ],
    },
    'run-abort': {
        'action': MUTATION_ACTION_ABORT_RUN,
        'compatibility': {
            _STATUS: {'type': 'string'},
            'note': {'type': 'string'},
        },
        'outcomes': [
            [MUTATION_ACCEPTED],
            [MUTATION_NOT_FOUND],
            [MUTATION_TERMINAL, MUTATION_CONFLICT],
        ],
    },
}


def mutation_endpoint_contract(endpoint: str) -> dict:
    try:
        return copy.deepcopy(_ENDPOINTS[endpoint])
    except KeyError as exc:
        raise ValueError(
            f'unknown orchestration mutation {endpoint!r}') from exc


def mutation_endpoint_contracts() -> dict[str, dict]:
    return {
        endpoint: copy.deepcopy(contract)
        for endpoint, contract in _ENDPOINTS.items()
    }


def mutation_endpoint_compatibility(
    endpoint: str,
    result: OrchestrationMutationResult,
    *,
    target_id: str = '',
    approved: bool = False,
) -> dict:
    """Project additive rolling-client fields for one mutation endpoint."""
    mutation_endpoint_contract(endpoint)
    target = str(target_id or result.target_id or '')
    status = str(result.run_status or '')
    if endpoint == 'human-approve':
        return {_GATE_ID: target, _GATE['approvalField']: bool(approved)}
    if endpoint == 'human-input':
        return {_GATE_ID: target}
    if endpoint == 'task-abort':
        return {_RUN_ID: target, _STATUS: status, _RUN_STATUS: status}
    if endpoint == 'task-remove':
        return {_RUN_STATUS: status}
    compatibility = {_STATUS: status}
    if result.canonical_reason == MUTATION_TERMINAL:
        compatibility['note'] = 'already finished'
    return compatibility


__all__ = [
    'mutation_endpoint_compatibility',
    'mutation_endpoint_contract',
    'mutation_endpoint_contracts',
]

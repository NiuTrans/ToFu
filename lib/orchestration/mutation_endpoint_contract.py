"""Endpoint-specific outcome policy for canonical mutation results."""

from __future__ import annotations

import copy

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
)


_ENDPOINTS = {
    'human-approve': {
        'action': MUTATION_ACTION_APPROVE_GATE,
        'outcomes': [[MUTATION_ACCEPTED], [MUTATION_NOT_FOUND]],
        'badRequest': True,
    },
    'human-input': {
        'action': MUTATION_ACTION_INPUT_GATE,
        'outcomes': [[MUTATION_ACCEPTED], [MUTATION_NOT_FOUND]],
        'badRequest': True,
    },
    'task-abort': {
        'action': MUTATION_ACTION_ABORT_RUN,
        'outcomes': [
            [MUTATION_ACCEPTED],
            [MUTATION_NOT_FOUND],
            [MUTATION_TERMINAL, MUTATION_CONFLICT],
            [MUTATION_PERSISTENCE_FAILED],
        ],
    },
    'task-remove': {
        'action': MUTATION_ACTION_DELETE_RUN,
        'outcomes': [
            [MUTATION_ACCEPTED],
            [MUTATION_NOT_FOUND],
            [MUTATION_ACTIVE],
            [MUTATION_PERSISTENCE_FAILED],
        ],
    },
    'run-abort': {
        'action': MUTATION_ACTION_ABORT_RUN,
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


__all__ = [
    'mutation_endpoint_contract',
    'mutation_endpoint_contracts',
]

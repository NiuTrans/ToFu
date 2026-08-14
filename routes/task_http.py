"""Shared HTTP ingress and projection for versioned task replay pages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.api_response import api_payload
from lib.task_replay import (
    safe_replay_cursor,
    task_replay_http_status,
    task_replay_request_contract,
)


def task_replay_parameters() -> list[dict]:
    """Publish the query contract implemented by :func:`task_replay_cursor`."""
    contract = task_replay_request_contract()
    return [{
        'name': contract['queryField'],
        'in': 'query',
        'schema': {
            'type': 'integer',
            'minimum': contract['minimum'],
            'default': contract['default'],
        },
        'description': contract['description'],
    }]


def task_replay_cursor(args: Mapping[str, Any]) -> int:
    """Normalize one untrusted replay cursor through the shared protocol."""
    return safe_replay_cursor(
        args.get(task_replay_request_contract()['queryField']))


def task_replay_response(payload: dict):
    """Map every live/durable replay page through one HTTP status boundary."""
    return api_payload(payload, task_replay_http_status(payload))


__all__ = [
    'task_replay_parameters',
    'task_replay_cursor',
    'task_replay_response',
]

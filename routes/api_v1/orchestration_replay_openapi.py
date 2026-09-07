"""Shared OpenAPI projection for live and durable task replay pages."""

from __future__ import annotations

from lib.task_replay import (
    live_task_replay_response_schema as orchestration_live_replay_response_schema,
    task_replay_contract,
    task_replay_event_schema as orchestration_replay_event_schema,
    task_replay_response_schema,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_json_response,
)


def task_replay_route_responses(
    success_schema: dict,
    missing_schema: dict,
) -> dict:
    """Describe shared replay HTTP semantics from the executable contract."""
    statuses = task_replay_contract()['httpStatuses']
    return orchestration_api_responses({
        str(statuses['success']): orchestration_json_response(
            'Cursor replay page', success_schema),
        str(statuses['notFound']): orchestration_json_response(
            'Task or run not found', missing_schema),
    }, 401, 403, statuses['failure'])


def orchestration_live_replay_responses() -> dict:
    return task_replay_route_responses(
        orchestration_live_replay_response_schema(),
        orchestration_live_replay_response_schema(missing=True),
    )


__all__ = [
    'orchestration_replay_event_schema',
    'task_replay_response_schema', 'task_replay_route_responses',
    'orchestration_live_replay_response_schema',
    'orchestration_live_replay_responses',
]

"""OpenAPI projection for durable-run list, detail and replay reads."""

from __future__ import annotations

from lib.orchestration.durable_run_wire_schema import (
    durable_replay_response_schema,
    durable_run_list_response_schema,
    durable_run_read_response_schema,
    durable_run_schema,
)

from .orchestration_openapi import (
    orchestration_api_responses,
    orchestration_json_response,
)
from .orchestration_replay_openapi import (
    task_replay_route_responses,
)


def durable_task_route_responses(operation: str) -> dict:
    if operation == 'list':
        return orchestration_api_responses({
            '200': orchestration_json_response(
                'Durable orchestration run headers',
                durable_run_list_response_schema(),
            ),
        }, 400, 401, 403, 500)
    if operation == 'read':
        return orchestration_api_responses({
            '200': orchestration_json_response(
                'Durable orchestration run detail',
                durable_run_read_response_schema(),
            ),
        }, 401, 403, 404, 500)
    if operation == 'replay':
        return task_replay_route_responses(
            durable_replay_response_schema(),
            durable_replay_response_schema(missing=True),
        )
    raise ValueError(f'unknown durable task operation {operation!r}')


def durable_task_route_response_registry() -> dict[str, dict]:
    return {
        operation: durable_task_route_responses(operation)
        for operation in ('list', 'read', 'replay')
    }


__all__ = [
    'durable_run_schema', 'durable_run_list_response_schema',
    'durable_run_read_response_schema', 'durable_replay_response_schema',
    'durable_task_route_responses', 'durable_task_route_response_registry',
]

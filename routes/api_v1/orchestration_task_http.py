"""Shared HTTP ingress and projection for durable orchestration reads."""

from __future__ import annotations

from lib.api_response import api_not_found, api_ok
from lib.orchestration.application_result_ports import DurableReplayResultPort
from lib.task_replay import missing_replay_page
from routes.task_http import (
    task_replay_cursor,
    task_replay_parameters,
    task_replay_response,
)


# Stable orchestration-facing names over the shared replay ingress contract.
# These are aliases, not forwarding wrappers, so cursor parsing and OpenAPI
# parameters have one implementation for both live and durable task routes.
durable_replay_cursor = task_replay_cursor
durable_replay_parameters = task_replay_parameters


def durable_run_entry_response(run: dict | None):
    if run is None:
        return api_not_found('Run not found')
    return api_ok(run=run)


def durable_replay_response(
    replay: DurableReplayResultPort | None,
    cursor: int,
):
    payload = (
        replay.payload() if replay is not None
        else missing_replay_page(cursor).payload(
            {'message': 'Run not found'})
    )
    return task_replay_response(payload)


__all__ = [
    'durable_replay_parameters',
    'durable_replay_cursor',
    'durable_run_entry_response',
    'durable_replay_response',
]

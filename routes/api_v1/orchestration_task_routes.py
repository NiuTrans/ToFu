"""HTTP adapter for durable orchestration runs (Task Mode).

The shared Blueprint is composed in ``orchestrations.py``; definition and
ephemeral Studio adapters are split into sibling registration modules. This
module registers the durable endpoints against injected application-service
providers, preserving one URL contract without a second persistence path.
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.openapi import api_meta
from lib.orchestration.application_provider_ports import (
    DefinitionResolver,
    RunServiceProvider,
    RuntimeStartServiceProvider,
)
from lib.request_parser import parse_body

from .auth import current_auth, require_auth
from .orchestration_definition_request_http import (
    definition_selection_request_schema,
)
from .orchestration_endpoint_routes import orchestration_route
from .orchestration_run_openapi import run_start_responses
from .orchestration_runtime_start_http import runtime_start_request_response
from .orchestration_request_http import orchestration_request_response
from .orchestration_service_http import orchestration_service_response
from .orchestration_task_http import (
    durable_replay_cursor,
    durable_replay_parameters,
    durable_replay_response,
    durable_run_entry_response,
)
from .orchestration_task_list_http import (
    durable_run_list_parameters,
    durable_run_list_response,
    prepare_durable_run_list_query,
)
from .orchestration_task_openapi import durable_task_route_response_registry

_RUN_START_SCHEMA = definition_selection_request_schema(include_input=True)
_RUN_START_RESPONSES = run_start_responses('durable')
_RUN_LIST_PARAMETERS = durable_run_list_parameters()
_REPLAY_PARAMETERS = durable_replay_parameters()
_TASK_READ_RESPONSES = durable_task_route_response_registry()


def _created_by() -> str:
    ctx = current_auth()
    return getattr(ctx, 'key_id', '') if ctx else ''


def register_orchestration_task_routes(
    blueprint: Blueprint,
    *,
    resolve_definition: DefinitionResolver,
    run_service: RunServiceProvider,
    runtime_start_service: RuntimeStartServiceProvider,
) -> None:
    """Attach durable-run endpoints to the shared orchestration Blueprint.

    Providers are invoked at request/worker time so configuration reloads and
    test replacements never get trapped behind import-time service instances.
    """

    @orchestration_route(blueprint, 'task-create')
    @require_auth
    @api_meta(
        summary='Create a durable orchestration run instance (Task Mode)',
        description='Validates then runs the flow as a DURABLE, reopenable run. '
                    'Unlike /run, the definition snapshot + every event are '
                    'persisted to the DB. Pass an inline "definition" or a stored '
                    '"id", plus an optional "input". Returns {ok, run_id}; poll '
                    '/tasks/<run_id>/events for streamed + replayable events.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _RUN_START_SCHEMA,
        }}},
        responses=_RUN_START_RESPONSES,
    )
    def create_run_task():
        return runtime_start_request_response(
            'api_v1.orchestrations.start_task',
            'durable',
            parse_body(),
            resolve_definition=resolve_definition,
            runtime_start_service=runtime_start_service,
            created_by=_created_by(),
        )

    @orchestration_route(blueprint, 'task-list')
    @require_auth
    @api_meta(
        summary='List durable run instances',
        description='Returns run headers (newest first), without the definition '
                    'blob. Supports status/orchestration filters and a bounded '
                    'newest-first limit.',
        tags=['orchestrations'],
        parameters=_RUN_LIST_PARAMETERS,
        responses=_TASK_READ_RESPONSES['list'],
    )
    def list_run_tasks():
        return orchestration_request_response(
            prepare_durable_run_list_query(request.args),
            lambda query: orchestration_service_response(
                'api_v1.orchestrations.list_runs',
                lambda: run_service().list(
                    status=query.status,
                    orch_id=query.orchestration_id,
                    limit=query.probe_limit,
                ),
                lambda runs: durable_run_list_response(runs, query.limit),
            ),
        )

    @orchestration_route(blueprint, 'task-read')
    @require_auth
    @api_meta(
        summary='Fetch one durable run instance (header + definition)',
        tags=['orchestrations'],
        responses=_TASK_READ_RESPONSES['read'],
    )
    def get_run_task(run_id):
        return orchestration_service_response(
            'api_v1.orchestrations.get_run',
            lambda: run_service().get(run_id),
            lambda run: durable_run_entry_response(run),
        )

    @orchestration_route(blueprint, 'task-events')
    @require_auth
    @api_meta(
        summary='Durable cursor replay of a run\'s events',
        description='Returns persisted events with seq >= cursor. Survives reload '
                    'and server restart (unlike the in-memory /run/poll). '
                    'Uses tofu.task-replay/v1 and clamps future cursors to the '
                    'authoritative durable-log boundary. Bounded pages expose '
                    'caught_up=false until the cursor reaches that boundary. '
                    'Terminal pages include the canonical run snapshot only '
                    'after caught_up, already read for lifecycle '
                    'projection, avoiding a second header request.',
        tags=['orchestrations'],
        parameters=_REPLAY_PARAMETERS,
        responses=_TASK_READ_RESPONSES['replay'],
    )
    def get_run_task_events(run_id):
        cursor = durable_replay_cursor(request.args)
        return orchestration_service_response(
            'api_v1.orchestrations.replay_run',
            lambda: run_service().replay(run_id, cursor),
            lambda replay: durable_replay_response(replay, cursor),
        )

__all__ = ['register_orchestration_task_routes']

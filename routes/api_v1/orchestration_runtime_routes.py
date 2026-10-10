"""HTTP adapter for ephemeral orchestration plan, start and poll reads."""

from __future__ import annotations

from quart import Blueprint

from lib.openapi import api_meta
from lib.orchestration.application_provider_ports import (
    AuthoringServiceProvider,
    DefinitionResolver,
    RuntimeStartServiceProvider,
)
from lib.request_parser import parse_body
from lib.task_runtime_ports import TaskRouteRuntimePort
from routes._task_routes import register_task_routes

from .auth import require_auth
from .orchestration_authoring_http import authoring_plan_response
from .orchestration_authoring_action_openapi import authoring_action_responses
from .orchestration_definition_request_http import (
    definition_selection_request_schema,
    resolve_definition_request,
)
from .orchestration_endpoint_routes import (
    orchestration_endpoint_extensions,
    orchestration_endpoint_path,
    orchestration_route,
)
from .orchestration_run_openapi import run_start_responses
from .orchestration_runtime_start_http import runtime_start_request_response
from .orchestration_replay_openapi import orchestration_live_replay_responses
from .orchestration_request_http import orchestration_request_response
from .orchestration_service_http import orchestration_service_response

_DEFINITION_SELECTION_SCHEMA = definition_selection_request_schema()
_RUN_START_SCHEMA = definition_selection_request_schema(include_input=True)
_RUN_START_RESPONSES = run_start_responses('ephemeral')
_PLAN_RESPONSES = authoring_action_responses('plan')
_LIVE_REPLAY_RESPONSES = orchestration_live_replay_responses()


def register_orchestration_runtime_routes(
    blueprint: Blueprint,
    runtime: TaskRouteRuntimePort,
    *,
    resolve_definition: DefinitionResolver,
    authoring_service: AuthoringServiceProvider,
    runtime_start_service: RuntimeStartServiceProvider,
) -> None:
    """Register ephemeral run endpoints against shared runtime/services."""

    @orchestration_route(blueprint, 'plan')
    @require_auth
    @api_meta(
        summary='Dry-run a definition (no agents run)',
        description='Returns the ordered execution steps a run would take, '
                    'without invoking any LLM/agent. {ok, steps, error}.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _DEFINITION_SELECTION_SCHEMA,
        }}},
        responses=_PLAN_RESPONSES,
    )
    def plan_orchestration():
        def handle(resolved):
            definition = resolved.definition
            assert isinstance(definition, dict)
            return orchestration_service_response(
                'api_v1.orchestrations.plan',
                lambda: authoring_service().plan(definition),
                lambda result: authoring_plan_response(
                    result.plan,
                    result.inspection,
                    definition_source=resolved.source,
                ),
            )

        return orchestration_request_response(
            resolve_definition_request(
                parse_body(), resolve_definition=resolve_definition,
            ),
            handle,
        )

    @orchestration_route(blueprint, 'run-start')
    @require_auth
    @api_meta(
        summary='Execute an orchestration (background task)',
        description='Validates then runs the flow on a background task. '
                    'Returns {task_id}; poll /run/poll/<task_id> for streamed '
                    'events and the final result. Pass an inline "definition" '
                    'or a stored "id", plus an optional "input" string.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _RUN_START_SCHEMA,
        }}},
        responses=_RUN_START_RESPONSES,
    )
    def run_orchestration():
        return runtime_start_request_response(
            'api_v1.orchestrations.start_run',
            'ephemeral',
            parse_body(),
            resolve_definition=resolve_definition,
            runtime_start_service=runtime_start_service,
        )

    register_task_routes(
        blueprint,
        runtime,
        url_prefix='',
        enable_abort=False,
        poll_path=orchestration_endpoint_path('run-poll', method='GET'),
        poll_responses=_LIVE_REPLAY_RESPONSES,
        poll_extensions=orchestration_endpoint_extensions(
            'run-poll', method='GET'),
        route_decorators=(require_auth,),
        tags=('orchestrations',),
    )
__all__ = ['register_orchestration_runtime_routes']

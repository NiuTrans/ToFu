"""HTTP adapter for ephemeral and durable orchestration mutations."""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger
from lib.openapi import api_meta
from lib.orchestration.application_provider_ports import (
    HumanGateServiceProvider,
    RunServiceProvider,
    RuntimeMutationServiceProvider,
)
from lib.request_parser import parse_body
from lib.task_runtime_ports import TaskRouteRuntimePort
from routes._task_routes import register_task_routes

from .auth import request_user_id, require_auth
from .orchestration_mutation_http import (
    human_approval_request_schema,
    human_input_request_schema,
    prepare_human_approval_request,
    prepare_human_input_request,
)
from .orchestration_endpoint_routes import (
    orchestration_endpoint_extensions,
    orchestration_endpoint_path,
    orchestration_route,
)
from .orchestration_mutation_openapi import mutation_route_response_registry
from .orchestration_mutation_service_http import (
    orchestration_mutation_service_response,
)
from .orchestration_request_http import orchestration_request_response

logger = get_logger(__name__)

_HUMAN_APPROVAL_SCHEMA = human_approval_request_schema()
_HUMAN_INPUT_SCHEMA = human_input_request_schema()
_MUTATION_RESPONSES = mutation_route_response_registry()


def register_orchestration_mutation_routes(
    blueprint: Blueprint,
    runtime: TaskRouteRuntimePort,
    *,
    run_service: RunServiceProvider,
    runtime_mutation_service: RuntimeMutationServiceProvider,
    human_gate_service: HumanGateServiceProvider,
) -> None:
    """Register the single HTTP boundary for every run-state mutation."""

    @orchestration_route(blueprint, 'human-approve')
    @require_auth
    @api_meta(
        summary='Resolve a human approval gate in a running flow',
        description='Unblocks a flow paused on a human node with mode=approve. '
                    'Body: {requestId, approved}. Reuses the chat '
                    'write-approval primitive.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _HUMAN_APPROVAL_SCHEMA,
        }}},
        responses=_MUTATION_RESPONSES['human-approve'],
    )
    def orchestration_human_approve():
        def handle(prepared):
            request_id = prepared.request_id
            approved = prepared.approved
            return orchestration_mutation_service_response(
                'api_v1.orchestrations.approve_gate',
                lambda: human_gate_service().approve(
                    request_id,
                    approved,
                    owner_user_id=request_user_id(),
                ),
                endpoint='human-approve',
                on_success=lambda: logger.info(
                    '[Orchestrations] human approve req=%s approved=%s',
                    request_id,
                    approved,
                ),
            )

        return orchestration_request_response(
            prepare_human_approval_request(parse_body()),
            handle,
        )

    @orchestration_route(blueprint, 'human-input')
    @require_auth
    @api_meta(
        summary='Resolve a human input gate in a running flow',
        description='Unblocks a flow paused on a human node with mode=input. '
                    'Body: {requestId, response}. Reuses the chat ask-human '
                    'primitive.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _HUMAN_INPUT_SCHEMA,
        }}},
        responses=_MUTATION_RESPONSES['human-input'],
    )
    def orchestration_human_input():
        def handle(prepared):
            request_id = prepared.request_id
            response_text = prepared.response_text
            return orchestration_mutation_service_response(
                'api_v1.orchestrations.input_gate',
                lambda: human_gate_service().input(
                    request_id,
                    response_text,
                    owner_user_id=request_user_id(),
                ),
                endpoint='human-input',
                on_success=lambda: logger.info(
                    '[Orchestrations] human input req=%s len=%d',
                    request_id,
                    len(response_text),
                ),
            )

        return orchestration_request_response(
            prepare_human_input_request(parse_body()),
            handle,
        )

    @orchestration_route(blueprint, 'task-abort')
    @require_auth
    @api_meta(
        summary='Abort a running durable run instance',
        tags=['orchestrations'],
        request_body=False,
        responses=_MUTATION_RESPONSES['task-abort'],
    )
    def abort_run_task(run_id):
        return orchestration_mutation_service_response(
            'api_v1.orchestrations.abort_run',
            lambda: run_service().abort(run_id),
            endpoint='task-abort',
            on_success=lambda: logger.info(
                '[Orchestrations] task run ABORT run=%s', run_id),
        )

    @orchestration_route(blueprint, 'task-remove')
    @require_auth
    @api_meta(
        summary='Delete a durable run instance and its events',
        tags=['orchestrations'],
        responses=_MUTATION_RESPONSES['task-remove'],
    )
    def delete_run_task(run_id):
        return orchestration_mutation_service_response(
            'api_v1.orchestrations.delete_run',
            lambda: run_service().delete(run_id),
            endpoint='task-remove',
            on_success=lambda: logger.info(
                '[Orchestrations] task run DELETE run=%s', run_id),
        )

    def abort_runtime_task(task_id: str, owner_user_id: int):
        return orchestration_mutation_service_response(
            'api_v1.orchestrations.abort_runtime',
            lambda: runtime_mutation_service(owner_user_id).abort(task_id),
            endpoint='run-abort',
        )

    register_task_routes(
        blueprint,
        runtime,
        url_prefix='',
        enable_poll=False,
        abort_path=orchestration_endpoint_path('run-abort', method='POST'),
        abort_handler=abort_runtime_task,
        route_decorators=(require_auth,),
        tags=('orchestrations',),
        abort_responses=_MUTATION_RESPONSES['run-abort'],
        abort_extensions=orchestration_endpoint_extensions(
            'run-abort', method='POST'),
    )


__all__ = ['register_orchestration_mutation_routes']

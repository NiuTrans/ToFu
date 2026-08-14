"""HTTP adapter for pure orchestration authoring operations.

Validation, Composer, built-ins, the Studio contract and layout do not mutate
the definition repository.  Keeping them outside the CRUD adapter makes that
boundary explicit while reusing the composition root's shared resolver.
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.api_response import api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.orchestration.application_provider_ports import (
    AuthoringServiceProvider,
    DefinitionResolver,
)
from lib.orchestration.definition_wire_contracts import (
    definition_candidate_schema,
)
from lib.request_parser import parse_body

from .auth import require_auth
from .orchestration_authoring_http import (
    authoring_builtin_response,
    authoring_compose_response,
    authoring_definition_response,
    compose_request_schema,
    prepare_compose_request,
    role_contract_parameters,
    role_contract_query,
)
from .orchestration_authoring_openapi import (
    authoring_route_response_registry,
)
from .orchestration_definition_request_http import (
    definition_selection_request_schema,
    resolve_definition_request,
)
from .orchestration_endpoint_routes import orchestration_route
from .orchestration_request_http import orchestration_request_response
from .orchestration_service_http import orchestration_service_response

logger = get_logger(__name__)

_VALIDATION_SCHEMA = definition_candidate_schema()
_COMPOSE_SCHEMA = compose_request_schema()
_DEFINITION_SELECTION_SCHEMA = definition_selection_request_schema()
_ROLE_CONTRACT_PARAMETERS = role_contract_parameters()
_AUTHORING_RESPONSES = authoring_route_response_registry()


def register_orchestration_authoring_routes(
    blueprint: Blueprint,
    *,
    authoring_service: AuthoringServiceProvider,
    resolve_definition: DefinitionResolver,
) -> None:
    """Register repository-free Studio authoring routes on ``blueprint``."""

    @orchestration_route(blueprint, 'validation')
    @require_auth
    @api_meta(
        summary='Validate a definition without saving',
        description='Runs lib.orchestration.validate_definition and returns '
                    'a versioned inspection with diagnostics, contract and '
                    'rolling-client errors/warnings fields.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _VALIDATION_SCHEMA}}},
        responses=_AUTHORING_RESPONSES['validation'],
    )
    def validate_orchestration():
        return orchestration_service_response(
            'api_v1.orchestrations.validate',
            lambda: authoring_service().inspect(parse_body()),
            api_ok,
        )

    @orchestration_route(blueprint, 'compose')
    @require_auth
    @api_meta(
        summary='Compose / edit a definition from natural language',
        description='LLM turns a NL requirement (+ optional current graph + '
                    'chat history) into a validated, auto-laid-out definition. '
                    'Returns {ok, reply, definition, validation}.',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _COMPOSE_SCHEMA,
        }}},
        responses=_AUTHORING_RESPONSES['compose'],
    )
    def compose_orchestration():
        def handle(prepared):
            def project_result(result):
                logger.info(
                    '[Orchestrations] compose ok=%s nodes=%s',
                    result.get('ok'),
                    len((result.get('definition') or {}).get('nodes') or []),
                )
                return authoring_compose_response(result)

            return orchestration_service_response(
                'api_v1.orchestrations.compose',
                lambda: authoring_service().compose(
                    prepared.requirement,
                    current=prepared.current,
                    history=prepared.history,
                ),
                project_result,
            )

        return orchestration_request_response(
            prepare_compose_request(parse_body()), handle,
        )

    @orchestration_route(blueprint, 'builtin')
    @require_auth
    @api_meta(
        summary='Get a built-in canonical flow definition',
        description='Returns a server-authored reference flow (e.g. the '
                    'canonical endpoint loop) as a tofu.orchestration/v1 '
                    'definition. The backend is the single source of truth '
                    'for these shapes.',
        tags=['orchestrations'],
        responses=_AUTHORING_RESPONSES['builtin'],
    )
    def builtin_orchestration(name):
        return orchestration_service_response(
            'api_v1.orchestrations.builtin',
            lambda: authoring_service().builtin_inspection(name),
            lambda result: authoring_builtin_response(result, name=name),
        )

    @orchestration_route(blueprint, 'authoring-contract')
    @require_auth
    @api_meta(
        summary='Get the Orchestration Studio authoring contract',
        description='Returns the backend-owned role/control FieldSpecs, '
                    'personas, default emits, built-ins and Typed I/O metadata '
                    'used by the Studio. Field labels are i18n keys resolved '
                    'by the frontend.',
        tags=['orchestrations'],
        responses=_AUTHORING_RESPONSES['authoring-contract'],
    )
    def authoring_contract_orchestration():
        return orchestration_service_response(
            'api_v1.orchestrations.authoring_contract',
            lambda: authoring_service().contract(),
            api_ok,
        )

    @orchestration_route(blueprint, 'role-schema')
    @require_auth
    @api_meta(
        summary='Get one role schema (compatibility endpoint)',
        description='Query ?role=<name> for one role. Without a role this '
                    'legacy endpoint returns the same document as '
                    'authoring-contract. New Studio clients should use '
                    'authoring-contract.',
        tags=['orchestrations'],
        parameters=_ROLE_CONTRACT_PARAMETERS,
        responses=_AUTHORING_RESPONSES['role-schema'],
    )
    def role_schema_orchestration():
        role = role_contract_query(request.args)
        return orchestration_service_response(
            'api_v1.orchestrations.role_contract',
            lambda: authoring_service().role_contract(role),
            api_ok,
        )

    @orchestration_route(blueprint, 'layout')
    @require_auth
    @api_meta(
        summary='Auto-layout a definition (tidy node positions)',
        description='Runs lib.orchestration.layout_definition — BFS layering '
                    '+ barycenter crossing-minimization — and returns the '
                    'same definition with every node\'s pos recomputed into '
                    'clean top-down lanes. Pure: no agents run, nothing is '
                    'stored. Accepts an inline "definition" or a stored "id".',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _DEFINITION_SELECTION_SCHEMA,
        }}},
        responses=_AUTHORING_RESPONSES['layout'],
    )
    def layout_orchestration():
        def handle(resolved):
            definition = resolved.definition
            assert isinstance(definition, dict)
            return orchestration_service_response(
                'api_v1.orchestrations.layout',
                lambda: authoring_service().layout(definition),
                lambda arranged: authoring_definition_response(
                    arranged,
                    definition_source=resolved.source,
                ),
            )

        return orchestration_request_response(
            resolve_definition_request(
                parse_body(), resolve_definition=resolve_definition,
            ),
            handle,
        )


__all__ = ['register_orchestration_authoring_routes']

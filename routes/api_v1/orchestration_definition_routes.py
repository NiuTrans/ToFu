"""Thin HTTP adapter for orchestration definition persistence."""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger
from lib.openapi import api_meta
from lib.orchestration.application_provider_ports import DefinitionServiceProvider
from lib.orchestration.definition_contract_schema import (
    definition_request_schema,
)
from lib.request_parser import parse_body

from .auth import require_auth
from .orchestration_definition_http import (
    definition_entry_response,
    definition_list_response,
)
from .orchestration_definition_openapi import (
    definition_precondition_parameters,
    definition_route_response_registry,
)
from .orchestration_definition_request_http import definition_precondition
from .orchestration_request_http import orchestration_request_response
from .orchestration_endpoint_routes import orchestration_route
from .orchestration_definition_service_http import (
    orchestration_definition_delete_service_response,
    orchestration_definition_write_service_response,
)
from .orchestration_service_http import orchestration_service_response

logger = get_logger(__name__)

_DEF_SCHEMA = definition_request_schema()
_DEFINITION_PRECONDITION_PARAMETERS = definition_precondition_parameters()
_DEFINITION_RESPONSES = definition_route_response_registry()
def register_orchestration_definition_routes(
    blueprint: Blueprint,
    *,
    definition_service: DefinitionServiceProvider,
) -> None:
    """Register definition persistence routes on ``blueprint``."""

    @orchestration_route(blueprint, 'definition-list')
    @require_auth
    @api_meta(
        summary='List orchestration definitions',
        description='Returns all stored orchestration definitions with metadata.',
        tags=['orchestrations'],
        responses=_DEFINITION_RESPONSES['list'],
    )
    def list_orchestrations():
        # Coordinated bare-array migration (docs/API_CONTRACT.md §4): the
        # array moves under ``items``; the client retains an array fallback
        # for rolling-deploy skew.
        return orchestration_service_response(
            'api_v1.orchestrations.list',
            lambda: definition_service().list_summaries(),
            definition_list_response,
        )

    @orchestration_route(blueprint, 'definition-read')
    @require_auth
    @api_meta(
        summary='Get one orchestration definition',
        tags=['orchestrations'],
        responses=_DEFINITION_RESPONSES['read'],
    )
    def get_orchestration(orch_id):
        return orchestration_service_response(
            'api_v1.orchestrations.get',
            lambda: definition_service().get_entry(orch_id),
            definition_entry_response,
        )

    @orchestration_route(blueprint, 'definition-create')
    @require_auth
    @api_meta(
        summary='Create an orchestration definition',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _DEF_SCHEMA}}},
        responses=_DEFINITION_RESPONSES['create'],
    )
    def create_orchestration():
        return orchestration_definition_write_service_response(
            'api_v1.orchestrations.create',
            lambda: definition_service().create(parse_body()),
            endpoint='definition-create',
            on_success=lambda entry: logger.info(
                '[Orchestrations] created id=%s name=%r',
                entry['id'],
                entry.get('name'),
            ),
        )

    @orchestration_route(blueprint, 'definition-update')
    @require_auth
    @api_meta(
        summary='Replace an orchestration definition',
        tags=['orchestrations'],
        request_body={'required': True, 'content': {'application/json': {
            'schema': _DEF_SCHEMA}}},
        parameters=_DEFINITION_PRECONDITION_PARAMETERS,
        responses=_DEFINITION_RESPONSES['replace'],
    )
    def update_orchestration(orch_id):
        return orchestration_request_response(
            definition_precondition(),
            lambda expected_updated_at:
            orchestration_definition_write_service_response(
                'api_v1.orchestrations.update',
                lambda: definition_service().update(
                    orch_id,
                    parse_body(),
                    expected_updated_at=expected_updated_at,
                ),
                endpoint='definition-update',
                expected_updated_at=expected_updated_at,
                on_success=lambda entry: logger.info(
                    '[Orchestrations] updated id=%s name=%r',
                    orch_id,
                    entry.get('name'),
                ),
            ),
        )

    @orchestration_route(blueprint, 'definition-delete')
    @require_auth
    @api_meta(
        summary='Delete an orchestration definition',
        tags=['orchestrations'],
        parameters=_DEFINITION_PRECONDITION_PARAMETERS,
        responses=_DEFINITION_RESPONSES['delete'],
    )
    def delete_orchestration(orch_id):
        return orchestration_request_response(
            definition_precondition(),
            lambda expected_updated_at:
            orchestration_definition_delete_service_response(
                'api_v1.orchestrations.delete',
                lambda: definition_service().delete_if_current(
                    orch_id,
                    expected_updated_at=expected_updated_at,
                ),
                endpoint='definition-delete',
                expected_updated_at=expected_updated_at,
                on_success=lambda: logger.info(
                    '[Orchestrations] deleted id=%s', orch_id),
            ),
        )

__all__ = ['register_orchestration_definition_routes']

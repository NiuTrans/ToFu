"""Application-owned built-in inspection and role-contract selection."""

from __future__ import annotations

import pytest

import lib.orchestration.authoring_service as authoring_service_module
import lib.orchestration.authoring_operations as authoring_operations_module
import lib.orchestration.authoring_results as authoring_results_module
import lib.orchestration.service as service_facade

from lib.orchestration.authoring_service import (
    AuthoringServiceError,
    OrchestrationAuthoringService,
)


pytestmark = pytest.mark.unit


def test_builtin_definition_and_inspection_are_one_application_result():
    service = OrchestrationAuthoringService()

    result = service.builtin_inspection('endpoint')
    assert result.definition is not None
    assert result.definition['schema'] == 'tofu.orchestration/v1'
    assert result.inspection is not None
    assert result.inspection['ok'] is True

    missing = service.builtin_inspection('missing')
    assert missing.definition is None
    assert missing.inspection is None

    parameterized = service.build_builtin('endpoint', max_iterations=3)
    assert parameterized is not None
    loop = next(node for node in parameterized['nodes']
                if node.get('kind') == 'loop')
    assert loop['params']['max_iterations'] == 3


def test_role_contract_selection_is_owned_by_the_application_port():
    service = OrchestrationAuthoringService()

    assert service.role_contract('') == service.contract()
    assert service.role_contract('worker')['role'] == 'worker'


def test_composer_dependency_uses_shared_typed_service_boundary():
    failure = OSError('composer provider unavailable')

    def fail(*_args, **_kwargs):
        raise failure

    service = OrchestrationAuthoringService(composer=fail)

    with pytest.raises(
        AuthoringServiceError,
        match='failed to compose orchestration definition',
    ) as caught:
        service.compose('build a review flow')

    assert caught.value.__cause__ is failure


def test_authoring_routes_only_project_application_results():
    route_source = open(
        'routes/api_v1/orchestration_authoring_routes.py',
        encoding='utf-8',
    ).read()
    builtin_start = route_source.index('def builtin_orchestration')
    builtin_end = route_source.index(
        "@orchestration_route(blueprint, 'authoring-contract')",
        builtin_start,
    )
    builtin_route = route_source[builtin_start:builtin_end]
    role_start = route_source.index('def role_schema_orchestration')
    role_end = route_source.index(
        "@orchestration_route(blueprint, 'layout')",
        role_start,
    )
    role_route = route_source[role_start:role_end]

    assert 'authoring_service().builtin_inspection(name)' in builtin_route
    assert 'authoring_builtin_response(result, name=name)' in builtin_route
    assert 'result.definition' not in builtin_route
    assert '.inspect(' not in builtin_route
    assert 'service = authoring_service()' not in builtin_route
    assert 'authoring_service().role_contract(role)' in role_route
    assert 'if role else' not in role_route

    ports = open(
        'lib/orchestration/application_service_ports.py', encoding='utf-8',
    ).read()
    port_start = ports.index('class AuthoringServicePort')
    port_end = ports.index('\n\nclass DefinitionServicePort', port_start)
    canonical_port = ports[port_start:port_end]
    assert 'def builtin_inspection(' in canonical_port
    assert 'def builtin(' not in canonical_port


def test_rolling_service_facade_reexports_the_new_application_result():
    assert service_facade.AuthoringBuiltinResult is \
        authoring_service_module.AuthoringBuiltinResult
    assert service_facade.inspect_builtin_definition is \
        authoring_service_module.inspect_builtin_definition
    assert authoring_service_module.AuthoringBuiltinResult is \
        authoring_results_module.AuthoringBuiltinResult
    assert authoring_service_module.inspect_builtin_definition is \
        authoring_operations_module.inspect_builtin_definition
    assert authoring_service_module.compose_definition is \
        authoring_operations_module.compose_definition


def test_authoring_contract_value_sections_have_one_focused_owner():
    facade = open(
        'lib/orchestration/authoring_contract.py', encoding='utf-8').read()
    sections = open(
        'lib/orchestration/authoring_contract_sections.py',
        encoding='utf-8',
    ).read()
    schemas = open(
        'lib/orchestration/authoring_contract_schema.py',
        encoding='utf-8',
    ).read()
    builtins = open(
        'lib/orchestration/authoring_builtin_registry.py',
        encoding='utf-8',
    ).read()

    assert 'from lib.orchestration.authoring_contract_sections import (' \
        in facade
    assert 'def authoring_object_sections()' in sections
    assert 'def node_authoring_defaults()' in sections
    assert 'def role_schema_registry()' in sections
    assert 'def persona_registry()' in sections
    assert 'def authoring_object_sections()' not in facade
    assert 'def node_authoring_defaults()' not in facade
    assert 'from lib.orchestration.authoring_contract_schema import (' \
        in facade
    assert 'def authoring_contract_response_schema()' in schemas
    assert 'def authoring_object_section_schemas()' in schemas
    assert 'def authoring_contract_response_schema()' not in facade
    assert 'def build_builtin_definition(' in builtins
    assert 'def build_builtin_definition(' not in facade
    assert "'eventContract': runtime_event_contract()" in sections
    assert "'requestLimits': request_limits_contract()" in sections
    assert len(facade.splitlines()) < 100
    assert len(sections.splitlines()) < 170
    assert len(schemas.splitlines()) < 230
    assert len(builtins.splitlines()) < 50

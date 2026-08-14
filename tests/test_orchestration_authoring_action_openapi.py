"""Contract tests for Studio authoring-action response documentation."""

from __future__ import annotations

import pytest

from lib.orchestration.definition_inspection import inspect_definition
from lib.orchestration.definition_wire_contracts import (
    definition_candidate_schema,
    definition_request_schema,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_contract,
    inspection_response_fields,
)
from lib.orchestration.authoring_service import plan_authoring_definition
from routes.api_v1.orchestration_authoring_action_openapi import (
    authoring_action_response_registry,
    authoring_action_responses,
    compose_response_schema,
    definition_action_response_schema,
    inspection_response_schema,
    plan_response_schema,
)


pytestmark = pytest.mark.unit


def _definition() -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': 'Documented flow',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'worker', 'type': 'role', 'role': 'worker'},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'worker'},
            {'from': 'worker', 'to': 'stop'},
        ],
    }


def test_inspection_schema_derives_format_and_diagnostic_registry():
    import lib.orchestration.inspection_wire_contract as inspection_module
    import routes.api_v1.orchestration_authoring_action_openapi as openapi_module

    contract = inspection_contract()
    schema = inspection_response_schema()
    diagnostic = schema['properties']['diagnostics']['items']

    assert schema['properties']['format']['enum'] == [contract['format']]
    assert schema['required'] == contract['responseFields']
    assert diagnostic['required'] == contract['diagnosticFields']
    assert diagnostic['properties']['severity']['enum'] == \
        contract['diagnosticSeverities']
    assert schema['properties']['contract']['required'] == \
        contract['contractFields']
    contract['diagnosticFields'].clear()
    assert inspection_contract()['diagnosticFields']
    assert openapi_module.inspection_response_schema is \
        inspection_module.inspection_response_schema
    assert 'def inspection_response_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_validation_candidate_does_not_claim_a_persistable_draft_is_required():
    candidate = definition_candidate_schema()
    persisted = definition_request_schema()

    assert candidate == {
        'type': 'object',
        'additionalProperties': True,
        'description': 'Possibly incomplete orchestration draft to inspect.',
    }
    assert persisted['required'] == ['name', 'nodes', 'edges']


def test_action_schemas_match_live_projection_shapes():
    import lib.orchestration.authoring_action_wire_contracts as wire_module
    import routes.api_v1.orchestration_authoring_action_openapi as openapi_module

    definition = _definition()
    inspection = inspect_definition(definition)
    plan_result = plan_authoring_definition(definition)
    plan_body = {
        **plan_result.plan,
        **inspection_response_fields(plan_result.inspection),
        'definitionSource': 'inline',
    }
    samples = {
        'inspection': (inspection_response_schema(), inspection),
        'compose': (compose_response_schema(), {
            'ok': True,
            'reply': 'Built the graph.',
            'definition': definition,
            'inspection': inspection,
            'validation': {'ok': True, 'errors': [], 'warnings': []},
            'error': None,
        }),
        'layout': (definition_action_response_schema(
            definition_source=True, layout=True), {
                'ok': True,
                'definition': definition,
                'definitionSource': 'inline',
            }),
        'plan': (plan_response_schema(), plan_body),
    }
    for name, (schema, sample) in samples.items():
        assert set(schema['required']) <= set(sample), name
    layout_node = samples['layout'][0]['properties'][
        'definition']['properties']['nodes']['items']
    assert 'pos' in layout_node['required']
    assert layout_node['properties']['pos']['required'] == ['x', 'y']
    compose_success = samples['compose'][0]['allOf'][0]
    assert compose_success['if']['properties']['ok'] == {'const': True}
    assert compose_success['then']['properties']['definition'] == \
        definition_request_schema()
    for schema_name in (
        'compose_response_schema',
        'definition_action_response_schema',
        'plan_response_schema',
    ):
        assert getattr(openapi_module, schema_name) is getattr(
            wire_module, schema_name)
        assert f'def {schema_name}' not in open(
            openapi_module.__file__, encoding='utf-8').read()


def test_authoring_action_response_registry_covers_status_semantics():
    registry = authoring_action_response_registry()
    assert set(registry) == {
        'validation', 'compose', 'builtin', 'layout', 'plan',
    }
    assert set(registry['validation']) == {'200', '400', '401', '403', '500'}
    assert set(registry['compose']) == {'200', '400', '401', '403', '500'}
    assert set(registry['layout']) == {'200', '400', '401', '403', '500'}
    assert set(registry['plan']) == {'200', '400', '401', '403', '500'}
    assert set(registry['builtin']) == {'200', '401', '403', '404', '500'}
    with pytest.raises(ValueError, match='unknown authoring action'):
        authoring_action_responses('execute')

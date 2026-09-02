"""Contract tests for shared repository-free authoring HTTP projections."""

from __future__ import annotations

import pytest

import lib.orchestration.compose_request_contract as compose_contract_module
from lib.request_parser import BadRequest
import routes.api_v1.orchestration_authoring_http as authoring_http
import routes.api_v1.orchestration_authoring_openapi as authoring_openapi


pytestmark = pytest.mark.unit


def test_authoring_http_does_not_publish_a_parallel_service_call_wrapper():
    assert not hasattr(authoring_http, 'authoring_service_call')


def test_compose_schema_uses_the_parser_requirement_limit():
    contract = compose_contract_module.compose_request_contract()
    assert contract == {
        'requirementField': 'requirement',
        'currentField': 'current',
        'historyField': 'history',
        'requirementMaxLength': authoring_http.MAX_COMPOSE_REQUIREMENT_LENGTH,
        'historyRetainedItems': authoring_http.MAX_COMPOSE_HISTORY_ITEMS,
        'historyContentMaxLength':
            authoring_http.MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    }
    assert authoring_http.compose_request_schema is \
        compose_contract_module.compose_request_schema
    assert authoring_http.compose_request_contract is \
        compose_contract_module.compose_request_contract
    assert 'def compose_request_schema' not in open(
        'routes/api_v1/orchestration_authoring_http.py',
        encoding='utf-8',
    ).read()
    schema = authoring_http.compose_request_schema()
    assert schema['required'] == ['requirement']
    assert schema['properties']['requirement'] == {
        'type': 'string',
        'minLength': 1,
        'maxLength': authoring_http.MAX_COMPOSE_REQUIREMENT_LENGTH,
    }
    assert schema['properties']['history']['items'] == {
        'type': 'object',
        'required': ['role', 'content'],
        'properties': {
            'role': {
                'type': 'string',
                'enum': ['user', 'assistant'],
            },
            'content': {
                'type': 'string',
                'maxLength': authoring_http.MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
            },
        },
    }
    assert schema['properties']['history']['x-retainedItems'] == \
        authoring_http.MAX_COMPOSE_HISTORY_ITEMS


def test_authoring_ingress_consumes_canonical_argument_identity():
    source = open(
        'routes/api_v1/orchestration_authoring_http.py',
        encoding='utf-8',
    ).read()
    contract_source = open(
        'lib/orchestration/compose_request_contract.py',
        encoding='utf-8',
    ).read()
    assert "body, 'requirement'" not in source
    assert "body, 'current'" not in source
    assert "body, 'history'" not in source
    assert 'role-schema' not in source
    assert len(contract_source.splitlines()) < 85


def test_authoring_catalogue_projects_closed_backend_registries():
    from lib.orchestration.authoring_contract import authoring_contract

    contract = authoring_contract()
    roles = set(contract['roleNames'])
    assert roles == set(contract['roles'])
    assert roles == set(contract['personas'])
    assert roles == set(contract['defaultEmits'])
    assert roles == set(contract['nodeDefaults']['roles'])
    assert contract['roles']['general'] == contract['generic']
    assert contract['personas']['router'] == contract['personas']['general']
    assert contract['personas']['synthesizer'] == \
        contract['personas']['general']
    for role in roles:
        assert contract['personas'][role]['tier'] == \
            contract['nodeDefaults']['roles'][role]['tier']

    controls = set(contract['controls'])
    assert controls == set(contract['controlSchemas'])
    assert controls == set(contract['nodeDefaults']['controls'])


def test_typed_io_client_failure_codes_are_canonical_and_detached():
    from lib.orchestration.io_contract import io_contract_schema
    from lib.orchestration.io_issue_codes import io_client_failure_codes

    contract = io_contract_schema()
    assert contract['failureCodes'] == io_client_failure_codes()
    assert contract['failureCodes']['maxPorts'] == 'io.side.max_ports'
    assert contract['failureCodes']['missingPortName'] == \
        'io.port.name.required'
    contract['failureCodes']['maxPorts'] = 'mutated.by.consumer'
    assert io_contract_schema()['failureCodes']['maxPorts'] == \
        'io.side.max_ports'


def test_authoring_response_schema_comes_from_live_section_registries():
    import lib.orchestration.authoring_contract as contract_module

    from lib.orchestration.authoring_contract import (
        AUTHORING_OBJECT_SECTION_NAMES,
        RUNTIME_CONTRACT_SECTION_NAMES,
        authoring_contract,
        authoring_object_section_schemas,
        rolling_optional_section_fields,
    )

    schema = authoring_openapi.authoring_contract_response_schema()
    properties = schema['properties']
    registry = properties['contractSections']['properties']
    assert set(AUTHORING_OBJECT_SECTION_NAMES) < set(schema['required'])
    assert registry['authoring']['items']['enum'] == \
        list(AUTHORING_OBJECT_SECTION_NAMES)
    assert registry['runtime']['items']['enum'] == \
        list(RUNTIME_CONTRACT_SECTION_NAMES)
    assert registry['authoring']['minItems'] == \
        len(AUTHORING_OBJECT_SECTION_NAMES)
    assert registry['runtime']['maxItems'] == \
        len(RUNTIME_CONTRACT_SECTION_NAMES)
    assert 'rollingOptionalFields' in properties[
        'contractSections']['required']
    rolling_schema = registry['rollingOptionalFields']
    assert rolling_schema['required'] == list(
        rolling_optional_section_fields())
    assert {
        name: spec['items']['enum']
        for name, spec in rolling_schema['properties'].items()
    } == rolling_optional_section_fields()
    assert set(authoring_contract()) <= set(properties)
    event_contract = authoring_contract()['eventContract']
    event_schema = properties['eventContract']
    assert event_schema['properties']['schema']['enum'] == [
        event_contract['schema'],
    ]
    assert event_schema['properties']['types']['required'] == \
        list(event_contract['types'])
    assert event_schema['properties']['types']['additionalProperties'] is True
    assert event_schema['properties']['types']['properties']['flow_start'][
        'properties']['runStatus']['enum'] == ['running']
    run_contract = authoring_contract()['runContract']
    run_schema = properties['runContract']
    assert run_schema['required'] == [
        'schema', 'initial', 'statuses', 'terminal', 'categories',
    ]
    assert run_schema['properties']['statuses']['items']['enum'] == \
        run_contract['statuses']
    assert run_schema['properties']['terminal']['items']['enum'] == \
        run_contract['terminal']
    assert run_schema['properties']['categories']['required'] == \
        run_contract['statuses']
    assert {
        status: spec['enum'][0]
        for status, spec in run_schema['properties']['categories'][
            'properties'].items()
    } == run_contract['categories']

    section_schemas = authoring_object_section_schemas()
    assert tuple(section_schemas) == AUTHORING_OBJECT_SECTION_NAMES
    assert section_schemas == {
        name: properties[name] for name in AUTHORING_OBJECT_SECTION_NAMES
    }
    for name in RUNTIME_CONTRACT_SECTION_NAMES:
        assert section_schemas[name]['type'] == 'object'
        assert section_schemas[name]['required'] == list(
            authoring_contract()[name])
    assert section_schemas['requestLimits']['properties']['runInput'][
        'properties']['maxLength']['const'] == \
        authoring_contract()['requestLimits']['runInput']['maxLength']
    assert section_schemas['outcomeContract']['properties']['categories'][
        'items']['enum'] == authoring_contract()['outcomeContract']['categories']
    assert section_schemas['traceContract']['properties']['textLimits'][
        'properties']['output']['const'] == \
        authoring_contract()['traceContract']['textLimits']['output']
    assert section_schemas['traceContract']['properties']['historyLimit'][
        'const'] == authoring_contract()['traceContract']['historyLimit']
    assert section_schemas['traceContract']['properties']['activityFields'][
        'properties']['stateChanging']['enum'] == ['state_changing']
    assert section_schemas['mutationContract']['properties']['actions'][
        'items']['enum'] == authoring_contract()['mutationContract']['actions']
    assert section_schemas['replayContract']['properties']['cursor'][
        'required'] == list(authoring_contract()['replayContract']['cursor'])
    assert section_schemas['runtimeStartContract']['properties']['kinds'][
        'items']['enum'] == authoring_contract()['runtimeStartContract']['kinds']
    assert section_schemas['durableRunContract']['properties']['readFields'][
        'items']['enum'] == authoring_contract()['durableRunContract'][
            'readFields']
    for name in (
        'inspectionContract', 'definitionListContract',
        'definitionEntryContract', 'definitionWriteContract',
        'fieldValueContract', 'ioContract',
    ):
        assert section_schemas[name]['required'] == list(
            authoring_contract()[name])
        assert section_schemas[name]['additionalProperties'] is False
    assert section_schemas['fieldValueContract']['properties']['kinds'][
        'required'] == list(authoring_contract()['fieldValueContract']['kinds'])
    assert section_schemas['ioContract']['properties']['types']['items'][
        'enum'] == authoring_contract()['ioContract']['types']
    for name in ('roles', 'controlSchemas'):
        registry_schema = section_schemas[name]
        assert registry_schema['required'] == list(authoring_contract()[name])
        assert registry_schema['additionalProperties'] is False
        for field_list in registry_schema['properties'].values():
            field = field_list['items']
            assert field['required'] == ['key', 'kind', 'label']
            assert field['properties']['kind']['enum'] == sorted(
                {'text', 'textarea', 'select', 'list', 'int', 'bool'})
            assert {'visibleWhen', 'allowUnknown', 'severity'} <= set(
                field['properties'])
    personas = section_schemas['personas']
    assert personas['required'] == list(authoring_contract()['personas'])
    assert personas['additionalProperties'] is False
    assert all(spec['required'] == ['prompt', 'whenToUse', 'tier']
               for spec in personas['properties'].values())
    emits = section_schemas['defaultEmits']
    assert emits['required'] == list(authoring_contract()['defaultEmits'])
    assert all(spec['enum'] == ['assistant', 'user']
               for spec in emits['properties'].values())
    execution = section_schemas['executionOptions']
    assert execution['required'] == list(
        authoring_contract()['executionOptions'])
    assert execution['properties']['tiers']['items']['enum'] == \
        authoring_contract()['executionOptions']['tiers']
    node_defaults = section_schemas['nodeDefaults']
    assert node_defaults['required'] == list(
        authoring_contract()['nodeDefaults'])
    assert node_defaults['additionalProperties'] is False
    blank = node_defaults['properties']['blankSubflow']['properties']
    assert len(blank['nodes']['items']['oneOf']) == 3
    assert len(blank['edges']['items']['oneOf']) == 2
    assert properties['roleNames']['items']['enum'] == \
        authoring_contract()['roleNames']
    assert properties['kinds']['items']['enum'] == \
        authoring_contract()['kinds']
    assert properties['controls']['required'] == list(
        authoring_contract()['controls'])
    assert properties['builtins']['items']['enum'] == \
        authoring_contract()['builtins']
    section_schemas['eventContract']['required'].clear()
    assert authoring_object_section_schemas()['eventContract']['required']

    schema['required'].clear()
    assert authoring_openapi.authoring_contract_response_schema()['required']
    assert authoring_openapi.authoring_contract_responses()['200']['content'][
        'application/json']['schema'] == \
        authoring_openapi.authoring_contract_response_schema()
    assert authoring_openapi.authoring_contract_response_schema is \
        contract_module.authoring_contract_response_schema
    source = open(authoring_openapi.__file__, encoding='utf-8').read()
    assert 'def authoring_contract_response_schema' not in source


def test_authoring_openapi_has_one_contract_discovery_route():
    assert set(authoring_openapi.authoring_route_response_registry()) == {
        'validation', 'compose', 'builtin', 'layout', 'plan',
        'authoring-contract',
    }


def test_compose_request_preparation_owns_authoring_body_shape(monkeypatch):
    body = {
        'requirement': '  revise flow  ',
        'current': {'nodes': []},
        'history': [{
            'role': 'user', 'content': '  start  ', 'clientOnly': True,
        }],
    }
    prepared, failure = authoring_http.prepare_compose_request(body)
    assert failure is None
    assert prepared is not None
    assert prepared.requirement == 'revise flow'
    assert prepared.current is body['current']
    assert prepared.history == [{'role': 'user', 'content': 'start'}]
    assert prepared.history is not body['history']

    sentinel = object()
    monkeypatch.setattr(
        authoring_http,
        'api_bad_request',
        lambda message, **fields: (sentinel, message, fields),
    )
    prepared, failure = authoring_http.prepare_compose_request({})
    assert prepared is None
    assert failure == (
        sentinel, 'requirement is required', {'field': 'requirement'},
    )

    with pytest.raises(BadRequest, match='requirement must be a string'):
        authoring_http.prepare_compose_request({'requirement': 7})

    with pytest.raises(BadRequest, match='current must be an object'):
        authoring_http.prepare_compose_request({
            'requirement': 'revise', 'current': [],
        })
    with pytest.raises(BadRequest, match='history must be a list'):
        authoring_http.prepare_compose_request({
            'requirement': 'revise', 'history': {},
        })
    with pytest.raises(BadRequest, match=r'history\[0\] must be a dict'):
        authoring_http.prepare_compose_request({
            'requirement': 'revise', 'history': ['bad'],
        })


def test_compose_request_keeps_only_the_backend_consumed_history_window():
    history = [
        {'role': 'user', 'content': str(index)}
        for index in range(authoring_http.MAX_COMPOSE_HISTORY_ITEMS + 3)
    ]
    prepared, failure = authoring_http.prepare_compose_request({
        'requirement': 'revise', 'history': history,
    })

    assert failure is None
    assert prepared is not None
    assert prepared.history == history[-authoring_http.MAX_COMPOSE_HISTORY_ITEMS:]


def test_definition_action_projection_preserves_builtin_and_layout_shapes(
        monkeypatch):
    calls = []

    def fake_ok(data=None, **fields):
        calls.append((data, fields))
        return data, fields

    monkeypatch.setattr(authoring_http, 'api_ok', fake_ok)
    definition = {'nodes': [], 'edges': []}
    inspection = {'ok': True, 'diagnostics': []}

    builtin = authoring_http.authoring_definition_response(
        definition, inspection=inspection)
    layout = authoring_http.authoring_definition_response(
        definition, definition_source='')

    assert builtin == ({
        'definition': definition,
        'inspection': inspection,
    }, {})
    assert 'definitionSource' not in builtin[0]
    assert layout == ({
        'definition': definition,
        'definitionSource': '',
    }, {})
    assert len(calls) == 2


def test_builtin_projection_owns_found_and_missing_http_semantics(monkeypatch):
    found = object()
    missing = object()
    monkeypatch.setattr(
        authoring_http,
        'authoring_definition_response',
        lambda definition, **values: (found, definition, values),
    )
    monkeypatch.setattr(
        authoring_http,
        'api_not_found',
        lambda message: (missing, message),
    )

    present = type('Builtin', (), {
        'definition': {'name': 'Endpoint'},
        'inspection': {'ok': True},
    })()
    absent = type('Builtin', (), {
        'definition': None,
        'inspection': None,
    })()

    assert authoring_http.authoring_builtin_response(
        present, name='endpoint') == (
            found, {'name': 'Endpoint'}, {'inspection': {'ok': True}},
        )
    assert authoring_http.authoring_builtin_response(
        absent, name='missing') == (
            missing, "Unknown built-in flow 'missing'",
        )


def test_compose_projection_uses_result_passthrough(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_payload(payload, status=200, **fields):
        captured.update(payload=payload, status=status, fields=fields)
        return sentinel

    monkeypatch.setattr(authoring_http, 'api_payload', fake_payload)
    result = {
        'ok': False,
        'definition': None,
        'error': 'composed graph failed validation',
    }

    assert authoring_http.authoring_compose_response(result) is sentinel
    assert captured == {'payload': result, 'status': 200, 'fields': {}}


def test_plan_projection_is_shared_and_does_not_mutate_service_output(
        monkeypatch):
    captured = {}
    monkeypatch.setattr(
        authoring_http,
        'api_ok',
        lambda data=None, **fields: captured.update(
            data=data, fields=fields) or data,
    )
    plan = {'ok': True, 'steps': [{'node': 'worker'}]}
    inspection = {
        'ok': True,
        'warnings': ['review'],
        'contract': {'format': 'contract-v1'},
    }

    response = authoring_http.authoring_plan_response(
        plan, inspection, definition_source='stored:flow-1',
    )

    assert response == {
        'ok': True,
        'steps': [{'node': 'worker'}],
        'inspection': inspection,
        'warnings': ['review'],
        'contract': {'format': 'contract-v1'},
        'definitionSource': 'stored:flow-1',
    }
    assert captured['fields'] == {}
    assert 'inspection' not in plan
    response['steps'][0]['node'] = 'mutated'
    response['inspection']['warnings'].append('later')
    assert plan['steps'][0]['node'] == 'worker'
    assert inspection['warnings'] == ['review']

"""HTTP documentation matches declared response protocols and live assembly."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from quart import Blueprint, jsonify
from jsonschema import Draft202012Validator

from lib.api_v4 import attach_v4_envelope, NativeContractBlueprint
from lib.generated_api_v4 import OPENAPI_DOCUMENT
from lib.app.app_factory import create_base_app
from lib.openapi import api_meta, build_spec

pytestmark = pytest.mark.unit


def _schema_validator(spec, schema):
    return Draft202012Validator({**schema, 'components': spec['components']})


@pytest.mark.parametrize('domain', [
    {'key': 'value'}, {'ok': True, 'key': 'value'},
    {'ok': False, 'error': 'logical_failure'}, [1, 2], None,
    {'data': {'key': 'nested'}, 'meta': {'name': 'domain'}},
])
def test_product_wire_preserves_domain_value_and_matches_documentation(domain):
    app = create_base_app('openapi-envelope-example', {'TESTING': True})
    bp = Blueprint('example', __name__)

    @bp.get('/api/v4/example')
    @api_meta(responses={'200': {'description': 'Domain result', 'content': {
        'application/json': {'schema': {}, 'example': domain},
    }}})
    async def example():
        return jsonify(domain)

    # Re-registering the same declared boundary must not double-wrap bodies.
    app.register_blueprint(attach_v4_envelope(attach_v4_envelope(bp)))
    spec = build_spec(app)
    response = asyncio.run(app.test_client().get('/api/v4/example'))
    body = asyncio.run(response.get_json())
    expected = deepcopy(domain)
    if isinstance(expected, dict) and expected.get('ok') is True:
        expected.pop('ok')
    assert body['data'] == expected
    assert set(body) == {'data', 'meta'}
    assert set(body['meta']) == {'requestId', 'serverTimeMs'}
    media = spec['paths']['/api/v4/example']['get']['responses']['200']['content']['application/json']
    _schema_validator(spec, media['schema']).validate(body)
    _schema_validator(spec, media['schema']).validate(media['example'])


def test_response_projection_keeps_native_receipts_and_generic_problems_distinct():
    app = create_base_app('openapi-native-example')
    bp = NativeContractBlueprint('native', __name__, contract_id='example/native')

    @bp.get('/api/v4/example')
    @api_meta(responses={'409': {'description': 'Conflict receipt', 'content': {
        'application/json': {'schema': {'type': 'object', 'required': ['receipt']}},
    }}})
    async def example():
        return jsonify({'receipt': 'retry'}), 409

    app.register_blueprint(bp)
    operation = build_spec(app)['paths']['/api/v4/example']['get']
    assert 'application/json' in operation['responses']['409']['content']
    assert operation['x-tofu-http-response-contract'] == 'example/native'
    other = create_base_app('openapi-generic-example')
    generic = Blueprint('generic', __name__)
    generic.add_url_rule('/api/v4/example', 'example', example)
    other.register_blueprint(attach_v4_envelope(generic))
    failure = build_spec(other)['paths']['/api/v4/example']['get']['responses']['409']
    assert set(failure['content']) == {'application/problem+json'}


def test_complete_spec_has_resolvable_refs_native_types_and_websocket_upgrade():
    from scripts.gen_api_endpoint_index import route_app

    app = route_app()
    spec = build_spec(app)
    assert '/api/v1/' not in str(spec['paths'].keys())
    assert spec['info']['version'] == '4.0.0'
    assert '/api/v4/' in spec['info']['description']
    native = spec['paths']['/api/v4/conversations/{conversationId}/sync']['get']
    assert native['x-tofu-http-response-contract'] == 'tofu.conversation-sync/v3'
    assert native['security'] == [{'bearerAuth': ['chat']}]
    assert native['responses']['200']['content']['application/json']['schema']['$ref'].endswith(
        '/ConversationSyncV3_ConversationSyncSnapshot')
    product = spec['paths']['/api/v4/conversations']['get']
    assert product['responses']['200']['content']['application/json']['schema']['required'] == ['data', 'meta']
    sockets = [op for item in spec['paths'].values() for op in item.values()
               if isinstance(op, dict) and op.get('x-tofu-transport') == 'websocket']
    assert sockets and all('101' in op['responses'] and '200' not in op['responses'] for op in sockets)

    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value and value['$ref'].startswith('#/'):
                target = spec
                for part in value['$ref'][2:].split('/'):
                    target = target[part.replace('~1', '/').replace('~0', '~')]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(spec)
    assert spec['paths']['/api/v4/meta']['get'] == OPENAPI_DOCUMENT['paths']['/api/v4/meta']['get']
    spec['paths']['/api/v4/meta']['get']['responses'].clear()
    assert OPENAPI_DOCUMENT['paths']['/api/v4/meta']['get']['responses']


def test_nested_domain_resource_validates_after_success_envelope_projection():
    from lib.openapi._wire import rebase_inline_schema_refs
    from jsonschema import Draft202012Validator, ValidationError
    domain = {'$defs': {'Node': {'type': 'object', 'additionalProperties': False,
              'properties': {'value': {'type': 'integer'}, 'child': {'$ref': '#/$defs/Node'}},
              'required': ['value']}}, '$ref': '#/$defs/Node'}
    operation = {'responses': {'200': {'content': {'application/json': {'schema': {
        'type': 'object', 'properties': {'data': domain}, 'required': ['data']}}}}}}
    path = '/api/v4/nested'
    projected = rebase_inline_schema_refs(operation, path, 'get')
    document = {'paths': {path: {'get': projected}}, '$ref':
                '#/paths/~1api~1v4~1nested/get/responses/200/content/application~1json/schema'}
    validator = Draft202012Validator(document)
    validator.validate({'data': {'value': 1, 'child': {'value': 2}}})
    with pytest.raises(ValidationError):
        validator.validate({'data': {'value': 1, 'child': {'value': 'invalid'}}})


def test_spec_construction_failure_is_not_a_successful_empty_catalog(monkeypatch):
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes import api_docs

    app = create_base_app('openapi-failure', {'TESTING': True})
    app.register_blueprint(api_docs.api_docs_bp)
    register_http_error_handlers(app)

    def broken(_app):
        raise RuntimeError('synthetic-private-provider-detail')
    monkeypatch.setattr(api_docs, 'build_spec', broken)
    response = asyncio.run(app.test_client().get('/api/openapi.json'))
    assert response.status_code == 500
    body = asyncio.run(response.get_json())
    assert 'synthetic-private-provider-detail' not in str(body)


@pytest.mark.parametrize(('flag', 'required'), [
    ({'type': 'boolean', 'const': True}, True),
    ({'type': 'boolean'}, True),
    ({'enum': [True]}, True),
    ({'const': False}, True),
])
def test_domain_schema_projects_only_top_level_transport_flag(flag, required):
    from lib.openapi._wire import project_v4_responses

    domain = {'type': 'object', 'additionalProperties': False,
              'properties': {'ok': flag, 'nested': {
                  'type': 'object', 'properties': {'ok': {'const': True}},
                  'required': ['ok'],
              }}, 'required': ['ok', 'nested'] if required else ['nested']}
    components = deepcopy(OPENAPI_DOCUMENT['components'])
    components['schemas']['Domain'] = domain
    response = {'200': {'description': 'Example', 'content': {
        'application/json': {'schema': {'$ref': '#/components/schemas/Domain'}},
    }}}
    projected = project_v4_responses(response, components, success_envelope=True)
    schema = projected['200']['content']['application/json']['schema']
    validator = _schema_validator({'components': components}, schema)
    body = {'data': {'nested': {'ok': True}},
            'meta': {'requestId': 'example', 'serverTimeMs': 0}}
    if flag.get('const') is False:
        body['data']['ok'] = False
    validator.validate(body)
    assert components['schemas']['Domain'] == domain
    assert response['200']['content']['application/json']['schema'] == {
        '$ref': '#/components/schemas/Domain'}


def test_documentation_leaves_stream_download_and_bodyless_responses_intact():
    from lib.openapi._wire import project_v4_responses

    responses = {
        '200': {'description': 'Events', 'content': {'text/event-stream': {
            'schema': {'type': 'string'}}}},
        '201': {'description': 'Image', 'content': {'image/png': {
            'schema': {'type': 'string', 'format': 'binary'}}}},
        '204': {'description': 'No content'},
    }
    assert project_v4_responses(responses, {}, success_envelope=True) == responses


@pytest.mark.parametrize('status', [204, 205])
def test_bodyless_status_is_not_eligible_for_json_envelope(status):
    from quart import Response
    from lib.api_v4 import translate_v4_envelope

    response = Response('{"ok":true}', status=status, content_type='application/json')
    assert asyncio.run(translate_v4_envelope(response)) is response
    assert asyncio.run(response.get_data()) == b'{"ok":true}'


def test_embedded_definition_refs_preserve_recursive_resources():
    from lib.openapi import schema_definition
    from lib.openapi._wire import rebase_inline_schema_refs

    authority = {'$defs': {
        'Node': {'type': 'object', 'properties': {'next': {'$ref': '#/$defs/Node'}}},
        'Unused': {'type': 'string'},
    }}
    original = deepcopy(authority)
    resource = schema_definition(authority, 'Node')
    assert set(resource['$defs']) == {'Node'}
    operation = {'requestBody': {'content': {'application/json': {'schema': resource}}}}
    projected = rebase_inline_schema_refs(operation, '/api/v4/example/{id}', 'post')
    ref = projected['requestBody']['content']['application/json']['schema']['properties']['next']['$ref']
    assert ref == ('#/paths/~1api~1v4~1example~1{id}/post/requestBody/content/'
                   'application~1json/schema/$defs/Node')
    assert authority == original
    assert operation['requestBody']['content']['application/json']['schema']['properties']['next']['$ref'] == '#/$defs/Node'

    spec = {'paths': {'/api/v4/example/{id}': {'post': projected}}}
    Draft202012Validator({**spec, '$ref': (
        '#/paths/~1api~1v4~1example~1{id}/post/requestBody/content/application~1json/schema'
    )}).validate({'next': {'next': {}}})


def test_reference_sibling_constraints_are_conjoined_after_projection():
    from lib.openapi._wire import project_v4_responses

    components = deepcopy(OPENAPI_DOCUMENT['components'])
    components['schemas']['Domain'] = {
        'type': 'object', 'properties': {
            'ok': {'const': True}, 'count': {'type': 'integer', 'minimum': 1},
        }, 'required': ['ok', 'count'],
    }
    responses = {'200': {'content': {'application/json': {'schema': {
        '$ref': '#/components/schemas/Domain',
        'properties': {'count': {'maximum': 3}},
    }}}}}
    schema = project_v4_responses(responses, components, success_envelope=True)[
        '200']['content']['application/json']['schema']
    validator = _schema_validator({'components': components}, schema)
    body = {'data': {'count': 2}, 'meta': {'requestId': 'example', 'serverTimeMs': 0}}
    validator.validate(body)
    for count in (0, 4):
        body['data']['count'] = count
        assert not validator.is_valid(body)

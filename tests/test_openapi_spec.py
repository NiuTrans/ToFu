"""tests/test_openapi_spec.py — OpenAPI generation unit tests.

Mostly pure-function checks; the route discovery test mocks an app
with a couple of registered routes to confirm `build_spec` walks
``url_map`` correctly.
"""

import unittest

import pytest


pytestmark = pytest.mark.unit


class ApiMetaDecoratorTest(unittest.TestCase):

    def test_attaches_metadata(self):
        from lib.openapi import api_meta

        @api_meta(summary='Hi', tags=['t'], scope='chat',
                  request_body={'required': True})
        def handler():
            return 'ok'

        self.assertTrue(hasattr(handler, '_api_meta'))
        meta = handler._api_meta
        self.assertEqual(meta['summary'], 'Hi')
        self.assertEqual(meta['tags'], ['t'])
        self.assertEqual(meta['scope'], 'chat')
        self.assertTrue(meta['request_body']['required'])

    def test_default_falsy(self):
        from lib.openapi import api_meta

        @api_meta()
        def h():
            return None
        m = h._api_meta
        self.assertEqual(m['summary'], '')
        self.assertEqual(m['tags'], [])
        self.assertFalse(m['deprecated'])
        self.assertFalse(m['public'])

    def test_vendor_extensions_are_validated_and_published(self):
        from lib.openapi import api_meta, build_spec

        @api_meta(extensions={'x-tofu-contract': 'example/v1'})
        def handler():
            return None

        app = BuildSpecTest()._stub_app([
            ('/api/v1/example', 'example', {'GET'}),
        ])
        app.view_functions = {'example': handler}
        operation = build_spec(app)['paths']['/api/v1/example']['get']
        self.assertEqual(operation['x-tofu-contract'], 'example/v1')
        with self.assertRaisesRegex(ValueError, 'must start with x-'):
            api_meta(extensions={'contract': 'invalid'})

    def test_explicit_bodyless_write_suppresses_default_request_body(self):
        from lib.openapi import api_meta, build_spec

        @api_meta(request_body=False)
        def handler():
            return None

        app = BuildSpecTest()._stub_app([
            ('/api/v1/example/abort', 'abort_example', {'POST'}),
        ])
        app.view_functions = {'abort_example': handler}
        operation = build_spec(app)['paths'][
            '/api/v1/example/abort']['post']
        self.assertNotIn('requestBody', operation)
        with self.assertRaisesRegex(TypeError, 'dict, False or None'):
            api_meta(request_body=True)


class BuildSpecTest(unittest.TestCase):

    def _stub_app(self, rules):
        """Build a minimal app-like object with a url_map.iter_rules()."""
        class Rule:
            def __init__(self, rule, endpoint, methods):
                self.rule = rule
                self.endpoint = endpoint
                self.methods = methods

            def __str__(self):
                return self.rule

        class UrlMap:
            def __init__(self, rs):
                self._rs = rs
            def iter_rules(self):
                return iter(self._rs)

        class App:
            url_map = UrlMap([Rule(*r) for r in rules])
            view_functions: dict = {}

        return App()

    def test_basic_spec(self):
        from lib.openapi import api_meta, build_spec

        @api_meta(summary='List things', tags=['stuff'], scope='chat')
        def list_handler():
            return None

        @api_meta(summary='Create thing', tags=['stuff'], scope='admin',
                  request_body={'required': True, 'content': {
                      'application/json': {'schema': {'type': 'object'}}}})
        def create_handler():
            return None

        app = self._stub_app([
            ('/api/v1/things', 'list_things', {'GET'}),
            ('/api/v1/things', 'create_thing', {'POST'}),
            ('/static/foo', 'static', {'GET'}),  # should be skipped
        ])
        app.view_functions = {
            'list_things': list_handler,
            'create_thing': create_handler,
            'static': lambda: None,
        }
        spec = build_spec(app, title='X', version='9')
        self.assertEqual(spec['openapi'], '3.1.0')
        self.assertEqual(spec['info']['title'], 'X')
        self.assertEqual(spec['info']['version'], '9')
        self.assertIn('/api/v1/things', spec['paths'])
        self.assertIn('get', spec['paths']['/api/v1/things'])
        self.assertIn('post', spec['paths']['/api/v1/things'])
        # /static/* skipped.
        self.assertNotIn('/static/foo', spec['paths'])
        # Components include the standard schemas.
        self.assertIn('ErrorEnvelope', spec['components']['schemas'])
        self.assertIn('TypedErrorEnvelope', spec['components']['schemas'])
        self.assertIn('ChatCompletionRequest', spec['components']['schemas'])
        self.assertIn('TaskState', spec['components']['schemas'])
        self.assertIn('ApiKey', spec['components']['schemas'])
        # Security schemes wired.
        self.assertIn('bearerAuth', spec['components']['securitySchemes'])
        self.assertEqual(
            set(spec['components']['securitySchemes']), {'bearerAuth'})

    def test_error_schemas_share_the_runtime_envelope_contract(self):
        from lib.error_envelope import KINDS, make_envelope
        from lib.openapi._schema import _components

        schemas = _components()['schemas']
        typed = schemas['TypedErrorEnvelope']
        sample = make_envelope('timeout')

        self.assertEqual(set(typed['required']), {
            'kind', 'severity', 'retryable', 'message', 'hint', 'detail',
            'model', 'context', 'source', 'raw',
        })
        self.assertEqual(set(typed['properties']['kind']['enum']), set(KINDS))
        self.assertTrue(set(typed['required']).issubset(sample))
        self.assertEqual(
            schemas['ErrorEnvelope']['properties']['error']['anyOf'][1],
            {'$ref': '#/components/schemas/TypedErrorEnvelope'},
        )
        self.assertEqual(
            schemas['TaskState']['properties']['error']['oneOf'],
            [
                {'$ref': '#/components/schemas/TypedErrorEnvelope'},
                {'type': 'null'},
            ],
        )

    def test_typed_error_schema_is_fresh_per_components_build(self):
        from lib.openapi._schema import _components

        first = _components()
        first['schemas']['TypedErrorEnvelope']['required'].append('mutated')
        second = _components()
        self.assertNotIn(
            'mutated', second['schemas']['TypedErrorEnvelope']['required'])

    def test_path_parameter_extraction(self):
        from lib.openapi import _flask_to_openapi_path, _path_parameters
        self.assertEqual(_flask_to_openapi_path('/api/v1/tasks/<task_id>'),
                         '/api/v1/tasks/{task_id}')
        self.assertEqual(_flask_to_openapi_path('/api/x/<int:n>/<name>'),
                         '/api/x/{n}/{name}')
        params = _path_parameters('/api/v1/tasks/<task_id>')
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0]['name'], 'task_id')
        self.assertEqual(params[0]['in'], 'path')
        self.assertTrue(params[0]['required'])


class HtmlViewersTest(unittest.TestCase):

    def test_swagger_html(self):
        from lib.openapi import swagger_html
        out = swagger_html('/api/openapi.json')
        self.assertIn('SwaggerUIBundle', out)
        self.assertIn('/api/openapi.json', out)

    def test_redoc_html(self):
        from lib.openapi import redoc_html
        out = redoc_html('/api/openapi.json')
        self.assertIn('redoc', out)
        self.assertIn('/api/openapi.json', out)


if __name__ == '__main__':
    unittest.main()

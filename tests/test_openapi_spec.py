"""tests/test_openapi_spec.py — OpenAPI generation unit tests.

Mostly pure-function checks; the route discovery test mocks an app
with a couple of registered routes to confirm `build_spec` walks
``url_map`` correctly.
"""

import unittest


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
        self.assertIn('ChatCompletionRequest', spec['components']['schemas'])
        self.assertIn('TaskState', spec['components']['schemas'])
        self.assertIn('ApiKey', spec['components']['schemas'])
        # Security schemes wired.
        self.assertIn('bearerAuth', spec['components']['securitySchemes'])
        self.assertIn('tunnelTokenHeader', spec['components']['securitySchemes'])

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

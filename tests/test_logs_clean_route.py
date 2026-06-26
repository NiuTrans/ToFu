"""tests/test_logs_clean_route.py — POST /api/v1/logs/clean integration."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import unittest


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    from quart.wrappers import Request as _QR
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync(self, *a, **kw):
            return asyncio.run(_orig(self, *a, **kw))
        _QR.get_json = _sync


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class LogsCleanRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys
        cls._orig_path = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        os.environ['TUNNEL_TOKEN'] = 'tt'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.logs import api_v1_logs_bp
        cls.app.register_blueprint(api_v1_logs_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(name='log-test', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()

    def _post(self, body, headers=None):
        async def go():
            h = {'Authorization': f'Bearer {self.token}'}
            if headers:
                h.update(headers)
            return await self.app.test_client().post(
                '/api/v1/logs/clean', headers=h, json=body)
        return _new_loop_run(go())

    def test_no_noise_returns_no_noise_flag(self):
        r = self._post({'text': 'just\nfive\nclean\nlines\nhere'})
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body.get('no_noise'))

    def test_noisy_log_returns_cleaning_result(self):
        text = '\n'.join([
            'INFO 2026-01-01 10:00:00,000 module.foo Starting up',
            'INFO 2026-01-01 10:00:01,000 module.foo Connecting',
            'INFO 2026-01-01 10:00:02,000 module.foo Connected',
            'INFO 2026-01-01 10:00:03,000 module.foo Working',
            'INFO 2026-01-01 10:00:04,000 module.foo Done',
        ] * 5)
        r = self._post({'text': text})
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertNotIn('no_noise', body)
        self.assertIn('cleanedText', body)
        self.assertIn('savedChars', body)
        self.assertGreater(body['savedChars'], 0)
        self.assertGreater(body['savedPct'], 0)
        self.assertIsInstance(body['ops'], list)
        self.assertTrue(body['ops'])

    def test_missing_text_field_400(self):
        r = self._post({})
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_rejected(self):
        from lib.auth_mode import reset_for_tests
        prev = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'private'
        reset_for_tests()
        try:
            async def go():
                return await self.app.test_client().post(
                    '/api/v1/logs/clean', json={'text': 'hello'})
            r = _new_loop_run(go())
            self.assertEqual(r.status_code, 401)
        finally:
            if prev is None:
                os.environ.pop('TOFU_AUTH_MODE', None)
            else:
                os.environ['TOFU_AUTH_MODE'] = prev
            reset_for_tests()


class ExtractFileChangesRouteTest(unittest.TestCase):
    """Independent test class — reuses the same wiring as LogsCleanRouteTest
    but with its own setUpClass/tearDownClass so neither class's tests
    interfere with the other's fixtures."""

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys
        cls._orig_path = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        os.environ['TUNNEL_TOKEN'] = 'tt'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.logs import api_v1_logs_bp
        cls.app.register_blueprint(api_v1_logs_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(name='extract-test', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()

    def _post(self, body, headers=None):
        async def go():
            h = {'Authorization': f'Bearer {self.token}'}
            if headers:
                h.update(headers)
            return await self.app.test_client().post(
                '/api/v1/messages/extract-file-changes',
                headers=h, json=body)
        return _new_loop_run(go())

    def test_empty_rounds_returns_empty_list(self):
        r = self._post({'toolRounds': []})
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertEqual(body['files'], [])

    def test_single_write_returned(self):
        rounds = [{
            'toolName': 'write_file',
            'toolArgs': '{"path": "myproj:src/foo.py"}',
            'results': [{'badge': 'Created', 'writeOk': True}],
        }]
        r = self._post({'toolRounds': rounds})
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertEqual(len(body['files']), 1)
        f = body['files'][0]
        self.assertEqual(f['path'], 'src/foo.py')
        self.assertEqual(f['action'], 'created')
        self.assertEqual(f['root'], 'myproj')
        self.assertTrue(f['ok'])

    def test_missing_field_400(self):
        r = self._post({})
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_rejected(self):
        from lib.auth_mode import reset_for_tests
        prev = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'private'
        reset_for_tests()
        try:
            async def go():
                return await self.app.test_client().post(
                    '/api/v1/messages/extract-file-changes',
                    json={'toolRounds': []})
            r = _new_loop_run(go())
            self.assertEqual(r.status_code, 401)
        finally:
            if prev is None:
                os.environ.pop('TOFU_AUTH_MODE', None)
            else:
                os.environ['TOFU_AUTH_MODE'] = prev
            reset_for_tests()


if __name__ == '__main__':
    unittest.main()

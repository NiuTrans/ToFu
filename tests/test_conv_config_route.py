"""tests/test_conv_config_route.py — config/resolve + settings/resolve integration."""


from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import json
import os
import tempfile
import unittest


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ConvConfigRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys

        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.conversations import api_v1_conversations_bp
        cls.app.register_blueprint(api_v1_conversations_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(owner_user_id=1, name='cfg-test', scopes=['conversations'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        cls._tmp.cleanup()

    def _post(self, path, body, headers=None):
        async def go():
            h = {'Authorization': f'Bearer {self.token}'}
            if headers:
                h.update(headers)
            return await self.app.test_client().post(
                path, headers=h, json=body)
        return _new_loop_run(go())

    # ── /config/resolve ────────────────────────────────────────────

    def test_config_resolve_active_uses_overrides(self):
        r = self._post('/api/v1/conversations/config/resolve', {
            'conv_settings': {'model': 'inactive', 'searchMode': 'single'},
            'overrides': {'model': 'override-m', 'searchMode': 'multi'},
            'server_defaults': {'serverModel': 'default-m'},
            'is_active': True,
        })
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        # api_ok merges fields into top-level
        self.assertEqual(body['model'], 'override-m')
        self.assertEqual(body['searchMode'], 'multi')

    def test_config_resolve_inactive_uses_stored(self):
        r = self._post('/api/v1/conversations/config/resolve', {
            'conv_settings': {'model': 'stored', 'searchMode': 'single'},
            'overrides': {'model': 'override', 'searchMode': 'multi'},
            'is_active': False,
        })
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertEqual(body['model'], 'stored')
        self.assertEqual(body['searchMode'], 'single')

    def test_config_resolve_empty_body_returns_defaults(self):
        r = self._post('/api/v1/conversations/config/resolve', {})
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        # All expected keys present even with empty input
        for k in ('model', 'searchMode', 'memoryEnabled'):
            self.assertIn(k, body, f'missing key: {k}')

    # ── /settings/resolve ──────────────────────────────────────────

    def test_settings_resolve_basic(self):
        r = self._post('/api/v1/conversations/settings/resolve', {
            'conv_settings': {
                'model': 'm', 'thinkingDepth': 'high',
                'searchMode': 'multi', 'projectPath': '/code',
                'folderId': 'f1',
            },
        })
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertEqual(body['model'], 'm')
        self.assertEqual(body['thinkingDepth'], 'high')
        self.assertEqual(body['projectPath'], '/code')
        self.assertEqual(body['folderId'], 'f1')

    def test_settings_resolve_memory_default_true(self):
        r = self._post('/api/v1/conversations/settings/resolve', {
            'conv_settings': {},
        })
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['memoryEnabled'])
        self.assertEqual(body['searchMode'], 'multi')

    # ── Auth ───────────────────────────────────────────────────────

    def test_unauth_rejected(self):
        # Credential gate only fires in private/multi-user mode; open mode
        # (default) lets unauth requests through with a synthetic principal.
        from lib.auth_mode import reset_for_tests
        prev = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'private'
        reset_for_tests()
        try:
            async def go():
                return await self.app.test_client().post(
                    '/api/v1/conversations/config/resolve',
                    json={'conv_settings': {}})
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

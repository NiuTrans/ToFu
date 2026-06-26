"""tests/test_usage_and_metrics_routes.py — /api/v1/usage + /metrics."""

import asyncio
import os
import sys
import tempfile
import unittest

import pytest


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    from quart.wrappers import Request as _QR
    import inspect
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync(self, *a, **kw):
            import asyncio as _a
            coro = _orig(self, *a, **kw)
            return _a.run(coro)
        _QR.get_json = _sync


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class UsageRouteTest(unittest.TestCase):

    # The per-key request counter is recorded by the auth middleware ONLY
    # after a real credential resolves — which in 'open' mode (conftest
    # default) is short-circuited before that step. This file asserts the
    # usage/metrics contract, so the per-test fixture forces private mode.
    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()

        from lib import api_keys
        cls._orig_keys_path = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False

        from lib import usage_tracker as ut
        cls._orig_usage_path = ut._STORE_PATH
        ut._STORE_PATH = os.path.join(cls._tmp.name, 'usage.json')
        ut._state.clear()
        ut._loaded = False

        os.environ['TUNNEL_TOKEN'] = 'test-tunnel-no-real'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)

        from routes.api_v1.usage import api_v1_usage_bp
        from routes.api_v1.keys import api_v1_keys_bp
        from routes.metrics import metrics_bp
        cls.app.register_blueprint(api_v1_usage_bp)
        cls.app.register_blueprint(api_v1_keys_bp)
        cls.app.register_blueprint(metrics_bp)

        from lib.api_keys import create_key
        _row, cls.user_token = create_key(name='user', scopes=['usage', 'chat'])
        _row, cls.admin_token = create_key(name='admin', scopes=[],
                                            admin=True)

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys, usage_tracker
        api_keys._STORE_PATH = cls._orig_keys_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        usage_tracker._STORE_PATH = cls._orig_usage_path
        usage_tracker._state.clear()
        usage_tracker._loaded = False
        cls._tmp.cleanup()

    def setUp(self):
        from lib import usage_tracker
        usage_tracker._state.clear()
        usage_tracker._loaded = False
        usage_tracker._dirty = False
        # Drop the on-disk file so _ensure_loaded() doesn't replay
        # the previous test's flushed state.
        try:
            os.remove(usage_tracker._STORE_PATH)
        except FileNotFoundError:
            pass

    def test_usage_for_self(self):
        from lib.usage_tracker import record
        record('k_self_user', n_tokens=42, model='m')

        async def go():
            r = await self.app.test_client().get(
                '/api/v1/usage?days=7',
                headers={'Authorization': f'Bearer {self.user_token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            # body merges the data dict into top level (api_ok pattern)
            self.assertIn('days', body)
            self.assertEqual(len(body['days']), 7)
            self.assertEqual(body['total']['requests'], 1)  # from middleware
        _new_loop_run(go())

    def test_usage_inspect_other_key_requires_admin(self):
        async def go():
            r = await self.app.test_client().get(
                '/api/v1/usage?key_id=k_someoneelse',
                headers={'Authorization': f'Bearer {self.user_token}'})
            self.assertEqual(r.status_code, 403)
        _new_loop_run(go())

    def test_usage_admin_can_inspect_other(self):
        from lib.usage_tracker import record
        record('k_target', n_tokens=99, model='m')

        async def go():
            r = await self.app.test_client().get(
                '/api/v1/usage?key_id=k_target&days=1',
                headers={'Authorization': f'Bearer {self.admin_token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertEqual(body['key_id'], 'k_target')
            self.assertEqual(body['total']['tokens'], 99)
        _new_loop_run(go())

    def test_summary_admin_only(self):
        async def go():
            r = await self.app.test_client().get(
                '/api/v1/usage/summary?days=1',
                headers={'Authorization': f'Bearer {self.user_token}'})
            self.assertEqual(r.status_code, 403)
            r2 = await self.app.test_client().get(
                '/api/v1/usage/summary?days=1',
                headers={'Authorization': f'Bearer {self.admin_token}'})
            self.assertEqual(r2.status_code, 200)
            data = await r2.get_json()
            self.assertIn('per_key', data)
            self.assertIn('daily', data)
            self.assertIn('active_keys', data)
        _new_loop_run(go())

    def test_metrics_text_format(self):
        from lib.usage_tracker import record
        record('k_metric', n_tokens=10, model='m')

        async def go():
            r = await self.app.test_client().get(
                '/metrics',
                headers={'Authorization': f'Bearer {self.admin_token}'})
            self.assertEqual(r.status_code, 200)
            ct = r.headers.get('Content-Type', '')
            self.assertIn('text/plain', ct)
            body = await r.get_data(as_text=True)
            self.assertIn('# HELP tofu_usage_requests_total', body)
            self.assertIn('# TYPE tofu_usage_requests_total counter', body)
            self.assertIn('tofu_usage_tokens_total', body)
            self.assertIn('tofu_active_keys', body)
            # Should be valid Prometheus exposition — every metric line
            # has either `name value` or `name{labels} value`.
            for line in body.splitlines():
                if not line or line.startswith('#'):
                    continue
                # Must end with a number.
                parts = line.rsplit(' ', 1)
                self.assertEqual(len(parts), 2,
                                  f'malformed metrics line: {line!r}')
                float(parts[1])  # raises if not numeric
        _new_loop_run(go())

    def test_metrics_requires_admin(self):
        async def go():
            r = await self.app.test_client().get(
                '/metrics',
                headers={'Authorization': f'Bearer {self.user_token}'})
            self.assertEqual(r.status_code, 403)
        _new_loop_run(go())


if __name__ == '__main__':
    unittest.main()

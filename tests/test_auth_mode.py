"""tests/test_auth_mode.py — lib.auth_mode + /api/v1/auth/mode + open-mode gate.

Validates four invariants:

  1. ``lib.auth_mode`` round-trips every mode through the JSON store
     and the in-process cache.
  2. ``TOFU_AUTH_MODE`` env var locks the file-backed store.
  3. The ``GET /api/v1/auth/mode`` route is publicly readable.
  4. When the gate is in ``open`` mode, every API path passes without
     a credential AND ``g.auth_ctx.via_open_mode`` is True so
     ``require_scope`` decorators still pass.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


class AuthModeUnitTest(unittest.TestCase):
    """Direct unit tests on :mod:`lib.auth_mode`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch('lib.auth_mode._STORE_PATH',
                             os.path.join(self._tmp.name, 'auth.json'))
        self._patch.start()
        # Clear env override + cached state.
        self._env_was = os.environ.pop('TOFU_AUTH_MODE', None)
        from lib import auth_mode
        auth_mode.reset_for_tests()

    def tearDown(self):
        from lib import auth_mode
        auth_mode.reset_for_tests()
        self._patch.stop()
        if self._env_was is not None:
            os.environ['TOFU_AUTH_MODE'] = self._env_was
        self._tmp.cleanup()

    def test_default_is_open(self):
        from lib.auth_mode import get_mode
        self.assertEqual(get_mode(), 'open')

    def test_set_and_round_trip(self):
        from lib.auth_mode import get_mode, set_mode
        set_mode('private', set_by='unit-test')
        self.assertEqual(get_mode(), 'private')
        set_mode('multi-user')
        self.assertEqual(get_mode(), 'multi-user')
        set_mode('open')
        self.assertEqual(get_mode(), 'open')

    def test_unknown_mode_rejected(self):
        from lib.auth_mode import set_mode
        with self.assertRaises(ValueError):
            set_mode('whatever')

    def test_env_overrides_file(self):
        from lib import auth_mode
        auth_mode.set_mode('private')
        os.environ['TOFU_AUTH_MODE'] = 'open'
        auth_mode.reset_for_tests()
        # After reset, env wins.
        self.assertEqual(auth_mode.get_mode(), 'open')
        self.assertTrue(auth_mode.env_overrides_file())
        with self.assertRaises(RuntimeError):
            auth_mode.set_mode('private')

    def test_aliases(self):
        from lib.auth_mode import set_mode, get_mode
        set_mode('disabled')  # alias for open
        self.assertEqual(get_mode(), 'open')
        set_mode('on')        # alias for private
        self.assertEqual(get_mode(), 'private')
        set_mode('multi_user')
        self.assertEqual(get_mode(), 'multi-user')


class OpenModeGateTest(unittest.TestCase):
    """End-to-end: ``open`` mode lets unauthenticated calls through."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # Isolate api_keys + auth_mode stores from production.
        from lib import api_keys, auth_mode
        cls._orig_keys_path = api_keys._STORE_PATH
        cls._orig_mode_path = auth_mode._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        auth_mode._STORE_PATH = os.path.join(cls._tmp.name, 'auth.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        # Force open mode regardless of conftest's env default.
        cls._env_was = os.environ.pop('TOFU_AUTH_MODE', None)
        auth_mode.reset_for_tests()
        auth_mode.set_mode('open', set_by='test-fixture')
        # Build the app AFTER overrides land.
        import server  # noqa: F401
        from server import app
        cls.app = app
        cls.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys, auth_mode
        api_keys._STORE_PATH = cls._orig_keys_path
        auth_mode._STORE_PATH = cls._orig_mode_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        # Restore the env var to exactly what it was before this class ran
        # (the shared conftest sets a session-wide TOFU_AUTH_MODE default;
        # do NOT hardcode a mode here — an earlier version pinned 'private'
        # and poisoned every downstream test once the conftest default
        # became 'open').
        if cls._env_was is not None:
            os.environ['TOFU_AUTH_MODE'] = cls._env_was
        else:
            os.environ.pop('TOFU_AUTH_MODE', None)
        auth_mode.reset_for_tests()
        cls._tmp.cleanup()

    def test_get_mode_route_is_public(self):
        import asyncio
        import json
        async def go():
            c = self.app.test_client()
            r = await c.get('/api/v1/auth/mode')
            self.assertEqual(r.status_code, 200)
            body = json.loads(await r.get_data(as_text=True))
            self.assertTrue(body['ok'])
            self.assertEqual(body['mode'], 'open')
            self.assertIn('private', body['modes'])
        asyncio.run(go())

    def test_unauthed_call_passes_in_open_mode(self):
        import asyncio
        import json
        async def go():
            c = self.app.test_client()
            r = await c.get('/api/v1/keys/whoami')
            self.assertEqual(r.status_code, 200)
            body = json.loads(await r.get_data(as_text=True))
            self.assertTrue(body['authenticated'])
            self.assertEqual(body['name'], 'local')
        asyncio.run(go())

    def test_admin_only_route_passes_in_open_mode(self):
        # /api/v1/keys (list) requires the admin scope. Open mode's
        # synthetic context carries it.
        import asyncio
        async def go():
            c = self.app.test_client()
            r = await c.get('/api/v1/keys')
            self.assertEqual(r.status_code, 200)
        asyncio.run(go())


if __name__ == '__main__':
    unittest.main()

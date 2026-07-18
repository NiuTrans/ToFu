"""tests/test_update_restart_guard.py — self-update restart guard.

Root cause this guards against (2026-07-13 incident): ``POST
/api/v1/update/restart`` re-execs the whole server process, which kills EVERY
in-flight task across ALL conversations. In OPEN-auth mode an agent's own
``run_command`` probing the endpoint (``curl -X POST .../update/restart``)
silently interrupted its long-running sibling conversations — the "automatic
interruption" observed on conversations mrhz1e83i4itsu / mri07ozdjslr1d.

The fix: the route refuses with 409 when OTHER conversations have running
tasks, unless ``{"force": true}`` is passed. The caller's own conversation
(``convId``) is excluded so a conversation can still restart itself.

We patch ``lib.tasks_pkg.manager.list_running_tasks`` so the test does not
depend on real task state, and we NEVER let the real ``_deferred_reexec``
run — the guard rejects before scheduling, and the force path is patched to
capture the intent instead of re-execing the test process.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import pytest


def _install_shim():
    import server  # noqa: F401 — side-effect installs the full Flask→Quart shim
    return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class UpdateRestartGuardTest(unittest.TestCase):

    pytestmark = pytest.mark.auth_mode('private')

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
        from routes.api_v1.update import api_v1_update_bp
        cls.app.register_blueprint(api_v1_update_bp)

        from lib.api_keys import create_key
        _r, cls.admin_token = create_key(name='admin-test', scopes=['admin'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()

    def _hdr(self):
        return {'Authorization': f'Bearer {self.admin_token}'}

    def _post(self, json_body):
        async def go():
            return await self.app.test_client().post(
                '/api/v1/update/restart', headers=self._hdr(), json=json_body)
        return _run(go())

    def test_refused_when_sibling_tasks_running(self):
        running = [{'taskId': 'aaaaaaaa', 'convId': 'convX', 'elapsed': 12.3}]
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=running) as m, \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({})
        self.assertEqual(r.status_code, 409)
        body = _run(r.get_json())
        self.assertFalse(body['ok'])
        self.assertTrue(body.get('needsForce'))
        self.assertEqual(len(body.get('runningTasks') or []), 1)
        # The critical guarantee: no re-exec was scheduled.
        reexec.assert_not_called()
        m.assert_called_once()

    def test_own_conversation_excluded(self):
        # A conversation restarting itself should NOT be blocked by its own
        # running task — exclude_conv_id filters it out, leaving zero siblings.
        def _fake_list(exclude_conv_id=None):
            all_running = [{'taskId': 'own1', 'convId': 'convSelf', 'elapsed': 1.0}]
            return [t for t in all_running if t['convId'] != exclude_conv_id]

        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   side_effect=_fake_list), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'convId': 'convSelf'})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body['restarting'])
        reexec.assert_called_once()

    def test_force_overrides_running_tasks(self):
        running = [{'taskId': 'bbbbbbbb', 'convId': 'convY', 'elapsed': 5.0}]
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=running), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'force': True})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body['forced'])
        self.assertEqual(body['interruptedTasks'], 1)
        reexec.assert_called_once()

    def test_idle_allows_restart(self):
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        reexec.assert_called_once()


if __name__ == '__main__':
    unittest.main()

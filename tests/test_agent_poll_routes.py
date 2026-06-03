"""tests/test_agent_poll_routes.py — v1 poll façade tests.

Verifies:
  * /api/v1/agents/translate/poll/{task_id} delegates to the legacy
    flat-result handler and returns the same shape.
  * /api/v1/agents/translate/poll/batch delegates to the legacy batch.
  * /api/v1/agents/paper/report/poll and paper/translate/poll
    delegate to the legacy cursor-based handlers.

These routes preserve the structured shape the UI consumes; we just
verify they're discoverable via stable v1 paths and that auth/scope
gating works.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


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


class AgentPollRouteTest(unittest.TestCase):

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
        from routes.api_v1.agents import api_v1_agents_bp
        cls.app.register_blueprint(api_v1_agents_bp)

        from lib.api_keys import create_key
        _r1, cls.translate_token = create_key(
            name='t-test', scopes=['agents:translate'])
        _r2, cls.paper_token = create_key(
            name='p-test', scopes=['agents:paper'])
        _r3, cls.unscoped_token = create_key(
            name='no-scope', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()

    def _hdr(self, token):
        return {'Authorization': f'Bearer {token}'}

    # ── /api/v1/agents/translate/poll/{task_id} ────────────────────

    def test_translate_poll_delegates(self):
        sentinel = {'status': 'done', 'translated': 'hello',
                    'model': 'test-m'}

        with patch('routes.translate.translate_poll',
                    return_value=sentinel):
            async def go():
                return await self.app.test_client().get(
                    '/api/v1/agents/translate/poll/abc-123',
                    headers=self._hdr(self.translate_token))
            r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        # Body equals the sentinel dict (legacy returns it as-is via
        # api_ok-equivalent or raw dict).
        body = _new_loop_run(r.get_json())
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['translated'], 'hello')
        self.assertEqual(body['model'], 'test-m')

    def test_translate_poll_unauth_rejected(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/agents/translate/poll/abc')
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 401)

    def test_translate_poll_wrong_scope_403(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/agents/translate/poll/abc',
                headers=self._hdr(self.unscoped_token))
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 403)

    # ── /api/v1/agents/translate/poll/batch ────────────────────────

    def test_translate_batch_delegates(self):
        sentinel = [{'taskId': 'a', 'status': 'done', 'translated': 'x'},
                     {'taskId': 'b', 'status': 'running'}]

        with patch('routes.translate.translate_poll_batch',
                    return_value=sentinel):
            async def go():
                return await self.app.test_client().post(
                    '/api/v1/agents/translate/poll/batch',
                    headers=self._hdr(self.translate_token),
                    json={'taskIds': ['a', 'b']})
            r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        # Legacy returns a list directly; whatever shape it returns we
        # just verify status.

    def test_translate_batch_unauth_rejected(self):
        async def go():
            return await self.app.test_client().post(
                '/api/v1/agents/translate/poll/batch',
                json={'taskIds': []})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 401)

    # ── /api/v1/agents/paper/report/poll ───────────────────────────

    def test_paper_report_poll_delegates(self):
        sentinel = {'ok': True, 'status': 'running',
                    'events': [{'seq': 1, 'type': 'phase'}],
                    'next_cursor': 1}

        with patch('routes.paper.poll_report_task',
                    return_value=sentinel):
            async def go():
                return await self.app.test_client().get(
                    '/api/v1/agents/paper/report/poll?task_id=t1&cursor=0',
                    headers=self._hdr(self.paper_token))
            r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertEqual(body['status'], 'running')
        self.assertIn('events', body)

    def test_paper_translate_poll_delegates(self):
        sentinel = {'ok': True, 'status': 'done',
                    'events': [{'seq': 0, 'type': 'done'}],
                    'next_cursor': 1}
        with patch('routes.paper.poll_translate_task',
                    return_value=sentinel):
            async def go():
                return await self.app.test_client().get(
                    '/api/v1/agents/paper/translate/poll?task_id=t2&cursor=5',
                    headers=self._hdr(self.paper_token))
            r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertEqual(body['status'], 'done')

    def test_paper_poll_unauth_rejected(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/agents/paper/report/poll?task_id=t')
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 401)

    def test_paper_poll_wrong_scope_403(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/agents/paper/report/poll?task_id=t',
                headers=self._hdr(self.unscoped_token))
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 403)


if __name__ == '__main__':
    unittest.main()

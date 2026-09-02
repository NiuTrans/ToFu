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

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest


def _install_shim():
    """Install the FULL Flask→Quart shim by importing server.

    Previously this file hand-rolled a PARTIAL shim that only wrapped
    ``Request.get_json`` (and reverted it on teardown). That was both
    insufficient — it never wrapped ``make_response``, so a sync route
    returning a coroutine (e.g. the un-mocked paper-poll handler) made Quart
    raise ``TypeError: response value type (coroutine) is not valid`` — and
    harmful: reverting ``get_json`` partially UNDID server's real shim for
    later tests. ``import server`` runs ``_install_flask_shim()`` (idempotent;
    unwraps prior installs) and applies ALL the sync-safe wrappers. It is also
    already installed at conftest-import time, so this is normally a cached
    no-op kept here for standalone runs of this file. No revert needed — the
    full shim is the intended process-wide state.
    """
    import server  # noqa: F401 — side-effect installs the full shim
    return None


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AgentPollRouteTest(unittest.TestCase):

    # unauth → 401 assertions need the credential gate active, which it only
    # is in private/multi-user mode. The shared conftest defaults to 'open'
    # (synthetic admin for loopback); the per-test conftest fixture honours
    # this marker and forces private for every test method here.
    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        _install_shim()  # full Flask→Quart shim (idempotent; no revert)
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys

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
        _r1, cls.translate_token = create_key(owner_user_id=1, 
            name='t-test', scopes=['agents:translate'])
        _r2, cls.paper_token = create_key(owner_user_id=1, 
            name='p-test', scopes=['agents:paper'])
        _r3, cls.unscoped_token = create_key(owner_user_id=1, 
            name='no-scope', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        cls._tmp.cleanup()

    def _hdr(self, token):
        return {'Authorization': f'Bearer {token}'}

    # ── /api/v1/agents/translate/poll/{task_id} ────────────────────

    def test_translate_poll_delegates(self):
        sentinel = {'status': 'done', 'translated': 'hello',
                    'model': 'test-m'}

        with patch('routes.api_v1.translate.translate_poll_v1',
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

        with patch('routes.api_v1.translate.translate_poll_batch_v1',
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

        # poll_report_task is ``async def``, so a bare patch() yields an
        # AsyncMock whose call returns a coroutine — which the sync façade
        # route would hand to Quart verbatim (TypeError). Force a sync
        # MagicMock so the delegate returns the dict the route serializes.
        with patch('routes.paper_pkg._report.poll_report_task',
                    new_callable=MagicMock, return_value=sentinel):
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
        with patch('routes.paper_pkg._qa_translate.poll_translate_task',
                    new_callable=MagicMock, return_value=sentinel):
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

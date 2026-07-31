"""tests/test_update_restart_guard.py — self-update restart guard.

Root cause this guards against (2026-07-13 incident): ``POST
/api/v1/update/restart`` re-execs the whole server process, which kills EVERY
in-flight task across ALL conversations. In OPEN-auth mode an agent's own
``run_command`` probing the endpoint (``curl -X POST .../update/restart``)
silently interrupted its long-running sibling conversations — the "automatic
interruption" observed on conversations mrhz1e83i4itsu / mri07ozdjslr1d.

The 2026-07-28 incident hardened this further (epic pt_40d00fd526e5479a): an
autopilot conversation (ms4206iqwyb7h4) curl'ed this endpoint TWICE in three
minutes on a VU's "approval" (an LLM role-playing the owner — NOT a human),
killing 12 + 11 in-flight tasks. Owner ruling: restart/shutdown of a LIVE
server requires HUMAN approval. The route now:

  * answers 202 + {pendingApproval} when the request carries no ``approvalId``
    (NOTHING is executed — the human approves in the UI first);
  * 403s on an invalid/forged/expired/consumed token;
  * 409s on running sibling tasks with ``needsForce`` (the approval token is
    NOT consumed, so the force retry keeps it);
  * 429s inside the 15-minute restart cooldown (the anti-double-fire net that
    stops a crash-resume from re-firing a restart that already succeeded);
  * consumes the one-time token ONLY at acceptance.

We patch ``lib.tasks_pkg.manager.list_running_tasks`` so the test does not
depend on real task state, and we NEVER let the real ``_deferred_reexec``
run — the guard rejects before scheduling, and the accepted path is patched
to capture the intent instead of re-execing the test process. The approval
store lives in temp files (module-level paths monkeypatched).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

import lib.lifecycle_approval as la


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

        # Lifecycle approval store → temp files (never touch the real ones).
        cls._orig_approvals = la._APPROVALS_FILE
        cls._orig_state = la._STATE_FILE
        la._APPROVALS_FILE = os.path.join(cls._tmp.name, 'approvals.json')
        la._STATE_FILE = os.path.join(cls._tmp.name, 'state.json')

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
        la._APPROVALS_FILE = cls._orig_approvals
        la._STATE_FILE = cls._orig_state
        cls._tmp.cleanup()

    def setUp(self):
        # Fresh cooldown state per test (approvals may accumulate — ids are random).
        if os.path.exists(la._STATE_FILE):
            os.unlink(la._STATE_FILE)

    def _hdr(self):
        return {'Authorization': f'Bearer {self.admin_token}'}

    def _post(self, json_body, path='/api/v1/update/restart'):
        async def go():
            return await self.app.test_client().post(
                path, headers=self._hdr(), json=json_body)
        return _run(go())

    def _approved_token(self, action='restart'):
        rec = la.create_request(action, origin={'ua': 'pytest'})
        decided = la.decide(rec['id'], True)
        assert decided and decided['status'] == 'approved'
        return rec['id']

    # ── approval gate ────────────────────────────────────────────────

    def test_no_token_pends_nothing_scheduled(self):
        """THE gate: without an approvalId the endpoint answers 202 and the
        re-exec is NEVER scheduled — a unilateral agent curl is inert."""
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]) as m, \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'force': True, 'convId': 'convA'})
        self.assertEqual(r.status_code, 202)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body.get('needsApproval'))
        rec = body.get('pendingApproval') or {}
        self.assertEqual(rec.get('action'), 'restart')
        self.assertEqual(rec.get('status'), 'pending')
        self.assertTrue(rec.get('id'))
        # Attribution is recorded on the pending record (origin.conv_id).
        self.assertEqual((rec.get('origin') or {}).get('conv_id'), 'convA')
        reexec.assert_not_called()
        m.assert_called_once()

    def test_forged_token_403(self):
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': 'forged-token-does-not-exist'})
        self.assertEqual(r.status_code, 403)
        reexec.assert_not_called()

    def test_consumed_token_cannot_be_reused(self):
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec'):
            r1 = self._post({'approvalId': token})
            self.assertEqual(r1.status_code, 200)
            # Clear the acceptance cooldown so THIS assertion isolates the
            # one-time-token property (otherwise the 429 net answers first).
            if os.path.exists(la._STATE_FILE):
                os.unlink(la._STATE_FILE)
            # Second use of the SAME one-time token → 403 (one approval = one action).
            r2 = self._post({'approvalId': token})
            self.assertEqual(r2.status_code, 403)

    def test_action_mismatch_token_403(self):
        token = self._approved_token('shutdown')
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token})
        self.assertEqual(r.status_code, 403)
        reexec.assert_not_called()

    def test_cooldown_429(self):
        """A second restart inside the cooldown is refused — the anti
        double-fire net (crash-resume re-firing an already-fired restart)."""
        la.stamp_restart()
        token = self._approved_token()
        with patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token, 'force': True})
        self.assertEqual(r.status_code, 429)
        body = _run(r.get_json())
        self.assertFalse(body['ok'])
        self.assertGreater(body.get('retryAfterSec', 0), 0)
        reexec.assert_not_called()
        # The token was NOT consumed by the 429 (a retry after the window works).
        ok, _why = la.validate(token, 'restart')
        self.assertTrue(ok)

    # ── running-tasks guard (now behind the token) ───────────────────

    def test_refused_when_sibling_tasks_running(self):
        running = [{'taskId': 'aaaaaaaa', 'convId': 'convX', 'elapsed': 12.3}]
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=running) as m, \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token})
        self.assertEqual(r.status_code, 409)
        body = _run(r.get_json())
        self.assertFalse(body['ok'])
        self.assertTrue(body.get('needsForce'))
        self.assertEqual(len(body.get('runningTasks') or []), 1)
        # The critical guarantee: no re-exec was scheduled.
        reexec.assert_not_called()
        m.assert_called_once()
        # …and the 409 did NOT consume the token — the force retry rides it.
        ok, _why = la.validate(token, 'restart')
        self.assertTrue(ok)

    def test_own_conversation_excluded(self):
        # A conversation restarting itself should NOT be blocked by its own
        # running task — exclude_conv_id filters it out, leaving zero siblings.
        def _fake_list(exclude_conv_id=None):
            all_running = [{'taskId': 'own1', 'convId': 'convSelf', 'elapsed': 1.0}]
            return [t for t in all_running if t['convId'] != exclude_conv_id]

        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   side_effect=_fake_list), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'convId': 'convSelf', 'approvalId': token})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body['restarting'])
        reexec.assert_called_once()

    def test_force_overrides_running_tasks(self):
        running = [{'taskId': 'bbbbbbbb', 'convId': 'convY', 'elapsed': 5.0}]
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=running), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'force': True, 'approvalId': token})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertTrue(body['forced'])
        self.assertEqual(body['interruptedTasks'], 1)
        reexec.assert_called_once()

    def test_idle_allows_restart(self):
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token})
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        reexec.assert_called_once()
        # …and the cooldown was stamped for the accepted restart.
        self.assertGreater(la.restart_cooldown_remaining(), 0)

    # ── NEUTER: strip the gate and the attack succeeds ───────────────

    def test_neuter_gate_bypassed_means_unilateral_restart(self):
        """NEUTER proof: with validate+consume monkeypatched to always-allow
        (i.e. the gate neutered), a FORGED token restarts unilaterally —
        demonstrating the two checks above are what actually block it."""
        token = 'totally-forged'
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec, \
             patch.object(la, 'validate', return_value=(True, '')), \
             patch.object(la, 'consume', return_value=(True, '')):
            r = self._post({'approvalId': token})
        self.assertEqual(r.status_code, 200)   # ← neutered gate lets it through
        reexec.assert_called_once()

    # ── shutdown gate ────────────────────────────────────────────────

    def test_shutdown_pends_without_token(self):
        with patch('routes.api_v1.update._deferred_shutdown') as sd:
            r = self._post({}, path='/api/v1/update/shutdown')
        self.assertEqual(r.status_code, 202)
        body = _run(r.get_json())
        self.assertEqual((body.get('pendingApproval') or {}).get('action'), 'shutdown')
        sd.assert_not_called()

    def test_shutdown_with_approved_token(self):
        token = self._approved_token('shutdown')
        with patch('routes.api_v1.update._deferred_shutdown') as sd:
            r = self._post({'approvalId': token}, path='/api/v1/update/shutdown')
        self.assertEqual(r.status_code, 200)
        sd.assert_called_once()

    # ── approval list/decide endpoints (the human UI surface) ───────

    def test_list_and_decide_endpoints(self):
        rec = la.create_request('restart', origin={'ua': 'pytest-list'})
        async def go():
            c = self.app.test_client()
            r_list = await c.get('/api/v1/update/lifecycle-approvals?status=pending',
                                 headers=self._hdr())
            r_dec = await c.post(
                f"/api/v1/update/lifecycle-approvals/{rec['id']}/decide",
                headers=self._hdr(), json={'approved': True})
            r_get = await c.get(f"/api/v1/update/lifecycle-approvals/{rec['id']}",
                                headers=self._hdr())
            return r_list, r_dec, r_get
        r_list, r_dec, r_get = _run(go())
        self.assertEqual(r_list.status_code, 200)
        listed = _run(r_list.get_json())
        self.assertTrue(any(x['id'] == rec['id'] for x in listed.get('records', [])))
        self.assertEqual(r_dec.status_code, 200)
        self.assertEqual((_run(r_dec.get_json())).get('record', {}).get('status'), 'approved')
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual((_run(r_get.get_json())).get('record', {}).get('id'), rec['id'])

    def test_decide_unknown_id_404(self):
        async def go():
            return await self.app.test_client().post(
                '/api/v1/update/lifecycle-approvals/nope/decide',
                headers=self._hdr(), json={'approved': True})
        r = _run(go())
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()

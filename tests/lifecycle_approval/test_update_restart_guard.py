"""tests/lifecycle_approval/test_update_restart_guard.py — self-update restart guard.

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
    NOT consumed, so the force retry keeps it) — on BOTH wire shapes: the
    v1 ``{ok:false,...}`` envelope and the v4 RFC 7807 problem body carry
    the same receipt members (2026-09-29 parity fix);
  * 429s inside the restart cooldown ONLY when the operator opts in via
    TOFU_LIFECYCLE_RESTART_COOLDOWN (the window is 0/off by default since the
    2026-09-20 owner ruling — the consumed one-time token is the real
    anti-double-fire net);
  * consumes the one-time token ONLY at acceptance.

We patch ``lib.tasks_pkg.manager.list_running_tasks`` so the test does not
depend on real task state, and we NEVER let the real ``_deferred_reexec``
run — the guard rejects before scheduling, and the accepted path is patched
to capture the intent instead of re-execing the test process. The approval
store lives in temp files (module-level paths monkeypatched).
"""


from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import logging
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


@pytest.mark.unit
def test_restart_preparation_failure_is_visible(monkeypatch, caplog):
    import routes.api_v1.update as update_route

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError('synthetic frontend preparation failure')

    monkeypatch.setattr(
        'serverctl_pkg.application_restart.run_application_restart_control', fail_prepare)
    with caplog.at_level(logging.WARNING, logger='routes.api_v1.update'):
        failure = update_route._prepare_server_reexec_frontend()
    assert failure == 'synthetic frontend preparation failure'
    assert failure in caplog.text


class UpdateRestartGuardTest(unittest.TestCase):

    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys

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
        # The dual-mounted v4 twin, so refusal receipts are asserted at the
        # exact boundary the browser restart button talks to.
        from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
        cls.app.register_blueprint(attach_v4_envelope(
            copy_blueprint_to_v4(api_v1_update_bp, name='api_v1_update_v4')))

        from lib.api_keys import create_key
        _r, cls.admin_token = create_key(owner_user_id=1, name='admin-test', scopes=['admin'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        la._APPROVALS_FILE = cls._orig_approvals
        la._STATE_FILE = cls._orig_state
        cls._tmp.cleanup()

    def setUp(self):
        # The approval document now owns both token state and the atomic
        # restart-cooldown authority. Isolate both stores per test.
        for path in (la._APPROVALS_FILE, la._STATE_FILE):
            if os.path.exists(path):
                os.unlink(path)
        self._restart_preflight = patch(
            'routes.api_v1.update._prepare_server_reexec_frontend',
            return_value='',
        )
        self._restart_preflight.start()
        self.addCleanup(self._restart_preflight.stop)

    def _clear_cooldown_only(self):
        def _mut(document):
            document = dict(document or {})
            document.pop('last_restart_at', None)
            document.pop('last_restart_pid', None)
            return document

        la.update_json_atomic(
            la._APPROVALS_FILE, _mut, default={'records': []}, strict=True)
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
            self._clear_cooldown_only()
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
        """With the cooldown OPTED IN, a second restart inside the window is
        refused — the belt-and-suspenders net behind the consumed token."""
        token = self._approved_token()
        with patch.object(la, 'RESTART_COOLDOWN_SEC', 900), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            la.stamp_restart()
            r = self._post({'approvalId': token, 'force': True})
        self.assertEqual(r.status_code, 429)
        body = _run(r.get_json())
        self.assertFalse(body['ok'])
        self.assertGreater(body.get('retryAfterSec', 0), 0)
        self.assertTrue(body.get('restartPending'))
        self.assertIn('has not happened yet', str(body.get('error') or ''))
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

    def test_v4_refusal_keeps_lifecycle_receipt_members(self):
        """v4 parity: the RFC 7807 problem body must carry the lifecycle
        receipt members (needsForce/runningTasks). The frontend restart
        button reads them off the 409 body to show the informed
        force-confirm; when the v4 problem adapter dropped every route extra
        (2026-09-29 incident: only the v1 shape carried them), the button
        could only render a dead-end refusal card."""
        running = [{'taskId': 'cccccccc', 'convId': 'convZ', 'elapsed': 7.5}]
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=running), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token}, path='/api/v4/update/restart')
        self.assertEqual(r.status_code, 409)
        self.assertIn('application/problem+json', r.content_type or '')
        body = _run(r.get_json())
        # Standard problem members…
        self.assertEqual(body.get('code'), 'conflict')
        self.assertIn('Restart refused', str(body.get('detail') or ''))
        # …plus the declared lifecycle receipts the frontend reads.
        self.assertTrue(body.get('needsForce'))
        self.assertEqual(len(body.get('runningTasks') or []), 1)
        self.assertEqual((body['runningTasks'][0] or {}).get('taskId'), 'cccccccc')
        # The v1 envelope shape must not leak into the problem body.
        self.assertNotIn('ok', body)
        self.assertNotIn('error', body)
        reexec.assert_not_called()
        # The refusal still did not consume the token — the v4 force retry
        # rides the same approval.
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
        with patch.object(la, 'RESTART_COOLDOWN_SEC', 900), \
             patch('lib.tasks_pkg.manager.list_running_tasks',
                   return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec:
            r = self._post({'approvalId': token})
            self.assertEqual(r.status_code, 200)
            body = _run(r.get_json())
            self.assertTrue(body['ok'])
            reexec.assert_called_once()
            # …and the acceptance stamp was recorded (remaining reflects the
            # pinned window; with the shipped default of 0 the stamp is a
            # no-op).
            self.assertGreater(la.restart_cooldown_remaining(), 0)

    def test_second_restart_allowed_by_default(self):
        """2026-09-20 owner ruling: the cooldown is OFF by default — two
        independently human-approved restarts back-to-back both execute
        (the consumed token, not a wall-clock window, stops double-fire)."""
        for _ in range(2):
            token = self._approved_token()
            with patch('lib.tasks_pkg.manager.list_running_tasks',
                       return_value=[]), \
                 patch('routes.api_v1.update._deferred_reexec') as reexec:
                r = self._post({'approvalId': token, 'force': True})
            self.assertEqual(r.status_code, 200)
            reexec.assert_called_once()

    def test_frontend_preflight_failure_keeps_server_and_approval_live(self):
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks', return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec, \
             patch(
                 'routes.api_v1.update._prepare_server_reexec_frontend',
                 return_value='stale locale artifact'):
            r = self._post({'approvalId': token})

        self.assertEqual(r.status_code, 409)
        body = _run(r.get_json())
        self.assertTrue(body.get('restartPreflightFailed'))
        self.assertIn('still running', str(body.get('error') or ''))
        self.assertIn('stale locale artifact', str(body.get('error') or ''))
        # The full preflight log ALSO rides the detail extra so the restart
        # card can render it in a copyable block without re-parsing.
        self.assertIn('stale locale artifact', str(body.get('detail') or ''))
        reexec.assert_not_called()
        self.assertEqual(la.validate(token, 'restart'), (True, ''))
        self.assertEqual(la.restart_cooldown_remaining(), 0)

    def test_actual_preparation_phase_is_readable_before_restart_acceptance(self):
        import time
        from types import SimpleNamespace
        from serverctl_pkg.native_engine import _report_stage
        token = self._approved_token()
        snapshots = []
        clock = [100.0]
        phases = ('compiling', 'retrying', 'checking', 'validating', 'frontend')
        def preflight():
            for phase in phases:
                clock[0] += 10
                _report_stage(phase, 'raw compiler output must not reach progress')
                snapshots.append(la.get(token))
                self.assertEqual(la.validate(token, 'restart'), (True, ''))
            return 'synthetic validation refusal'
        with patch('lib.tasks_pkg.manager.list_running_tasks', return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec, \
             patch('routes.api_v1.update.time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time)), \
             patch('routes.api_v1.update._prepare_server_reexec_frontend', side_effect=preflight):
            result = self._post({'approvalId': token}, path='/api/v4/update/restart')
        self.assertEqual(result.status_code, 409)
        self.assertEqual([record['preparation']['phase'] for record in snapshots], list(phases))
        self.assertEqual([record['preparation']['elapsedSeconds'] for record in snapshots],
                         [10, 20, 30, 40, 50])
        self.assertTrue(all(record['status'] == 'approved' for record in snapshots))
        self.assertTrue(all(set(record['preparation']) == {'phase', 'elapsedSeconds', 'updatedAt'}
                            for record in snapshots))
        self.assertEqual(la.get(token)['preparation']['phase'], 'failed')
        self.assertEqual(la.validate(token, 'restart'), (True, ''))
        reexec.assert_not_called()

    def test_v4_frontend_preflight_preserves_reason_and_approval(self):
        token = self._approved_token()
        with patch('lib.tasks_pkg.manager.list_running_tasks', return_value=[]), \
             patch('routes.api_v1.update._deferred_reexec') as reexec, \
             patch(
                 'routes.api_v1.update._prepare_server_reexec_frontend',
                 return_value='stale locale artifact'):
            r = self._post({'approvalId': token}, path='/api/v4/update/restart')

        self.assertEqual(r.status_code, 409)
        body = _run(r.get_json())
        self.assertTrue(body.get('restartPreflightFailed'))
        self.assertIn('still running', str(body.get('detail') or ''))
        self.assertIn('stale locale artifact', str(body.get('detail') or ''))
        reexec.assert_not_called()
        self.assertEqual(la.validate(token, 'restart'), (True, ''))
        self.assertEqual(la.restart_cooldown_remaining(), 0)

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
             patch.object(la, 'consume_restart',
                          return_value=(True, '', 0)):
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


@pytest.mark.unit
def test_restart_reuses_successful_source_preparation_without_duplicate_reads(monkeypatch):
    import serverctl
    import lib.vite_assets as assets
    from serverctl_pkg.application_restart import main
    calls = []
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *_, **__: '')
    monkeypatch.setattr(serverctl, 'prepare_source_frontend_artifact', lambda reason, **options: calls.append((reason, options)) or '')
    monkeypatch.setattr(assets, 'validate_vite_artifact', lambda: pytest.fail('the owner already validated'))
    assert main(['--stage', 'prepare']) == 0
    assert calls == [('in-app restart', {'capture_output': True})]

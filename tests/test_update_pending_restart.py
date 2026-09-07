#!/usr/bin/env python3
"""tests/test_update_pending_restart.py — persisted apply state →
/update/check projection (the reload-robust "download finished, restart?"
path).

WHY
---
A self-update download runs 5-15 minutes; the user closes or reloads the
page. Push frames are transient and the frontend's in-memory state dies with
the page, so a finished download used to leave NO trace: /update/check just
said "update available", inviting a SECOND 50MB+ download (tarball path) or
an "already up to date" card that never offered the restart the landed code
still needs (git path). The apply worker now persists its terminal state
(``update_apply_state.json``) and ``_enrich_with_apply_state`` projects:

  * ``pending_restart``   — code landed for new_version while the running
                            process still serves an older one (self-clears
                            after the restart reports the new version);
  * ``apply_in_progress`` — only while the worker thread is verifiably alive
                            in THIS process (a stale 'running' marker left by
                            a dead process is rewritten to 'interrupted').

NEUTER: the shipped-source needle asserts update_check routes through the
enricher — delete the call and the route-level test goes red.
"""


from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _install_shim():
    import server  # noqa: F401 — installs the full Flask→Quart shim


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class EnrichWithApplyStateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        import routes.api_v1.update as upd
        cls.upd = upd
        cls._orig_path = upd._apply_state_path
        cls._state_file = os.path.join(cls._tmp.name, 'update_apply_state.json')
        upd._apply_state_path = lambda: cls._state_file

    @classmethod
    def tearDownClass(cls):
        cls.upd._apply_state_path = cls._orig_path
        cls._tmp.cleanup()

    def setUp(self):
        if os.path.exists(self._state_file):
            os.unlink(self._state_file)
        self.upd._ACTIVE_APPLIES.clear()

    def _write(self, state: dict):
        with open(self._state_file, 'w', encoding='utf-8') as fh:
            json.dump(state, fh)

    def _enrich(self):
        return self.upd._enrich_with_apply_state({'current': '0.16.0'})

    # ── pending_restart ──────────────────────────────────────────────

    def test_done_newer_version_projects_pending_restart(self):
        self._write({'status': 'done', 'ok': True, 'changed': True,
                     'needs_restart': True, 'old_version': '0.16.0',
                     'new_version': '0.17.0', 'method': 'git',
                     'finished_at': time.time(),
                     'deps_changed': False, 'deps_installed': False})
        with patch('lib.self_update._version.current_version',
                   return_value='0.16.0'):
            payload = self._enrich()
        pr = payload.get('pending_restart')
        self.assertIsNotNone(pr, payload)
        self.assertEqual(pr['new_version'], '0.17.0')
        self.assertTrue(pr['changed'])

    def test_pending_restart_self_clears_after_restart(self):
        """Once the running process reports the NEW version, the projection
        disappears — no ack endpoint needed."""
        self._write({'status': 'done', 'ok': True, 'changed': True,
                     'needs_restart': True, 'new_version': '0.17.0',
                     'finished_at': time.time()})
        with patch('lib.self_update._version.current_version',
                   return_value='0.17.0'):
            payload = self._enrich()
        self.assertNotIn('pending_restart', payload)

    def test_no_state_or_not_needed_means_no_projection(self):
        self.assertNotIn('pending_restart', self._enrich())  # no file at all
        self._write({'status': 'done', 'ok': True, 'changed': False,
                     'needs_restart': False, 'new_version': '0.16.0'})
        self.assertNotIn('pending_restart', self._enrich())

    # ── apply_in_progress ────────────────────────────────────────────

    def test_running_with_live_thread_projects_in_progress(self):
        self._write({'status': 'running', 'task_id': 't1',
                     'started_at': time.time(), 'old_version': '0.16.0'})
        stop = threading.Event()
        th = threading.Thread(target=stop.wait, daemon=True)
        th.start()
        self.upd._ACTIVE_APPLIES['t1'] = th
        try:
            payload = self._enrich()
            aip = payload.get('apply_in_progress')
            self.assertIsNotNone(aip, payload)
            self.assertEqual(aip['task_id'], 't1')
        finally:
            stop.set()
            th.join(timeout=5)

    def test_stale_running_marker_rewritten_to_interrupted(self):
        """A 'running' marker whose owner process died mid-apply is rewritten
        once to 'interrupted' and never surfaces apply_in_progress."""
        self._write({'status': 'running', 'task_id': 'ghost',
                     'started_at': time.time() - 600})
        payload = self._enrich()
        self.assertNotIn('apply_in_progress', payload)
        with open(self._state_file, encoding='utf-8') as fh:
            rewritten = json.load(fh)
        self.assertEqual(rewritten['status'], 'interrupted')
        self.assertIn('finished_at', rewritten)

    # ── shipped-source needle (NEUTER) ───────────────────────────────

    def test_update_check_wires_the_enricher(self):
        import inspect
        src = inspect.getsource(self.upd.update_check)
        self.assertIn('_enrich_with_apply_state(payload)', src)


class UpdateCheckRouteTest(unittest.TestCase):
    """Route level: GET /api/v1/update/check carries the projection in its
    JSON envelope (and drops it when the enricher is bypassed — NEUTER)."""

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys

        import routes.api_v1.update as upd
        cls.upd = upd
        cls._orig_state = upd._apply_state_path
        cls._state_file = os.path.join(cls._tmp.name, 'update_apply_state.json')
        upd._apply_state_path = lambda: cls._state_file

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        cls.app.register_blueprint(upd.api_v1_update_bp)

        from lib.api_keys import create_key
        _r, cls.token = create_key(owner_user_id=1, name='upd-check-test', scopes=['admin'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        cls.upd._apply_state_path = cls._orig_state
        cls._tmp.cleanup()

    def setUp(self):
        if os.path.exists(self._state_file):
            os.unlink(self._state_file)
        self.upd._ACTIVE_APPLIES.clear()

    def _get(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/update/check',
                headers={'Authorization': f'Bearer {self.token}'})
        return _run(go())

    _FAKE_CHECK = {'current': '0.16.0', 'latest': '0.17.0', 'tag': 'v0.17.0',
                   'update_available': True, 'dirty': False, 'blocking': [],
                   'update_method': 'git'}

    def test_route_projects_pending_restart(self):
        with open(self._state_file, 'w', encoding='utf-8') as fh:
            json.dump({'status': 'done', 'ok': True, 'changed': True,
                       'needs_restart': True, 'new_version': '0.17.0',
                       'finished_at': time.time()}, fh)
        with patch('lib.self_update.check_for_update',
                   return_value=dict(self._FAKE_CHECK)), \
             patch('lib.self_update._version.current_version',
                   return_value='0.16.0'):
            r = self._get()
        self.assertEqual(r.status_code, 200)
        body = _run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertEqual(body['pending_restart']['new_version'], '0.17.0')

    def test_neuter_bypassed_enricher_loses_projection(self):
        """NEUTER proof: bypass the enricher (identity passthrough) and the
        very same state file produces NO pending_restart — the enrichment is
        what carries the projection, not the check payload itself."""
        with open(self._state_file, 'w', encoding='utf-8') as fh:
            json.dump({'status': 'done', 'ok': True, 'changed': True,
                       'needs_restart': True, 'new_version': '0.17.0',
                       'finished_at': time.time()}, fh)
        with patch('lib.self_update.check_for_update',
                   return_value=dict(self._FAKE_CHECK)), \
             patch.object(self.upd, '_enrich_with_apply_state',
                          side_effect=lambda p: p):
            r = self._get()
        body = _run(r.get_json())
        self.assertNotIn('pending_restart', body)


if __name__ == '__main__':
    unittest.main()

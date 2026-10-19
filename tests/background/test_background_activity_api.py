"""tests/background/test_background_activity_api.py — owner-scoped background activity.

Covers the two halves of ``GET /api/v1/activity/background``:

* ``list_active_swarms`` (lib/swarm/integration/_state.py) — live-session
  listing: owner filter, terminated filter, payload shape. Sessions are
  lightweight stubs registered straight into the shared registry; no LLM.
* The route itself (routes/api_v1/activity.py) — auth required, empty
  steady state, and a seeded registry surfacing both a live swarm and a
  running background command.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

import pytest

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit

from lib.swarm.integration import _active_sessions, _session_timestamps, _sessions_lock
from lib.swarm.integration import list_active_swarms
from lib.tasks_pkg import background_commands as bgc

_TEST_OWNER = 1
_OTHER_OWNER = 2


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubSession:
    """MasterOrchestrator stand-in exposing only the activity surface."""

    def __init__(self, *, user_id: int, conv_id: str, terminated: bool = False,
                 agents: dict | None = None, running: int = 0, pending: int = 0,
                 completed: int = 0):
        self.user_id = user_id
        self.conv_id = conv_id
        self.task_id = 'run-' + conv_id
        self._lock = threading.Lock()
        self.specs = []
        self.is_terminated = terminated
        self._agents = agents or {}
        self.running_count = running
        self.pending_count = pending
        self.completed_count = completed

    def get_status(self):
        return dict(self._agents)


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    monkeypatch.setattr('lib.swarm.message_receipts.snapshot_receipts',
                        lambda session, agents, specs: {
                            'messageReceipts': [], 'messageReceiptsVersions': {},
                            'messageReceiptsAvailable': True, 'messageParticipants': []})

    keys = []
    with _sessions_lock:
        keys = [k for k in _active_sessions if k.startswith('acttest-')]
        for k in keys:
            _active_sessions.pop(k, None)
            _session_timestamps.pop(k, None)
    with bgc._lock:
        bgc._registry.clear()
    yield
    with _sessions_lock:
        for k in [k for k in _active_sessions if k.startswith('acttest-')]:
            _active_sessions.pop(k, None)
            _session_timestamps.pop(k, None)
    with bgc._lock:
        bgc._registry.clear()


def _register(key: str, session: _StubSession, *, created_at: float | None = None):
    with _sessions_lock:
        _active_sessions[key] = session
        _session_timestamps[key] = created_at if created_at is not None else time.time()


class TestListActiveSwarms(unittest.TestCase):

    def test_empty_registry_returns_empty(self):
        self.assertEqual(list_active_swarms(user_id=_TEST_OWNER), [])

    def test_live_session_shape(self):
        _register('acttest-live', _StubSession(
            user_id=_TEST_OWNER, conv_id='conv-1',
            agents={'a1': {'role': 'coder', 'objective': 'do x',
                           'status': 'running', 'round': 2, 'error': ''}},
            running=1, pending=2, completed=3))
        out = list_active_swarms(user_id=_TEST_OWNER)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertEqual(item['swarm_key'], 'acttest-live')
        self.assertEqual(item['swarm_id'], 'run-conv-1')
        self.assertIs(item['messageReceiptsAvailable'], True)
        self.assertEqual(item['messageReceipts'], [])
        self.assertEqual(item['conv_id'], 'conv-1')
        self.assertEqual(item['running'], 1)
        # Deliberately conflicting pool counters above cannot override rows.
        self.assertEqual(item['pending'], 0)
        self.assertEqual(item['completed'], 0)
        self.assertEqual(item['agent_count'], 1)
        self.assertEqual(item['agents'][0]['id'], 'a1')
        self.assertEqual(item['agents'][0]['role'], 'coder')
        self.assertGreater(item['created_at'], 0)

    def test_terminated_sessions_excluded(self):
        _register('acttest-dead', _StubSession(
            user_id=_TEST_OWNER, conv_id='conv-1', terminated=True))
        self.assertEqual(list_active_swarms(user_id=_TEST_OWNER), [])

    def test_other_owners_sessions_excluded(self):
        _register('acttest-foreign', _StubSession(
            user_id=_OTHER_OWNER, conv_id='conv-9'))
        self.assertEqual(list_active_swarms(user_id=_TEST_OWNER), [])
        self.assertEqual(
            len(list_active_swarms(user_id=_OTHER_OWNER)), 1)


class TestBackgroundActivityRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from lib.api_keys import create_key
        _row, cls.token = create_key(
            owner_user_id=_TEST_OWNER, name='activity-probe', scopes=['chat'])
        from quart import Quart
        from routes.api_v1.auth import bearer_auth_before_request
        from routes.api_v1.activity import api_v1_activity_bp
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        cls.app.before_request(bearer_auth_before_request)
        cls.app.register_blueprint(api_v1_activity_bp)

    def _client(self):
        return self.app.test_client()

    def test_empty_steady_state(self):
        async def go():
            r = await self._client().get(
                '/api/v1/activity/background',
                headers={'Authorization': f'Bearer {self.token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(body['swarms'], [])
            self.assertEqual(body['backgroundCommands'], [])
        _run(go())

    def test_seeded_activity_surfaces(self):
        _register('acttest-route', _StubSession(
            user_id=_TEST_OWNER, conv_id='conv-route',
            agents={'a1': {'role': 'researcher', 'objective': 'survey',
                           'status': 'running', 'round': 1, 'error': ''}},
            running=1))
        gate = threading.Event()
        cid = bgc.start_background_command(
            task={'id': 'acttest-task', 'convId': 'conv-route',
                  '_userId': _TEST_OWNER},
            command='sleep 60',
            execute=lambda t, _ctx: (gate.wait(5), '$ sleep 60\n[exit code: 0]')[1])
        try:
            async def go():
                r = await self._client().get(
                    '/api/v1/activity/background',
                    headers={'Authorization': f'Bearer {self.token}'})
                self.assertEqual(r.status_code, 200)
                body = await r.get_json()
                data = body
                self.assertEqual(len(data['swarms']), 1)
                swarm = data['swarms'][0]
                self.assertEqual(swarm['swarm_key'], 'acttest-route')
                self.assertEqual(swarm['conv_id'], 'conv-route')
                self.assertEqual(swarm['running'], 1)
                commands = data['backgroundCommands']
                self.assertEqual(len(commands), 1)
                self.assertEqual(commands[0]['command_id'], cid)
                self.assertEqual(commands[0]['conv_id'], 'conv-route')
                self.assertEqual(commands[0]['command'], 'sleep 60')
                self.assertEqual(commands[0]['status'], 'running')
                self.assertNotIn('result', commands[0])
            _run(go())
        finally:
            gate.set()

    def test_finished_commands_not_listed(self):
        cid = bgc.start_background_command(
            task={'id': 'acttest-task-2', 'convId': '', '_userId': _TEST_OWNER},
            command='echo hi',
            execute=lambda t: '$ echo hi\nhi\n[exit code: 0]')
        for _ in range(100):
            snap = bgc.check_background_command(cid, user_id=_TEST_OWNER)
            if snap and snap['status'] == 'completed':
                break
            time.sleep(0.02)
        async def go():
            r = await self._client().get(
                '/api/v1/activity/background',
                headers={'Authorization': f'Bearer {self.token}'})
            body = await r.get_json()
            self.assertEqual(body['backgroundCommands'], [])
        _run(go())


if __name__ == '__main__':
    unittest.main()

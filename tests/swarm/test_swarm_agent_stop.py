"""Owner Stop for ONE running swarm agent — the background-agent dock's control.

``stop_swarm_agent`` (lib/swarm/integration/_state.py) sends the same signal
as the model-facing ``kill`` tool (``MasterOrchestrator.kill_agent``): the
named worker settles ``cancelled`` while its siblings and the run continue.
It is addressed by the exact run ID that ``GET /api/v4/activity/background``
publishes (``swarms[].swarm_id``), so a stale browser row reaches neither
another owner's run nor a later run that took over the conversation key.
The HTTP adapter maps the typed outcome onto 200 / 404
``swarm_agent_not_found`` / 409 ``swarm_agent_not_running``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
import threading
import time
from unittest.mock import patch

import pytest

from lib import agent_inbox
from lib.swarm.integration import (
    _active_sessions,
    _session_timestamps,
    _sessions_lock,
    add_session_alias,
    execute_swarm_tool,
    stop_swarm_agent,
)
from lib.swarm.protocol import SubAgentResult, SubAgentStatus

pytestmark = pytest.mark.unit
OWNER = 1
NOT_FOUND = {'outcome': 'not_found', 'status': ''}


def _wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _forget(key: str) -> None:
    with _sessions_lock:
        _active_sessions.pop(key, None)
        _session_timestamps.pop(key, None)
    agent_inbox.reset_for_test(key)


def _status(session, agent_id: str) -> str:
    return session.get_status()[agent_id]['status']


class _LiveAgent:
    """Worker stand-in whose live invocation honours its abort flag (no LLM)."""

    def __init__(self, spec, release: dict, release_all: threading.Event):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self._child = {'aborted': False}
        self._release = release[spec.id]
        self._release_all = release_all
        self.result = SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                                     final_answer=f'Answer {spec.id}', rounds_used=1)

    def request_abort(self) -> bool:
        self._child['aborted'] = True
        return True

    def run(self) -> SubAgentResult:
        _wait_until(lambda: self._child['aborted'] or self._release.is_set()
                    or self._release_all.is_set())
        if self._child['aborted']:
            self.result.status = SubAgentStatus.CANCELLED.value
            self.result.final_answer = ''
        return self.result


@pytest.fixture
def swarm(request):
    task_id = f'stop-agent-{request.node.name}'
    release: dict[str, threading.Event] = defaultdict(threading.Event)
    release_all = threading.Event()
    spawned = []
    _forget(task_id)
    with patch('lib.swarm.master._build_sub_agent',
               side_effect=lambda spec, **_: _LiveAgent(spec, release, release_all)):
        def spawn(*agents):
            execute_swarm_tool('spawn_agents', {'agents': list(agents)},
                               task={'id': task_id, '_userId': OWNER})
            session = _active_sessions[task_id]
            spawned.append(session)
            return session

        yield spawn, release
        release_all.set()
        for session in spawned:
            assert _wait_until(lambda: session.is_terminated)
    _forget(task_id)


def test_owner_stop_cancels_only_the_named_running_agent(swarm):
    spawn, release = swarm
    session = spawn({'id': 'a', 'objective': 'no longer needed'},
                    {'id': 'b', 'objective': 'still needed'})
    assert _wait_until(lambda: _status(session, 'a') == _status(session, 'b') == 'running')

    assert stop_swarm_agent(session.task_id, 'a', user_id=OWNER) == {
        'outcome': 'stopped', 'status': 'running'}

    assert _wait_until(lambda: _status(session, 'a') == 'cancelled')
    assert _status(session, 'b') == 'running'
    assert not session.is_terminated
    release['b'].set()
    assert _wait_until(lambda: _status(session, 'b') == 'completed')


def test_stop_is_scoped_to_the_owner_and_the_exact_live_run(swarm):
    spawn, _release = swarm
    session = spawn({'id': 'a', 'objective': 'work'})
    assert _wait_until(lambda: _status(session, 'a') == 'running')
    # An older run's task ID still aliases this conversation's session key,
    # but it is not this run's identity.
    add_session_alias('older-run', session.task_id)

    assert stop_swarm_agent(session.task_id, 'a', user_id=OWNER + 1) == NOT_FOUND
    assert stop_swarm_agent('older-run', 'a', user_id=OWNER) == NOT_FOUND
    assert stop_swarm_agent(session.task_id, 'missing', user_id=OWNER) == NOT_FOUND
    assert _status(session, 'a') == 'running'


def test_pending_and_settled_agents_are_not_stoppable(swarm):
    spawn, release = swarm
    session = spawn({'id': 'a', 'objective': 'finishes first'},
                    {'id': 'b', 'objective': 'keeps running'},
                    {'id': 'c', 'objective': 'waits for b', 'depends_on': ['b']})
    assert _wait_until(lambda: _status(session, 'b') == 'running')

    assert stop_swarm_agent(session.task_id, 'c', user_id=OWNER) == {
        'outcome': 'not_running', 'status': 'pending'}
    release['a'].set()
    assert _wait_until(lambda: _status(session, 'a') == 'completed')
    assert stop_swarm_agent(session.task_id, 'a', user_id=OWNER) == {
        'outcome': 'not_running', 'status': 'completed'}
    assert _status(session, 'b') == 'running'
    assert _status(session, 'c') == 'pending'


class _RouteSession:
    """Registry stand-in exposing exactly what the stop path reads."""

    def __init__(self, statuses: dict[str, str]):
        self.task_id = 'route-run'
        self.user_id = OWNER
        self.statuses = statuses
        self.killed: list[str] = []

    def get_status(self):
        return {agent_id: {'status': status} for agent_id, status in self.statuses.items()}

    def kill_agent(self, agent_id: str) -> bool:
        self.killed.append(agent_id)
        return True


def test_http_stop_maps_outcomes_and_requires_swarm_scope():
    from quart import g, request

    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.app.app_factory import create_base_app
    from routes.api_v1.swarm import api_v1_swarm_bp

    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def _authenticate():
        g.auth_ctx = AuthContext(
            key_id='stop-test', name='stop-test',
            scopes=frozenset(filter(None, request.headers.get('X-Scopes', '').split(','))),
            owner_user_id=int(request.headers.get('X-Owner', OWNER)))
        g.rate_decision = None

    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        api_v1_swarm_bp, name='swarm_agent_stop_v4_test')))
    session = _RouteSession({'a': 'running', 'b': 'pending'})
    _forget('route-key')
    with _sessions_lock:
        _active_sessions['route-key'] = session
        _session_timestamps['route-key'] = time.time()
    scoped = {'X-Scopes': 'agents:swarm'}

    async def exercise():
        client = app.test_client()
        path = '/api/v4/swarm/runs/{run}/agents/{agent}/stop'

        denied = await client.post(path.format(run='route-run', agent='a'),
                                   headers={'X-Scopes': 'chat'})
        assert denied.status_code == 403 and session.killed == []

        foreign = await client.post(path.format(run='route-run', agent='a'),
                                    headers={**scoped, 'X-Owner': str(OWNER + 1)})
        assert foreign.status_code == 404
        assert (await foreign.get_json())['code'] == 'swarm_agent_not_found'
        stale = await client.post(path.format(run='route-key', agent='a'), headers=scoped)
        assert stale.status_code == 404 and session.killed == []

        pending = await client.post(path.format(run='route-run', agent='b'), headers=scoped)
        assert pending.status_code == 409
        problem = await pending.get_json()
        assert problem['code'] == 'swarm_agent_not_running'
        assert 'pending' in problem['detail'] and session.killed == []

        stopped = await client.post(path.format(run='route-run', agent='a'), headers=scoped)
        assert stopped.status_code == 200
        data = (await stopped.get_json())['data']
        assert (data['stopped'], data['swarmId'], data['agentId']) == (True, 'route-run', 'a')
        assert session.killed == ['a']

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()
        _forget('route-key')

"""Swarm controls reach the execution host that owns the swarm session.

In the split deployment the API process serves the swarm routes, while every
durable conversation, and the swarm it spawned, runs in the resident execution
host. The routes therefore address a session through the private execution
control channel (``lib/tasks_pkg/command_control_runtime.py``), as run_command
interrupt and background already do. The execution host here is a real second
process: its session registry holds the swarm, and a ``CommandControlServer``
applies the real controls to it. The API process's own registry is empty.
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
OWNER = 7
KEY = 'xctl-conversation'
RUN = 'xctl-run'
ROOT = Path(__file__).resolve().parents[2]

_EXECUTION_HOST = f'''
import asyncio, json, sys, threading, time
import lib.swarm.message_receipts as receipts
from lib.swarm.integration import _active_sessions, _session_timestamps, _sessions_lock
from lib.tasks_pkg.command_control_transport import CommandControlServer
from lib.tasks_pkg.command_controls import apply_command_control

receipts.snapshot_receipts = lambda session, agents, specs: {{'messageReceipts': []}}


class Session:
    def __init__(self):
        self.task_id, self.user_id, self.conv_id = {RUN!r}, {OWNER!r}, {KEY!r}
        self.specs, self._lock = [], threading.Lock()
        self.is_terminated = self.aborted = False
        self.killed = []
        self.statuses = {{'a': 'running', 'b': 'pending'}}

    def get_status(self):
        return {{agent: {{'status': status}} for agent, status in self.statuses.items()}}

    def kill_agent(self, agent):
        self.killed.append(agent)
        return True

    def abort(self):
        self.aborted = self.is_terminated = True

    def record_run_results(self):
        return False  # No storage in this process; the durable rows stay.


async def main():
    session = Session()
    with _sessions_lock:
        _active_sessions[{KEY!r}] = session
        _session_timestamps[{KEY!r}] = time.time()
    server = CommandControlServer('incarnation', apply_command_control)
    print(json.dumps(await server.start()), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await server.close()
    print(json.dumps({{'killed': session.killed, 'aborted': session.aborted}}), flush=True)

asyncio.run(main())
'''


def _line_within(stream, seconds):
    ready, _, _ = select.select([stream], [], [], seconds)
    assert ready, 'execution host did not answer'
    return json.loads(stream.readline())


@pytest.fixture
def execution_host():
    process = subprocess.Popen(
        [sys.executable, '-c', _EXECUTION_HOST], cwd=ROOT, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, 'PYTHONPATH': str(ROOT)})
    try:
        endpoint = _line_within(process.stdout, 120)

        def finish():
            process.stdin.close()
            return _line_within(process.stdout, 60)

        yield endpoint, finish
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.fixture
def api_app():
    from quart import g, request

    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.app.app_factory import create_base_app
    from routes.api_v1.activity import api_v1_activity_bp
    from routes.api_v1.swarm import api_v1_swarm_bp

    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def _authenticate():
        g.auth_ctx = AuthContext(key_id='xctl', name='xctl', scopes=frozenset({'agents:swarm'}),
                                 owner_user_id=int(request.headers.get('X-Owner', OWNER)))
        g.rate_decision = None

    for blueprint, name in ((api_v1_swarm_bp, 'xctl_swarm_v4'), (api_v1_activity_bp, 'xctl_activity_v4')):
        app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(blueprint, name=name)))
    return app


def _install_execution_owner(app, endpoint, *, state='ready'):
    from lib.tasks_pkg import command_control_runtime as runtime
    service = SimpleNamespace(host_matches=lambda record: True, record=lambda: {
        'state': state, 'serviceId': 'incarnation', 'commandControl': endpoint})
    app.extensions[runtime._CLIENT_KEY] = runtime.CommandControls(service=service)


def test_routes_reach_the_swarm_in_the_execution_host(execution_host, api_app):
    endpoint, finish = execution_host
    _install_execution_owner(api_app, endpoint)

    async def exercise():
        client = api_app.test_client()
        foreign = {'X-Owner': str(OWNER + 1)}

        activity = await client.get('/api/v4/activity/background')
        assert activity.status_code == 200
        [swarm] = (await activity.get_json())['data']['swarms']
        assert (swarm['swarm_id'], swarm['conv_id'], swarm['running'], swarm['pending']) == (
            RUN, KEY, 1, 1)
        other = await client.get('/api/v4/activity/background', headers=foreign)
        assert (await other.get_json())['data']['swarms'] == []

        stop = f'/api/v4/swarm/runs/{RUN}/agents/{{agent}}/stop'
        assert (await client.post(stop.format(agent='a'), headers=foreign)).status_code == 404
        pending = await client.post(stop.format(agent='b'))
        assert pending.status_code == 409
        problem = await pending.get_json()
        assert problem['code'] == 'swarm_agent_not_running' and 'pending' in problem['detail']
        stopped = await client.post(stop.format(agent='a'))
        assert stopped.status_code == 200
        assert (await stopped.get_json())['data'] == {'stopped': True, 'swarmId': RUN, 'agentId': 'a'}

        assert (await client.post(f'/api/v4/swarm/abort/{KEY}', headers=foreign)).status_code == 404
        assert (await client.post(f'/api/v4/swarm/abort/{KEY}')).status_code == 200
        assert (await client.post(f'/api/v4/swarm/abort/{KEY}')).status_code == 404

    asyncio.run(exercise())
    assert finish() == {'killed': ['a'], 'aborted': True}


def test_unconfirmed_execution_owner_is_unavailable_not_absent(api_app):
    _install_execution_owner(api_app, {}, state='starting')

    async def exercise():
        client = api_app.test_client()
        for response in (await client.get('/api/v4/activity/background'),
                         await client.post(f'/api/v4/swarm/runs/{RUN}/agents/a/stop'),
                         await client.post(f'/api/v4/swarm/abort/{KEY}')):
            assert response.status_code == 503

    asyncio.run(exercise())

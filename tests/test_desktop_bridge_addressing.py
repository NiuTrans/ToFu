"""Executable contract for owner-scoped desktop command routing."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from lib.desktop import bridge as db


OWNER = '1'


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    with db._async_waiters_lock:
        db._async_waiters.clear()


def _register(agent_id, *, owner=OWNER, name='device', **capabilities):
    meta = {'name': name, 'platform': 'linux'}
    if capabilities:
        meta['capabilities'] = capabilities
    db.register_agent(agent_id, meta, user_id=owner)


def _plant(
    cmd_id='cmd-1', *, target=None, owner=OWNER,
    cmd_type='desktop_list_files',
):
    command = {
        'id': cmd_id,
        'type': cmd_type,
        'params': {'path': '~'},
        'created_at': time.time(),
        'event': threading.Event(),
        'result': None,
        'error': None,
        'user_id': owner,
    }
    if target:
        command['target_agent_id'] = target
    with db.command_queue_lock:
        db.command_queue[cmd_id] = command
    return command


def _drain(agent_id, *, owner=OWNER, timeout=0.2):
    return _run_async(db.take_pending_commands_async(
        agent_id=agent_id, user_id=owner, timeout=timeout))


@pytest.mark.unit
class TestAgentRegistry:
    def test_registration_records_identity_capabilities_and_liveness(self):
        _register('agent-A', name='macbook', write=True, exec=False)
        agent = db.online_agents()[0]
        assert agent['agent_id'] == 'agent-A'
        assert agent['user_id'] == OWNER
        assert agent['name'] == 'macbook'
        assert agent['capabilities'] == {'write': True, 'exec': False}
        assert db.is_desktop_agent_connected()

    def test_owner_filtered_list_hides_other_devices(self):
        _register('agent-A', owner='101')
        _register('agent-B', owner='202')
        assert [a['agent_id'] for a in db.list_agents(user_id='101')] == [
            'agent-A']
        assert len(db.list_agents()) == 2

    def test_heartbeat_preserves_known_version_when_frame_omits_it(self):
        db.register_agent(
            'agent-A', {'name': 'mac', 'version': '2.0'}, user_id=OWNER)
        db.register_agent('agent-A', {'name': 'mac'}, user_id=OWNER)
        assert db.online_agents()[0]['version'] == '2.0'

    def test_stale_device_is_not_online(self):
        _register('agent-A')
        with db.command_queue_lock:
            db._agents['agent-A']['last_seen'] = time.time() - 3600
        assert db.online_agents() == []
        assert db.list_agents(user_id=OWNER)[0]['online'] is False


@pytest.mark.unit
class TestCommandRouting:
    def test_addressed_command_reaches_only_target_and_is_claimed(self):
        _register('agent-A')
        _register('agent-B')
        command = _plant(target='agent-A')
        assert _drain('agent-B') == []
        projected = _drain('agent-A')
        assert projected == [{
            'id': 'cmd-1',
            'type': 'desktop_list_files',
            'params': {'path': '~'},
            'target_agent_id': 'agent-A',
        }]
        assert command['claimed_agent_id'] == 'agent-A'

    def test_cross_owner_device_cannot_receive_command(self):
        _register('agent-A', owner='101')
        _register('agent-B', owner='202')
        _plant(target='agent-A', owner='101')
        assert _drain('agent-B', owner='202') == []
        assert _drain('agent-A', owner='101')[0]['id'] == 'cmd-1'

    def test_unaddressed_command_selects_only_owner_singleton(self):
        _register('agent-A', owner='101')
        _register('agent-B', owner='202')
        _plant(owner='101')
        assert _drain('agent-B', owner='202') == []
        assert _drain('agent-A', owner='101')[0]['id'] == 'cmd-1'

    def test_unaddressed_command_is_refused_with_multiple_owner_devices(self):
        _register('agent-A', name='mac')
        _register('agent-B', name='win')
        result, error = db.send_desktop_command(
            'desktop_list_files', {'path': '~'}, timeout=0.1,
            user_id=OWNER,
        )
        assert result is None
        assert error and 'target_agent_id' in error
        assert db.pending_commands_count() == 0

    def test_offline_explicit_target_is_refused_before_enqueue(self):
        result, error = db.send_desktop_command(
            'desktop_list_files', {'path': '~'}, timeout=0.1,
            target_agent_id='missing', user_id=OWNER,
        )
        assert result is None and 'missing' in error
        assert db.pending_commands_count() == 0


@pytest.mark.unit
class TestSettlementAuthority:
    def test_only_claiming_device_can_settle_result(self):
        _register('agent-A')
        _register('agent-B')
        command = _plant(target='agent-A')
        _drain('agent-A')
        result = [{'id': command['id'], 'result': {'ok': True}, 'error': None}]
        assert db.resolve_results(
            result, agent_id='agent-B', user_id=OWNER) == 0
        assert not command['event'].is_set()
        assert db.resolve_results(
            result, agent_id='agent-A', user_id=OWNER) == 1
        assert command['event'].is_set()

    def test_only_claiming_owner_and_device_can_append_stream_frames(self):
        _register('agent-A', owner='101')
        command = _plant(target='agent-A', owner='101')
        _drain('agent-A', owner='101')
        frames = [{
            'cmd_id': command['id'], 'seq': 1, 'stream': 'stdout',
            'data': 'hello', 'done': True,
        }]
        assert db.resolve_streams(
            frames, agent_id='agent-A', user_id='202') == 0
        assert db.get_command_stream(command['id']) is None
        assert db.resolve_streams(
            frames, agent_id='agent-A', user_id='101') == 1
        assert db.get_command_stream(command['id'])['stdout'] == 'hello'

    def test_blocking_roundtrip_uses_claimed_device(self):
        _register('agent-A')
        outcome = {}

        def producer():
            outcome['result'], outcome['error'] = db.send_desktop_command(
                'desktop_list_files', {'path': '~'}, timeout=3,
                target_agent_id='agent-A', user_id=OWNER)

        thread = threading.Thread(target=producer)
        thread.start()
        deadline = time.time() + 2
        while db.pending_commands_count() == 0 and time.time() < deadline:
            time.sleep(0.02)
        command = _drain('agent-A')[0]
        db.resolve_results(
            [{'id': command['id'], 'result': {'entries': []}, 'error': None}],
            agent_id='agent-A', user_id=OWNER,
        )
        thread.join(timeout=3)
        assert outcome == {'result': {'entries': []}, 'error': None}


@pytest.mark.api
class TestPollRoute:
    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        monkeypatch.setattr(db, 'POLL_WAIT_TIMEOUT', 0.1)

    @staticmethod
    def _auth():
        from lib.bridge_auth import process_agent_token
        return {'X-Bridge-Secret': process_agent_token()}

    def test_poll_registers_stable_agent(self, flask_client):
        response = flask_client.post('/api/desktop/poll', json={
            'results': [],
            'agent': {'agent_id': 'agent-A', 'name': 'mac'},
        }, headers=self._auth())
        assert response.status_code == 200
        assert db.online_agents()[0]['agent_id'] == 'agent-A'

    def test_authenticated_poll_without_identity_is_rejected(self, flask_client):
        response = flask_client.post(
            '/api/desktop/poll', json={'results': []}, headers=self._auth())
        assert response.status_code == 400
        assert response.get_json()['error'] == 'desktop_agent_identity_required'

    def test_poll_without_credential_is_rejected(self, flask_client):
        response = flask_client.post('/api/desktop/poll', json={
            'results': [], 'agent': {'agent_id': 'agent-A'},
        }, scope_base={'client': ('127.0.0.1', 5555)})
        assert response.status_code == 401


@pytest.mark.unit
class TestAgentSide:
    @staticmethod
    def _run_once(monkeypatch, tmp_path):
        import lib.desktop_agent._run as agent_runtime

        captured = {}
        stop = threading.Event()

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {'commands': []}

        def fake_post(_url, json=None, **_kwargs):
            captured['body'] = json
            stop.set()
            return Response()

        monkeypatch.setattr(agent_runtime.requests, 'post', fake_post)
        monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
        agent_runtime.run_agent(
            'http://server.example',
            {'allow_write': True, 'allow_exec': False, 'allow_gui': False,
             'allow_notification': True},
            poll_interval=0.01,
            stop_event=stop,
        )
        return captured['body']['agent']

    def test_agent_posts_identity_on_every_poll_and_keeps_it_across_restart(
        self, monkeypatch, tmp_path,
    ):
        first = self._run_once(monkeypatch, tmp_path)
        second = self._run_once(monkeypatch, tmp_path)
        assert first['agent_id']
        assert first['agent_id'] == second['agent_id']
        assert first['capabilities']['write'] is True

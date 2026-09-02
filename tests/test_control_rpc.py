"""Executable budgets and lifecycle guarantees for control RPC v1."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest
import yaml


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Client:
    def __init__(self) -> None:
        self.frames = []

    def enqueue_rpc(self, frame):
        self.frames.append(frame)
        return True


async def _wait_for_frame(client: _Client, request_id: str):
    for _ in range(200):
        for frame in client.frames:
            if frame.get('id') == request_id:
                return frame
        await asyncio.sleep(0.005)
    raise AssertionError(f'no control RPC response for {request_id}')


def test_contract_matches_runtime_budgets_and_allowlist():
    import lib.control_rpc as rpc

    contract = yaml.safe_load(
        (ROOT / 'contracts/control_rpc_v1.yaml').read_text(encoding='utf-8'))

    assert contract['contract'] == 'tofu.control-rpc/v1'
    assert contract['transport']['endpoint'] == '/api/push'
    assert contract['budgets']['requestBytes'] == rpc.REQUEST_MAX_BYTES
    assert contract['budgets']['responseBytes'] == rpc.RESPONSE_MAX_BYTES
    assert contract['budgets']['inFlightPerConnection'] \
        == rpc.IN_FLIGHT_PER_CONNECTION
    assert set(contract['methods']) == set(rpc.METHODS)


def test_project_browse_round_trip_uses_only_the_explicit_method(tmp_path):
    from lib.control_rpc import ControlRpcSession

    (tmp_path / 'alpha').mkdir()
    (tmp_path / 'beta.py').write_text('pass', encoding='utf-8')

    async def scenario():
        client = _Client()
        session = ControlRpcSession(client, user_id=7, request_id='page-ws1')
        assert session.receive({
            'jsonrpc': '2.0',
            'id': 'browse-1',
            'method': 'project.browse',
            'params': {'path': str(tmp_path), 'showHidden': False},
        }) is True
        frame = await _wait_for_frame(client, 'browse-1')
        await session.close()
        return frame

    frame = asyncio.run(scenario())
    assert frame['result']['ok'] is True
    assert frame['result']['path'] == str(tmp_path)
    assert [row['name'] for row in frame['result']['dirs']] == ['alpha']


def test_unknown_method_fails_without_invoking_an_http_path():
    from lib.control_rpc import ControlRpcSession, METHOD_NOT_FOUND

    async def scenario():
        client = _Client()
        session = ControlRpcSession(client, user_id=1)
        assert session.receive({
            'jsonrpc': '2.0', 'id': 'unknown-1',
            'method': '/api/v1/project/browse', 'params': {},
        }) is True
        await session.close()
        return client.frames

    frames = asyncio.run(scenario())
    assert frames[-1]['error']['code'] == METHOD_NOT_FOUND


def test_timeout_keeps_global_capacity_until_blocking_work_really_exits(
        monkeypatch):
    import lib.control_rpc as rpc

    started = threading.Event()
    finish = threading.Event()

    class _Slots:
        def __init__(self):
            self.acquired = 0
            self.released = 0

        def acquire(self, *, blocking):
            assert blocking is False
            self.acquired += 1
            return True

        def release(self):
            self.released += 1

    slots = _Slots()
    monkeypatch.setattr(rpc, '_GLOBAL_SLOTS', slots)

    def blocked(_context, _params):
        started.set()
        finish.wait(2)
        return {'ok': True}

    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(
            client,
            user_id=1,
            methods={'test.blocked': rpc.RpcMethod(blocked, 0.02)},
        )
        session.receive({
            'jsonrpc': '2.0', 'id': 'blocked-1',
            'method': 'test.blocked', 'params': {},
        })
        assert await asyncio.to_thread(started.wait, 1)
        frame = await _wait_for_frame(client, 'blocked-1')
        assert frame['error']['code'] == rpc.TIMED_OUT
        assert slots.released == 0
        finish.set()
        for _ in range(200):
            if slots.released:
                break
            await asyncio.sleep(0.005)
        await session.close()
        return slots.acquired, slots.released

    assert asyncio.run(scenario()) == (1, 1)


def test_timed_out_worker_keeps_its_per_connection_capacity(monkeypatch):
    import lib.control_rpc as rpc

    started = threading.Event()
    finish = threading.Event()
    monkeypatch.setattr(rpc, 'IN_FLIGHT_PER_CONNECTION', 1)

    def blocked(_context, _params):
        started.set()
        finish.wait(2)
        return {'ok': True}

    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(
            client,
            user_id=1,
            methods={'test.blocked': rpc.RpcMethod(blocked, 0.02)},
        )
        session.receive({
            'jsonrpc': '2.0', 'id': 'blocked-1',
            'method': 'test.blocked', 'params': {},
        })
        assert await asyncio.to_thread(started.wait, 1)
        first = await _wait_for_frame(client, 'blocked-1')
        assert first['error']['code'] == rpc.TIMED_OUT

        session.receive({
            'jsonrpc': '2.0', 'id': 'blocked-2',
            'method': 'test.blocked', 'params': {},
        })
        second = await _wait_for_frame(client, 'blocked-2')
        finish.set()
        await session.close()
        return second

    frame = asyncio.run(scenario())
    assert frame['error']['code'] == rpc.OVERLOADED
    assert frame['error']['data']['reason'] == 'connection_capacity'


def test_push_websocket_routes_control_rpc_end_to_end(monkeypatch, tmp_path):
    from lib.api_keys import AuthContext
    from lib.app_factory import create_base_app
    from routes.push import push_bp

    context = AuthContext(
        key_id='rpc-test',
        owner_user_id=17,
        account_user_id='rpc-test-user',
        scopes=frozenset({'chat'}),
    )
    monkeypatch.setattr(
        'routes.push._resolve_push_ws_auth',
        lambda _headers, _cookies, _peer: (context, 'token'),
    )
    (tmp_path / 'project').mkdir()
    app = create_base_app('control-rpc-websocket-test', {'TESTING': True})
    app.register_blueprint(push_bp)

    async def scenario():
        async with app.test_app():
            async with app.test_client().websocket(
                    '/api/push?_rid=rpc-websocket-test') as connection:
                await connection.send_json({
                    'jsonrpc': '2.0',
                    'id': 'browse-over-websocket',
                    'method': 'project.browse',
                    'params': {'path': str(tmp_path), 'showHidden': False},
                })
                return await asyncio.wait_for(
                    connection.receive_json(), timeout=2)

    frame = asyncio.run(scenario())
    assert frame['id'] == 'browse-over-websocket'
    assert frame['result']['ok'] is True
    assert [item['name'] for item in frame['result']['dirs']] == ['project']


def test_cancel_notification_is_correlated_and_visible():
    import lib.control_rpc as rpc

    started = threading.Event()
    finish = threading.Event()

    def blocked(_context, _params):
        started.set()
        finish.wait(2)
        return {'ok': True}

    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(
            client,
            user_id=1,
            methods={'test.blocked': rpc.RpcMethod(blocked, 1.0)},
        )
        session.receive({
            'jsonrpc': '2.0', 'id': 'cancel-1',
            'method': 'test.blocked', 'params': {},
        })
        assert await asyncio.to_thread(started.wait, 1)
        session.receive({
            'jsonrpc': '2.0', 'method': '$/cancelRequest',
            'params': {'id': 'cancel-1'},
        })
        frame = await _wait_for_frame(client, 'cancel-1')
        finish.set()
        await session.close()
        return frame

    assert asyncio.run(scenario())['error']['code'] == rpc.REQUEST_CANCELLED


def test_push_client_rpc_lane_never_drops_a_correlated_response():
    from lib.agent_core.push import PushClient

    client = PushClient(user_id=1)
    client._rpc_capacity = 1
    assert client.enqueue_rpc({'jsonrpc': '2.0', 'id': 'one'}) is True
    assert client.enqueue_rpc({'jsonrpc': '2.0', 'id': 'two'}) is False
    assert asyncio.run(client.drain()) is None

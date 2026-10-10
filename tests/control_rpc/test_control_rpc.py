"""Executable budgets and lifecycle guarantees for control RPC v1."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest
import yaml


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


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
    assert contract['budgets']['requestsPerConnectionPerMinute'] == rpc.REQUESTS_PER_MINUTE
    assert contract['transport']['cancellationMethod'] == rpc.CANCEL_METHOD
    assert contract['transport']['framing'] == 'json-rpc-' + rpc.JSON_RPC_VERSION
    assert contract['errors'] == {
        'invalidRequest': rpc.INVALID_REQUEST,
        'methodNotFound': rpc.METHOD_NOT_FOUND,
        'invalidParams': rpc.INVALID_PARAMS,
        'internal': rpc.INTERNAL_ERROR,
        'overloaded': rpc.OVERLOADED,
        'timedOut': rpc.TIMED_OUT,
        'responseTooLarge': rpc.RESPONSE_TOO_LARGE,
        'domain': rpc.DOMAIN_ERROR,
        'cancelled': rpc.REQUEST_CANCELLED,
    }
    assert set(contract['methods']) == set(rpc.METHODS)
    assert all(method.timeout_seconds * 1000
               == contract['budgets']['methodTimeoutMilliseconds']
               for method in rpc.METHODS.values())


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
            'method': '/api/v4/project/browse', 'params': {},
        }) is True
        await session.close()
        return client.frames

    frames = asyncio.run(scenario())
    assert frames[-1]['error']['code'] == METHOD_NOT_FOUND


def test_timeout_keeps_physical_accounting_until_worker_really_exits():
    import lib.control_rpc as rpc
    started, finish = threading.Event(), threading.Event()
    initial = rpc.control_rpc_metrics()['active_workers']
    def blocked(context, params):
        started.set()
        assert finish.wait(3)
        return {'ok': True}
    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(client, user_id=1,
            methods={'blocked': rpc.RpcMethod(blocked, 0.02)})
        try:
            session.receive({'jsonrpc': '2.0', 'id': 'first', 'method': 'blocked'})
            assert await asyncio.to_thread(started.wait, 1)
            assert (await _wait_for_frame(client, 'first'))['error']['code'] == rpc.TIMED_OUT
            assert rpc.control_rpc_metrics()['active_workers'] == initial + 1
            await session.close()
            assert rpc.control_rpc_metrics()['active_workers'] == initial + 1
        finally:
            finish.set()
            for _ in range(200):
                if rpc.control_rpc_metrics()['active_workers'] == initial:
                    break
                await asyncio.sleep(.005)
            await session.close()
        assert rpc.control_rpc_metrics()['active_workers'] == initial
    asyncio.run(scenario())


def test_burst_starts_without_socket_rate_global_or_default_pool_limits():
    from contextvars import ContextVar
    from concurrent.futures import ThreadPoolExecutor
    import lib.control_rpc as rpc
    entered = [threading.Event() for _ in range(144)]
    release = threading.Event()
    marker = ContextVar('rpc-test-owner', default='missing')
    def handler(context, params):
        assert context.user_id == '7' and marker.get() == 'owner-context'
        entered[params['index']].set()
        assert release.wait(5)
        return {'index': params['index']}
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        marker.set('owner-context')
        client = _Client()
        session = rpc.ControlRpcSession(client, user_id=7,
            methods={'work': rpc.RpcMethod(handler, 5)})
        try:
            for index in range(144):
                session.receive({'jsonrpc': '2.0', 'id': str(index),
                    'method': 'work', 'params': {'index': index}})
            deadline = loop.time() + 2
            while not all(event.is_set() for event in entered):
                assert loop.time() < deadline, 'an artificial capacity gate queued work'
                await asyncio.sleep(.005)
            assert not client.frames
            release.set()
            for index in range(144):
                frame = await _wait_for_frame(client, str(index))
                assert frame['result']['index'] == index
        finally:
            release.set()
            await session.close()
    asyncio.run(scenario())


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


def test_push_websocket_exits_before_lifespan_shutdown_when_drain_starts(
        monkeypatch):
    from lib.agent_core.push import PushHub
    from lib.api_keys import AuthContext
    from lib.app_factory import create_base_app
    from lib.server_shutdown import wait_for_shutdown_trigger
    from routes.push import push_bp

    context = AuthContext(
        key_id='shutdown-test', owner_user_id=17,
        account_user_id='shutdown-test-user', scopes=frozenset({'chat'}),
    )
    monkeypatch.setattr(
        'routes.push._resolve_push_ws_auth',
        lambda _headers, _cookies, _peer: (context, 'token'),
    )
    hub = PushHub()
    monkeypatch.setattr('routes.push.hub', hub)
    app = create_base_app('push-shutdown-test', {'TESTING': True})
    app.register_blueprint(push_bp)

    async def scenario():
        async with app.test_app():
            async with app.test_client().websocket('/api/push'):
                for _ in range(100):
                    if hub.client_count == 1:
                        break
                    await asyncio.sleep(0.01)
                assert hub.client_count == 1
                shutdown_requested = threading.Event()
                shutdown_requested.set()
                await wait_for_shutdown_trigger(
                    shutdown_requested,
                    close_push_clients=hub.close_clients_for_shutdown,
                )
                for _ in range(100):
                    if hub.client_count == 0:
                        break
                    await asyncio.sleep(0.01)
                assert hub.client_count == 0

    asyncio.run(scenario())


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
    frames = [{'jsonrpc': '2.0', 'id': index} for index in range(256)]
    assert all(client.enqueue_rpc(frame) for frame in frames)
    async def drain():
        return [await client.drain() for _ in frames]
    assert asyncio.run(drain()) == frames
    client.disconnect()
    assert asyncio.run(client.drain()) is None


def test_worker_finishing_after_event_loop_close_still_releases_physical_accounting():
    import lib.control_rpc as rpc
    entered, release = threading.Event(), threading.Event()
    initial = rpc.control_rpc_metrics()['active_workers']
    def handler(context, params):
        entered.set()
        assert release.wait(5)
        return {'ok': True}
    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(client, user_id=7,
            methods={'blocked': rpc.RpcMethod(handler, .01)})
        session.receive({'jsonrpc': '2.0', 'id': 'late', 'method': 'blocked'})
        await _wait_for_frame(client, 'late')
        assert entered.is_set()
        await session.close()
    try:
        asyncio.run(scenario())
        assert rpc.control_rpc_metrics()['active_workers'] == initial + 1
    finally:
        release.set()
        import time
        deadline = time.monotonic() + 2
        while rpc.control_rpc_metrics()['active_workers'] != initial:
            assert time.monotonic() < deadline
            time.sleep(.005)


def test_physical_worker_start_failure_is_typed_and_cannot_execute_later(monkeypatch):
    import lib.control_rpc as rpc
    original = threading.Thread.start
    runs = []
    initial = rpc.control_rpc_metrics()['active_workers']
    def fail(thread):
        raise RuntimeError('injected thread creation failure')
    async def scenario():
        client = _Client()
        session = rpc.ControlRpcSession(client, user_id=7,
            methods={'read': rpc.RpcMethod(lambda context, params: runs.append(params['id']) or {}, 1)})
        try:
            monkeypatch.setattr(threading.Thread, 'start', fail)
            assert session.receive({'jsonrpc': '2.0', 'id': 'failed', 'method': 'read', 'params': {'id': 'failed'}})
            frame = await _wait_for_frame(client, 'failed')
            assert frame['error']['code'] == rpc.INTERNAL_ERROR
            assert frame['error']['data']['reason'] == 'worker_start_failed'
            assert rpc.control_rpc_metrics()['active_workers'] == initial
            monkeypatch.setattr(threading.Thread, 'start', original)
            session.receive({'jsonrpc': '2.0', 'id': 'next', 'method': 'read', 'params': {'id': 'next'}})
            assert (await _wait_for_frame(client, 'next'))['result'] == {}
            assert runs == ['next']
        finally:
            monkeypatch.setattr(threading.Thread, 'start', original)
            await session.close()
    asyncio.run(scenario())

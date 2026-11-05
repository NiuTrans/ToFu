"""Push handshake auth stays off-loop, fail-closed and owned until quiescence."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar

import pytest
from quart import current_app, websocket
from quart.testing import WebsocketResponseError

from lib import log
from lib.agent_core.push import PushHub
from lib.api_keys import AuthContext
from lib.app.app_factory import create_base_app
from lib.http_transport.error_handlers import register_http_error_handlers
from lib.observability import InstrumentedThreadPoolExecutor


pytestmark = pytest.mark.unit


@pytest.fixture
def push_auth_app(monkeypatch):
    import routes.push as push

    hub = PushHub()
    monkeypatch.setattr(push, 'hub', hub)
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: True)
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', False)
    app = create_base_app('push-auth-offloop-test', {
        'TESTING': True, 'PROPAGATE_EXCEPTIONS': False,
    })
    register_http_error_handlers(app)
    app.register_blueprint(push.push_bp)
    return app, hub


def _run(coroutine):
    async def exercise():
        # Use the same demand-backed default executor as the serving loop.
        # The hint is idle reuse, not a second auth queue or concurrency cap.
        asyncio.get_running_loop().set_default_executor(
            InstrumentedThreadPoolExecutor(
                max_workers=1, metric_pool='push-auth-test',
                thread_name_prefix='push-auth-test'))
        return await coroutine

    return asyncio.run(exercise())


async def _wait_entered(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def test_slow_auth_runs_off_serving_thread_while_heartbeat_progresses(
        push_auth_app, monkeypatch):
    app, hub = push_auth_app
    entered, heartbeat_tick = threading.Event(), threading.Event()
    observed = {}
    context = AuthContext(key_id='slow-auth', owner_user_id=17,
                          scopes=frozenset({'chat'}))

    def expensive_validation(token):
        observed['token'] = token
        observed['auth_thread'] = threading.get_ident()
        entered.set()
        # Model blocking credential storage. The fallback bounds the failing
        # on-loop baseline; a healthy serving heartbeat releases the worker.
        observed['heartbeat_during_auth'] = heartbeat_tick.wait(1)
        return context

    monkeypatch.setattr('lib.api_keys.validate_token', expensive_validation)

    async def exercise():
        serving_thread = threading.get_ident()

        async def heartbeat():
            await _wait_entered(entered)
            heartbeat_tick.set()

        ticker = asyncio.create_task(heartbeat())
        try:
            async with app.test_client().websocket('/api/push', headers={
                    'Authorization': 'Bearer tofu_live_slow'}) as connection:
                await connection.send_json({'action': 'ping', 't': 7})
                async with asyncio.timeout(3):
                    assert await connection.receive_json() == {
                        'channel': 'system', 'type': 'pong', 't': 7}
                assert next(iter(hub._clients)).user_id == '17'
                assert observed['token'] == 'tofu_live_slow'
                assert observed['auth_thread'] != serving_thread, observed
                assert observed['heartbeat_during_auth'] is True
        finally:
            heartbeat_tick.set()
            await ticker
        assert hub.client_count == 0

    _run(exercise())


@pytest.mark.parametrize('headers, expected_token', [
    ({'authorization': 'Bearer tofu_live_header',
      'X-API-KEY': 'tofu_live_api',
      'Cookie': 'tofu_session=tofu_live_cookie'}, 'tofu_live_header'),
    ({'Authorization': 'Basic ignored', 'X-API-KEY': 'tofu_live_api',
      'Cookie': 'tofu_session=tofu_live_cookie'}, 'tofu_live_api'),
    ({'Cookie': 'tofu_session=tofu_live_cookie'}, 'tofu_live_cookie'),
])
def test_auth_snapshots_transport_and_copies_context_before_worker_dispatch(
        push_auth_app, monkeypatch, headers, expected_token):
    import routes.push as push

    app, hub = push_auth_app
    marker = ContextVar('push-auth-context', default='outside')
    entered, release = threading.Event(), threading.Event()
    captured, observed = {}, {}
    peer = ['203.0.113.7', 5555]
    context = AuthContext(key_id='snapshot-auth', owner_user_id=17,
                          account_user_id='alice', scopes=frozenset({'chat'}))
    resolve_auth = push._resolve_push_ws_auth
    dispatch_frame = push._handle_client_frame

    @app.before_websocket
    async def capture_transport():
        captured.update(headers=websocket.headers, cookies=websocket.cookies)
        marker.set('socket-context')
        log.set_req_id('copied-request-id')
        log.set_principal('before-auth', 23)

    def resolve_snapshot(worker_headers, worker_cookies, worker_peer):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('test did not release auth')
        observed.update(
            detached_headers=worker_headers is not captured['headers'],
            detached_cookies=worker_cookies is not captured['cookies'],
            peer=worker_peer,
            context=(marker.get(), log.req_id(), log.principal(), current_app.name),
        )
        # Worker ContextVar mutations must not replace the handler's later
        # authenticated binding, nor leak into another connection/caller.
        log.set_principal('worker-only', 99)
        return resolve_auth(worker_headers, worker_cookies, worker_peer)

    def validate(token):
        observed['token'] = token
        return context

    def capture_frame(client, raw):
        observed['frame'] = (client.user_id, client.req_id, log.principal())
        dispatch_frame(client, raw)

    monkeypatch.setattr(push, '_resolve_push_ws_auth', resolve_snapshot)
    monkeypatch.setattr('lib.api_keys.validate_token', validate)
    monkeypatch.setattr(push, '_handle_client_frame', capture_frame)

    async def exercise():
        caller_principal = log.principal()
        async with app.test_client().websocket(
                '/api/push?_rid=snapshot-rid', headers=headers,
                scope_base={'client': peer}) as connection:
            try:
                await _wait_entered(entered)
                captured['headers']['Authorization'] = 'Bearer tofu_live_changed'
                captured['headers']['X-API-KEY'] = 'tofu_live_changed'
                peer[0] = '127.0.0.1'
            finally:
                release.set()
            await connection.send_json({'action': 'ping', 't': 8})
            async with asyncio.timeout(3):
                assert (await connection.receive_json())['t'] == 8
            assert observed == {
                'detached_headers': True, 'detached_cookies': True,
                'peer': ('203.0.113.7', 5555),
                'context': ('socket-context', 'copied-request-id',
                            ('before-auth', '23'), app.name),
                'token': expected_token,
                'frame': ('17', 'snapshot-rid', ('snapshot-auth', '17')),
            }
        assert hub.client_count == 0
        assert log.principal() == caller_principal

    _run(exercise())


@pytest.mark.parametrize('interruption', ['disconnect', 'cancel'])
@pytest.mark.parametrize('auth_fails', [False, True])
def test_interrupted_handshake_joins_auth_without_late_socket_registration(
        push_auth_app, monkeypatch, interruption, auth_fails):
    app, hub = push_auth_app
    entered, release, finished = (threading.Event() for _ in range(3))
    handler, registered, teardown = [], [], []
    context = AuthContext(key_id='interrupted-auth', owner_user_id=17,
                          scopes=frozenset({'chat'}))
    register = hub.register

    @app.before_websocket
    async def capture_owner():
        handler.append(asyncio.current_task())

    @app.teardown_websocket
    async def capture_teardown(_error):
        teardown.append(finished.is_set())

    def blocked_validation(_token):
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError('test did not release auth')
            if auth_fails:
                raise OSError('injected credential storage failure')
            return context
        finally:
            finished.set()

    def capture_registration(client):
        registered.append(client.user_id)
        return register(client)

    monkeypatch.setattr('lib.api_keys.validate_token', blocked_validation)
    monkeypatch.setattr(hub, 'register', capture_registration)

    async def exercise():
        async with app.test_client().websocket('/api/push', headers={
                'Authorization': 'Bearer tofu_live_interrupted'}) as connection:
            try:
                await _wait_entered(entered)
                owner = handler[0]
                if interruption == 'disconnect':
                    await connection.disconnect()
                else:
                    owner.cancel()
                async with asyncio.timeout(3):
                    while not owner.cancelling():
                        await asyncio.sleep(0.001)
                # A second cancellation cannot release ownership of a still
                # executing physical validator or permit its late grant.
                await asyncio.sleep(0)
                owner.cancel()
                await asyncio.sleep(0)
                assert not owner.done() and not finished.is_set()
                assert teardown == [] and registered == []
                assert not connection.accepted and hub.client_count == 0
            finally:
                release.set()
        assert finished.is_set() and handler[0].cancelled()
        assert teardown == [True]
        assert registered == [] and hub.client_count == 0
        assert not connection.accepted

    _run(exercise())


@pytest.mark.parametrize('rejection', [
    'missing', 'invalid', 'storage_error', 'ownerless_multi_user', 'remote_open',
])
def test_denied_auth_never_accepts_or_registers_a_socket(
        push_auth_app, monkeypatch, rejection):
    app, hub = push_auth_app
    registered = []
    context = AuthContext(key_id='ownerless', scopes=frozenset({'chat'}))

    def validate(_token):
        if rejection == 'storage_error':
            raise OSError('injected credential storage failure')
        return context if rejection == 'ownerless_multi_user' else None

    monkeypatch.setattr('lib.api_keys.validate_token', validate)
    monkeypatch.setattr('lib.auth_mode.requires_credential',
                        lambda: rejection != 'remote_open')
    monkeypatch.setattr('lib.auth_mode.is_multi_user',
                        lambda: rejection == 'ownerless_multi_user')
    monkeypatch.setattr(hub, 'register', lambda client: registered.append(client))
    headers = {'Remote-Addr': '127.0.0.1', 'X-Forwarded-For': '127.0.0.1'}
    if rejection not in {'missing', 'remote_open'}:
        headers['Authorization'] = 'Bearer tofu_live_denied'

    async def exercise():
        connection = app.test_client().websocket(
            '/api/push', headers=headers,
            scope_base={'client': ('203.0.113.7', 5555)})
        with pytest.raises(WebsocketResponseError) as denied:
            async with connection:
                async with asyncio.timeout(3):
                    await connection.receive()
        assert denied.value.response.status_code == 401
        assert not connection.accepted
        assert registered == [] and hub.client_count == 0

    _run(exercise())


def test_open_loopback_handshake_keeps_personal_owner_and_principal(
        push_auth_app, monkeypatch):
    from lib.identity import PERSONAL_USER_ID

    app, hub = push_auth_app
    registered = []
    register = hub.register
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: False)

    def capture_registration(client):
        registered.append((client.user_id, log.principal()))
        return register(client)

    monkeypatch.setattr(hub, 'register', capture_registration)

    async def exercise():
        async with app.test_client().websocket(
                '/api/push', scope_base={'client': ('::1', 5555)}) as connection:
            await connection.send_json({'action': 'ping', 't': 9})
            async with asyncio.timeout(3):
                assert (await connection.receive_json())['t'] == 9
            owner = str(PERSONAL_USER_ID)
            assert registered == [(owner, ('local', owner))]
        assert hub.client_count == 0

    _run(exercise())
